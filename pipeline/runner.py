"""
Main pipeline runner — processes companies one by one, extracting emissions,
financial data, market data, and industry classifications.

Walks backwards from TARGET_END_YEAR to TARGET_START_YEAR, searching for
each missing year in turn. Bonus years found during a search are kept,
so earlier years get skipped if already covered.
"""

import logging
import os
import time
from datetime import datetime, date as date_type

import anthropic

from db.models import (
    Company, EmissionsRecord, FinancialRecord, Source, PipelineRun,
    get_session, create_tables,
)
from pipeline.searcher import search_for_emissions_source, search_for_annual_report, search_for_financial_history
from pipeline.parser import (
    parse_pdf, extract_tables_from_documents, parse_html, parse_excel,
    extract_html_text, detect_source_type, download_to_tempfile,
    render_pdf_page,
)
from pipeline.fallback_parser import parse_with_fallbacks
from pipeline.extractor import (
    find_emissions_tables, extract_emissions, extract_emissions_from_text,
    extract_emissions_from_pdf,
)
from pipeline.financial_extractor import find_financial_tables, extract_financials, normalise_to_units
from pipeline.market_data import get_equity_value_at_date, get_industry_info
from pipeline.industry_classifier import classify_company
from pipeline.storage import upload_file
from pipeline.config import (
    TARGET_START_YEAR, TARGET_END_YEAR, MAX_SEARCHES,
    CONFIDENCE_THRESHOLD, MODEL_FAST, MODEL_STRONG,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Cost tracking ────────────────────────────────────────────────────────

MODEL_COSTS = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-5": (15.0, 75.0),
}


def _new_cost_tracker():
    return {"total": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}


class _TrackedMessages:
    """Proxy for client.messages that records token usage and cost."""

    def __init__(self, messages, tracker):
        self._messages = messages
        self._tracker = tracker

    def create(self, **kwargs):
        response = self._messages.create(**kwargs)
        model = kwargs.get("model", "unknown")
        usage = response.usage
        in_rate, out_rate = MODEL_COSTS.get(model, (15.0, 75.0))
        cost = (usage.input_tokens * in_rate + usage.output_tokens * out_rate) / 1_000_000
        self._tracker["total"] += cost
        self._tracker["calls"] += 1
        self._tracker["input_tokens"] += usage.input_tokens
        self._tracker["output_tokens"] += usage.output_tokens
        return response


class _TrackedClient:
    """Wraps an Anthropic client to track API costs transparently."""

    def __init__(self, client, tracker):
        self._client = client
        self.messages = _TrackedMessages(client.messages, tracker)

    def __getattr__(self, name):
        return getattr(self._client, name)


def _parse_document(url, source_type, llama_key):
    """Parse a document and return table dicts + markdown list.

    Tries multiple access strategies (direct HTTP → Exa cache → Playwright
    → Wayback Machine) before giving up.
    """
    exa_key = os.environ.get("EXA_API_KEY")
    table_dicts, method, elapsed = parse_with_fallbacks(
        url=url,
        llama_key=llama_key,
        exa_key=exa_key,
        source_type=source_type,
    )
    if method != "direct":
        log.info(f"    Fetched via {method} fallback ({elapsed:.1f}s)")
    tables_md = [t["markdown"] for t in table_dicts]
    return table_dicts, tables_md


def _capture_source_preview(url, source_type, table_dicts, matched_table_idx, company_name):
    """Capture a screenshot (PDF) or HTML snippet for the matched table."""
    screenshot_path = None
    html_snippet = None
    page_number = None
    s3_pdf_key = None

    if source_type == "pdf" and matched_table_idx is not None:
        page_number = table_dicts[matched_table_idx].get("page_index")
        existing_screenshot = table_dicts[matched_table_idx].get("screenshot_path")

        if existing_screenshot and os.path.exists(existing_screenshot):
            screenshot_path = existing_screenshot
        try:
            pdf_local_path = download_to_tempfile(url)
            if page_number is not None and not screenshot_path:
                safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
                screenshots_dir = os.path.join(os.path.dirname(__file__), "..", "screenshots")
                os.makedirs(screenshots_dir, exist_ok=True)
                screenshot_path = os.path.join(screenshots_dir, f"{safe_name}_p{page_number}.png")
                render_pdf_page(pdf_local_path, page_number, screenshot_path)

            try:
                safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
                s3_key = f"sources/{safe_name}/report.pdf"
                s3_pdf_key = upload_file(pdf_local_path, s3_key)
            except Exception as e:
                log.warning(f"  Failed to upload PDF to S3: {e}")

            os.unlink(pdf_local_path)
        except Exception as e:
            log.warning(f"  Failed to capture PDF screenshot: {e}")

    elif matched_table_idx is not None:
        html_snippet = table_dicts[matched_table_idx].get("html_snippet")

    return screenshot_path, html_snippet, page_number, s3_pdf_key


def _parse_date(date_str):
    """Safely parse a YYYY-MM-DD string to a date, or return None."""
    if not date_str:
        return None
    try:
        return date_type.fromisoformat(date_str)
    except (ValueError, TypeError):
        return None


def _get_covered_years(session, company_id, model_class):
    """Return the set of reporting_years already in the database."""
    rows = session.query(model_class.reporting_year).filter_by(company_id=company_id).all()
    return {r[0] for r in rows}


def _target_years():
    """Return the full set of years we want data for."""
    return set(range(TARGET_START_YEAR, TARGET_END_YEAR + 1))


def _get_financial_needs(session, company_id, target_years):
    """Return years that still need financial data: no record, or key fields null.

    Revenue comes from the income statement and debt/cash from the balance sheet —
    these may be in different documents, so a record can exist with only some fields.
    This function ensures the pipeline keeps searching until all three are filled.
    """
    from sqlalchemy import or_

    covered_rows = (
        session.query(FinancialRecord.reporting_year)
        .filter_by(company_id=company_id)
        .all()
    )
    covered = {r[0] for r in covered_rows}
    no_record = target_years - covered

    # Years where the record exists but key fields are still null
    incomplete_rows = (
        session.query(FinancialRecord.reporting_year)
        .filter(
            FinancialRecord.company_id == company_id,
            FinancialRecord.reporting_year.in_(list(target_years)),
            or_(
                FinancialRecord.revenue.is_(None),
                FinancialRecord.outstanding_debt.is_(None),
                FinancialRecord.cash_and_equivalents.is_(None),
            ),
        )
        .all()
    )
    incomplete = {r[0] for r in incomplete_rows}

    return no_record | incomplete


def _try_parse_candidates(candidates, searched_urls, llama_key, max_attempts=3):
    """Try to parse documents from a ranked candidate list, skipping 403s.

    Returns (url, title, source_type, table_dicts, tables_md) or raises if all fail.
    """
    attempts = 0
    last_error = None

    for candidate in candidates:
        url = candidate["url"]
        if url in searched_urls:
            continue
        if attempts >= max_attempts:
            break

        attempts += 1
        searched_urls.add(url)
        source_type = detect_source_type(url)
        title = candidate["title"]

        try:
            table_dicts, tables_md = _parse_document(url, source_type, llama_key)
            if tables_md:
                log.info(f"    Found: {title} ({source_type}, {len(tables_md)} tables)")
                return url, title, source_type, table_dicts, tables_md
            log.warning(f"    {title}: document accessible but 0 tables — trying next candidate")
            last_error = ValueError("0 tables extracted")
            continue
        except Exception as e:
            log.warning(f"    {title}: {e} — trying next candidate")
            last_error = e
            continue

    raise ValueError(f"All candidate URLs failed (last: {last_error})")


# ── Source quality ranking ────────────────────────────────────────────────

def _source_quality(title):
    """Rate financial source quality from document title.  Higher = more authoritative.

    Audited annual reports have precise figures; investor presentations
    often round numbers or present them in a misleading context.

    Returns:
        int: 0–100 quality score.
    """
    if not title:
        return 0
    t = title.lower()

    # Presentations / marketing materials → always low quality
    if any(kw in t for kw in (
        "presentation", "capital markets day", "investor day", "roadshow", "webcast",
    )):
        return 20

    # Formal audited annual reports → highest quality
    if any(kw in t for kw in (
        "annual report and accounts", "annual report & accounts",
        "annual report", "10-k", "20-f", "statutory accounts",
    )):
        return 100

    # Financial statements, formal filings
    if any(kw in t for kw in ("financial statements", "accounts")):
        return 80

    # Multi-year summaries, company data pages
    if any(kw in t for kw in (
        "five year", "five-year", "5 year", "5-year",
        "financial summary", "financial highlights",
        "key financials", "key facts", "fact sheet", "factsheet",
    )):
        return 60

    # Results announcements (may be preliminary / unaudited)
    if any(kw in t for kw in (
        "results for the year", "preliminary results", "annual results",
        "full year results", "half year results", "interim results",
        "results announcement",
    )):
        return 40

    # Unknown → moderate default
    return 50


def _find_best_evidence_page(entry, filtered_pages, page_texts):
    """Find the original PDF page that best matches an extracted emissions entry.

    Searches page texts for the actual numeric values Claude extracted,
    rather than relying on Claude's reported source_page.
    """
    import re

    def _format_variants(value):
        if value is None or value == 0:
            return []
        variants = []
        # Try the raw number and common representations
        if isinstance(value, float):
            # e.g. 6.7 → ["6.7", "6,7"]
            variants.append(str(value))
            variants.append(str(value).replace(".", ","))
            # If it's a whole number stored as float, add int form
            if value == int(value):
                variants.append(str(int(value)))
        elif isinstance(value, int):
            variants.append(str(value))
            # Add comma-separated thousands: 11600000 → "11,600,000"
            variants.append(f"{value:,}")
        return variants

    s1 = entry.get("scope_1")
    s2l = entry.get("scope_2_location")
    s3 = entry.get("scope_3")
    year = entry.get("reporting_year")

    # Build search terms from the extracted values
    search_values = []
    for val in (s1, s2l, s3):
        search_values.extend(_format_variants(val))

    best_page = filtered_pages[0]
    best_score = 0

    for pg_idx in filtered_pages:
        text = page_texts.get(pg_idx, "")
        if not text:
            continue
        score = 0
        # Year must appear on the page
        if str(year) not in text:
            continue
        score += 1
        # Count how many extracted values appear on this page
        for val_str in search_values:
            if val_str in text:
                score += 2
        # Bonus for scope keywords alongside values
        text_lower = text.lower()
        for kw in ("scope 1", "scope 2", "scope 3"):
            if kw in text_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_page = pg_idx

    return best_page


# ══════════════════════════════════════════════════════════════════════════
# Emissions extraction (iterative)
# ══════════════════════════════════════════════════════════════════════════

def _extract_emissions_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None, events=None,
):
    """Run one search→parse→extract cycle for emissions. Returns count saved.

    Tries ALL high-scoring tables in the document, accumulating unique years
    across them (a trend table, a detailed scope table, and a Scope 3
    breakdown may each contribute different years).
    """
    saved = 0

    search_result = search_for_emissions_source(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
    )
    candidates = search_result.get("candidates", [search_result])
    url, title, source_type, table_dicts, tables_md = _try_parse_candidates(
        candidates, searched_urls, llama_key,
    )

    all_entries = []
    seen_years = set()
    matched_table_idx = None
    target_year_table_idx = None
    best_confidence = 0
    methodology_notes = ""

    # ── PDF path: filter relevant pages, send PDF to Claude ────────────
    if source_type == "pdf":
        try:
            from pipeline.fallback_parser import _EMISSIONS_KEYWORDS
            import pymupdf
            import json as _json

            pdf_path = download_to_tempfile(url)
            pdf_doc = pymupdf.open(pdf_path)

            kw_page_indices = []
            page_texts = {}
            for page_idx in range(len(pdf_doc)):
                text = pdf_doc[page_idx].get_text()
                if text and len(text.strip()) > 100:
                    if any(kw in text.lower() for kw in _EMISSIONS_KEYWORDS):
                        kw_page_indices.append(page_idx)
                        page_texts[page_idx] = text

            if kw_page_indices:
                log.info(f"    PDF extract: {len(kw_page_indices)} emissions-related "
                         f"pages (of {len(pdf_doc)} total)")

                # Build a filtered PDF with only the relevant pages
                filtered_pages = kw_page_indices[:30]
                filtered_doc = pymupdf.open()
                for pg in filtered_pages:
                    filtered_doc.insert_pdf(pdf_doc, from_page=pg, to_page=pg)
                safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
                debug_dir = os.path.join(os.path.dirname(__file__), "..", "debug")
                os.makedirs(debug_dir, exist_ok=True)
                filtered_path = os.path.join(debug_dir, f"{safe_name}_filtered.pdf")
                filtered_doc.save(filtered_path)
                filtered_doc.close()
                log.info(f"    Filtered PDF saved: {filtered_path} "
                         f"({len(filtered_pages)} pages, "
                         f"original indices {filtered_pages})")

                extraction = extract_emissions_from_pdf(
                    filtered_path, company_name, client, model=MODEL_FAST,
                    num_pages=len(filtered_pages),
                )

                # Save debug JSON
                debug_path = os.path.join(debug_dir, f"{safe_name}_pdf.json")
                with open(debug_path, "w") as _df:
                    _json.dump(extraction, _df, indent=2)

                if extraction and extraction.get("emissions"):
                    confidence = extraction.get("confidence_score", 0) or 0

                    if confidence < CONFIDENCE_THRESHOLD:
                        log.info(f"    PDF extract: low confidence ({confidence}), "
                                 "re-extracting with Opus")
                        stronger = extract_emissions_from_pdf(
                            filtered_path, company_name, client, model=MODEL_STRONG,
                            num_pages=len(filtered_pages),
                        )
                        if stronger and stronger.get("emissions"):
                            extraction = stronger
                            confidence = extraction.get("confidence_score", 0) or 0
                            debug_path_strong = os.path.join(
                                debug_dir, f"{safe_name}_pdf_opus.json")
                            with open(debug_path_strong, "w") as _df:
                                _json.dump(extraction, _df, indent=2)

                    best_confidence = confidence
                    methodology_notes = extraction.get("methodology_notes", "")

                    # Render screenshots and build entries
                    screenshots_dir = os.path.join(
                        os.path.dirname(__file__), "..", "screenshots")
                    os.makedirs(screenshots_dir, exist_ok=True)

                    for entry in extraction["emissions"]:
                        year = entry["reporting_year"]
                        s1 = entry.get("scope_1")
                        s2l = entry.get("scope_2_location")
                        s2m = entry.get("scope_2_market")
                        s3 = entry.get("scope_3")
                        log.info(f"    Year {year}: S1={s1} S2L={s2l} "
                                 f"S2M={s2m} S3={s3} "
                                 f"unit={entry.get('unit')} conf={confidence}")

                        # Use Claude's per-scope page numbers to find
                        # the best evidence page in the original PDF
                        best_filtered_pg = (
                            entry.get("scope_1_page")
                            or entry.get("scope_3_page")
                            or entry.get("scope_2_page")
                            or 1
                        )
                        # Claude returns 1-indexed but appears to count from
                        # page 0 of the PDF (off by one). Use the value
                        # directly as the 0-indexed position.
                        pg_idx = max(0, min(best_filtered_pg,
                                           len(filtered_pages) - 1))
                        original_pg = filtered_pages[pg_idx]
                        log.info(f"    Year {year}: Claude says page "
                                 f"{best_filtered_pg} of filtered PDF → "
                                 f"original page {original_pg}")

                        if year not in seen_years:
                            img_path = os.path.join(
                                screenshots_dir,
                                f"{safe_name}_p{original_pg}.png")
                            render_pdf_page(pdf_path, original_pg, img_path)
                            log.info(f"    Screenshot for {year}: "
                                     f"original page {original_pg}")

                            table_dicts.append({
                                "markdown": "(pdf extraction)",
                                "page_index": original_pg,
                                "screenshot_path": img_path,
                            })
                            tbl_idx = len(table_dicts) - 1
                            entry["_table_idx"] = tbl_idx
                            all_entries.append(entry)
                            seen_years.add(year)
                            if matched_table_idx is None:
                                matched_table_idx = tbl_idx
                            if year == target_year:
                                target_year_table_idx = tbl_idx

                    log.info(f"    PDF extract: {len(seen_years)} year(s) "
                             f"found {sorted(seen_years)}")


            pdf_doc.close()
            try:
                os.unlink(pdf_path)
            except OSError:
                pass
        except Exception as e:
            log.warning(f"    PDF extraction failed: {e}")

    # ── HTML path: table ranking + text fallback ───────────────────────
    if not all_entries and source_type == "html":
        if tables_md:
            ranked = find_emissions_tables(tables_md, client)
            top_tables = [r for r in ranked if r["score"] >= 30]
            for candidate_tbl in (top_tables or [])[:10]:
                try:
                    extraction = extract_emissions(
                        tables_md[candidate_tbl["index"]], company_name, client,
                        model=MODEL_FAST,
                    )
                    if extraction.get("emissions"):
                        confidence = extraction.get("confidence_score", 0) or 0
                        if confidence < CONFIDENCE_THRESHOLD:
                            stronger = extract_emissions(
                                tables_md[candidate_tbl["index"]], company_name, client,
                                model=MODEL_STRONG,
                            )
                            if stronger.get("emissions"):
                                extraction = stronger
                                confidence = extraction.get("confidence_score", 0) or 0
                        if matched_table_idx is None:
                            matched_table_idx = candidate_tbl["index"]
                            methodology_notes = extraction.get("methodology_notes", "")
                        if confidence > best_confidence:
                            best_confidence = confidence
                        for entry in extraction["emissions"]:
                            year = entry["reporting_year"]
                            if year not in seen_years:
                                entry["_table_idx"] = candidate_tbl["index"]
                                all_entries.append(entry)
                                seen_years.add(year)
                                if year == target_year:
                                    target_year_table_idx = candidate_tbl["index"]
                except Exception as e:
                    log.warning(f"    Extraction failed for table "
                                f"{candidate_tbl['index']}: {e}")

        if not all_entries:
            log.info("    No table results, trying text fallback")
            page_text = extract_html_text(url)
            extraction = extract_emissions_from_text(page_text, company_name, client,
                                                      model=MODEL_FAST)
            if extraction and extraction.get("emissions"):
                confidence = extraction.get("confidence_score", 0) or 0
                if confidence < CONFIDENCE_THRESHOLD:
                    stronger = extract_emissions_from_text(page_text, company_name, client,
                                                           model=MODEL_STRONG)
                    if stronger.get("emissions"):
                        extraction = stronger
                all_entries = extraction["emissions"]
                best_confidence = extraction.get("confidence_score", 0) or 0
                methodology_notes = extraction.get("methodology_notes", "")

    if all_entries:
        # Use the table that contained the target year for the screenshot,
        # falling back to the first table that had any data
        screenshot_idx = target_year_table_idx or matched_table_idx
        screenshot_path, html_snippet, page_number, s3_pdf_key = (
            _capture_source_preview(url, source_type, table_dicts, screenshot_idx, company_name)
        )

        source = Source(
            company_id=company.id, url=url, title=title,
            document_type=source_type, s3_pdf_key=s3_pdf_key,
            screenshot_path=screenshot_path, html_snippet=html_snippet,
            page_number=page_number,
        )
        session.add(source)
        session.flush()

        for entry in all_entries:
            year = entry["reporting_year"]
            if year < TARGET_START_YEAR:
                continue

            # Is this the year we explicitly searched for?
            is_targeted = (target_year is not None and year == target_year)

            existing_record = (
                session.query(EmissionsRecord)
                .filter_by(company_id=company.id, reporting_year=year)
                .first()
            ) if year in covered_years else None

            if existing_record:
                # Check if values differ from a different source
                values_differ = any(
                    getattr(existing_record, f) is not None
                    and entry.get(f) is not None
                    and getattr(existing_record, f) != entry.get(f)
                    for f in ("scope_1", "scope_2_location", "scope_2_market", "scope_3")
                )
                different_source = existing_record.source_id != source.id

                if values_differ and different_source:
                    # Different source reports different values — keep both
                    # (likely a restatement in a later report)
                    restated = EmissionsRecord(
                        company_id=company.id,
                        reporting_year=year,
                        period_start=_parse_date(entry.get("period_start")),
                        period_end=_parse_date(entry.get("period_end")),
                        scope_1=entry.get("scope_1"),
                        scope_2_location=entry.get("scope_2_location"),
                        scope_2_market=entry.get("scope_2_market"),
                        scope_3=entry.get("scope_3"),
                        scope_3_categories=entry.get("scope_3_categories"),
                        unit=entry.get("unit", "tonnes CO2e"),
                        boundary=entry.get("boundary"),
                        methodology_notes=methodology_notes,
                        source_id=source.id,
                        confidence_score=best_confidence,
                        is_restated=True,
                        review_status="pending",
                    )
                    session.add(restated)
                    saved += 1
                    log.info(f"    Year {year}: RESTATED — kept both values "
                             f"(old source #{existing_record.source_id}, "
                             f"new source #{source.id})")
                    continue

                # Same source or compatible values — fill gaps
                updated = False
                for field in ("scope_1", "scope_2_location", "scope_2_market", "scope_3"):
                    old_val = getattr(existing_record, field)
                    new_val = entry.get(field)
                    if old_val is None and new_val is not None:
                        setattr(existing_record, field, new_val)
                        updated = True
                    elif is_targeted and new_val is not None and old_val != new_val:
                        setattr(existing_record, field, new_val)
                        updated = True
                if is_targeted or existing_record.scope_3_categories is None:
                    if entry.get("scope_3_categories"):
                        existing_record.scope_3_categories = entry["scope_3_categories"]
                if is_targeted or existing_record.boundary is None:
                    if entry.get("boundary"):
                        existing_record.boundary = entry["boundary"]
                if is_targeted or existing_record.period_start is None:
                    existing_record.period_start = _parse_date(entry.get("period_start"))
                if is_targeted or existing_record.period_end is None:
                    existing_record.period_end = _parse_date(entry.get("period_end"))
                if is_targeted:
                    existing_record.source_id = source.id
                    existing_record.confidence_score = best_confidence
                    existing_record.methodology_notes = methodology_notes
                if updated:
                    action = "REPLACED (targeted)" if is_targeted else "filled gaps"
                    log.info(f"    Updated year {year}: {action}")
                    saved += 1
                continue

            record = EmissionsRecord(
                company_id=company.id,
                reporting_year=year,
                period_start=_parse_date(entry.get("period_start")),
                period_end=_parse_date(entry.get("period_end")),
                scope_1=entry.get("scope_1"),
                scope_2_location=entry.get("scope_2_location"),
                scope_2_market=entry.get("scope_2_market"),
                scope_3=entry.get("scope_3"),
                scope_3_categories=entry.get("scope_3_categories"),
                unit=entry.get("unit", "tonnes CO2e"),
                boundary=entry.get("boundary"),
                methodology_notes=methodology_notes,
                source_id=source.id,
                confidence_score=best_confidence,
                review_status="pending",
            )
            session.add(record)
            covered_years.add(year)
            saved += 1

        session.commit()

        if events is not None:
            source_evt = {"type": "source", "url": url, "title": title, "years": set()}
            for entry in all_entries:
                yr = entry["reporting_year"]
                if yr >= TARGET_START_YEAR:
                    source_evt["years"].add(yr)
                    events.append({
                        "type": "emissions", "year": yr,
                        "scope_1": entry.get("scope_1") is not None,
                        "scope_2": entry.get("scope_2_location") is not None
                                   or entry.get("scope_2_market") is not None,
                        "scope_3": entry.get("scope_3") is not None,
                        "years": {yr},
                    })
            events.append(source_evt)

    return saved


# ══════════════════════════════════════════════════════════════════════════
# Financial extraction (shared helper + iterative round)
# ══════════════════════════════════════════════════════════════════════════

def _extract_financials_from_document(
    url, title, source_type, table_dicts, tables_md,
    company, company_name, client, session, covered_years, events=None,
):
    """Extract financial data from ALL high-scoring tables in a parsed document.

    Tries up to 5 tables and merges results by year — revenue may come from
    the income statement while debt/cash come from the balance sheet.
    Returns count of new records saved.
    """
    if not tables_md:
        return 0

    ranked = find_financial_tables(tables_md, client)
    top = [r for r in ranked if r["score"] >= 30]
    if not top:
        return 0

    entries_by_year = {}  # year -> merged entry dict
    matched_table_idx = None
    best_confidence = 0

    for candidate_tbl in top[:10]:
        try:
            fin_extraction = extract_financials(
                tables_md[candidate_tbl["index"]], company_name, client,
                model=MODEL_FAST,
            )
            if fin_extraction.get("financials"):
                confidence = fin_extraction.get("confidence_score", 0) or 0

                # Re-extract with stronger model if confidence is low
                if confidence < CONFIDENCE_THRESHOLD:
                    log.info(f"    Table {candidate_tbl['index']}: "
                             f"low confidence ({confidence}), re-extracting with Opus")
                    stronger = extract_financials(
                        tables_md[candidate_tbl["index"]], company_name, client,
                        model=MODEL_STRONG,
                    )
                    if stronger.get("financials"):
                        fin_extraction = stronger
                        confidence = fin_extraction.get("confidence_score", 0) or 0

                if matched_table_idx is None:
                    matched_table_idx = candidate_tbl["index"]

                if confidence > best_confidence:
                    best_confidence = confidence

                new_this_table = 0
                for entry in fin_extraction["financials"]:
                    year = entry["reporting_year"]
                    if year in entries_by_year:
                        # Merge: fill in null fields from existing entry
                        existing = entries_by_year[year]
                        for key, val in entry.items():
                            if key == "reporting_year":
                                continue
                            if existing.get(key) is None and val is not None:
                                existing[key] = val
                    else:
                        entries_by_year[year] = dict(entry)
                        new_this_table += 1

                log.info(f"    Table {candidate_tbl['index']}: "
                         f"{new_this_table} new year(s), "
                         f"total so far {sorted(entries_by_year.keys())}")
        except Exception as e:
            log.warning(f"    Financial extraction failed for table {candidate_tbl['index']}: {e}")

    if not entries_by_year:
        return 0

    # Capture source preview
    screenshot_path, html_snippet, page_number, s3_pdf_key = (
        _capture_source_preview(url, source_type, table_dicts, matched_table_idx, company_name)
    )

    fin_source = Source(
        company_id=company.id, url=url, title=title,
        document_type=source_type, s3_pdf_key=s3_pdf_key,
        screenshot_path=screenshot_path, html_snippet=html_snippet,
        page_number=page_number,
    )
    session.add(fin_source)
    session.flush()

    # Source quality: higher-quality sources (annual reports) can overwrite
    # values from lower-quality sources (investor presentations).
    new_quality = _source_quality(title)
    log.info(f"    Source quality: '{title}' → q={new_quality}")

    saved = 0
    for entry in entries_by_year.values():
        year = entry["reporting_year"]
        if year < TARGET_START_YEAR:
            continue

        multiplier = entry.get("unit_multiplier", 1) or 1
        new_revenue = normalise_to_units(entry.get("revenue"), multiplier)
        new_debt = normalise_to_units(entry.get("outstanding_debt"), multiplier)
        new_cash = normalise_to_units(entry.get("cash_and_equivalents"), multiplier)

        # Upsert: if a record exists, fill in any null key fields.
        # Revenue comes from income statements, debt/cash from balance sheets —
        # these may arrive in different search rounds.
        # If the new source is higher quality (e.g. annual report vs investor
        # presentation), overwrite existing values too.
        existing_record = (
            session.query(FinancialRecord)
            .filter_by(company_id=company.id, reporting_year=year)
            .first()
        )

        if existing_record:
            old_source = (
                session.query(Source).get(existing_record.source_id)
                if existing_record.source_id else None
            )
            old_quality = _source_quality(old_source.title if old_source else "")
            upgrade = new_quality > old_quality

            updated = False
            for attr, new_val in [
                ("revenue", new_revenue),
                ("outstanding_debt", new_debt),
                ("cash_and_equivalents", new_cash),
            ]:
                old_val = getattr(existing_record, attr)
                if old_val is None and new_val is not None:
                    # Fill missing field (any source)
                    setattr(existing_record, attr, new_val)
                    updated = True
                elif upgrade and new_val is not None and old_val != new_val:
                    # Overwrite with higher-quality source
                    setattr(existing_record, attr, new_val)
                    updated = True

            # Fill other null metadata regardless of source quality
            if existing_record.currency is None and entry.get("currency"):
                existing_record.currency = entry["currency"]
            if existing_record.fiscal_year_end is None:
                existing_record.fiscal_year_end = _parse_date(entry.get("fiscal_year_end"))
            if existing_record.period_start is None:
                existing_record.period_start = _parse_date(entry.get("period_start"))
            if existing_record.period_end is None:
                existing_record.period_end = _parse_date(entry.get("period_end"))

            if updated:
                if upgrade:
                    existing_record.source_id = fin_source.id
                    existing_record.confidence_score = best_confidence
                    old_title = old_source.title if old_source else "unknown"
                    log.info(f"    Year {year}: UPGRADED source "
                             f"'{old_title}' (q={old_quality}) → "
                             f"'{title}' (q={new_quality})")
                else:
                    log.info(f"    Updated year {year}: filled in missing financial fields")
                saved += 1

            covered_years.add(year)
            continue

        fin_record = FinancialRecord(
            company_id=company.id,
            reporting_year=year,
            fiscal_year_end=_parse_date(entry.get("fiscal_year_end")),
            period_start=_parse_date(entry.get("period_start")),
            period_end=_parse_date(entry.get("period_end")),
            revenue=new_revenue,
            outstanding_debt=new_debt,
            cash_and_equivalents=new_cash,
            currency=entry.get("currency"),
            source_id=fin_source.id,
            confidence_score=best_confidence,
            review_status="pending",
        )
        session.add(fin_record)
        covered_years.add(year)
        saved += 1

    session.commit()

    if events is not None and entries_by_year:
        years_saved = {e["reporting_year"] for e in entries_by_year.values()
                       if e["reporting_year"] >= TARGET_START_YEAR}
        events.append({"type": "source", "url": url, "title": title, "years": years_saved})
        events.append({"type": "financial", "years": years_saved})

    return saved


def _extract_financials_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None, events=None,
):
    """Run one search→parse→extract cycle for financials. Returns count saved."""
    fin_search = search_for_annual_report(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
    )
    candidates = fin_search.get("candidates", [fin_search])
    url, title, source_type, table_dicts, tables_md = _try_parse_candidates(
        candidates, searched_urls, llama_key,
    )
    return _extract_financials_from_document(
        url, title, source_type, table_dicts, tables_md,
        company, company_name, client, session, covered_years,
        events=events,
    )


# ── Run summary ──────────────────────────────────────────────────────────

def _print_company_summary(company_name, events, cost, elapsed_s):
    """Print a human-readable summary of what happened during processing."""
    lines = [
        "",
        "═" * 70,
        f"  SUMMARY: {company_name}",
        "═" * 70,
    ]

    # Sources used
    sources = [e for e in events if e["type"] == "source"]
    if sources:
        lines.append("  Sources:")
        for s in sources:
            lines.append(f"    • {s['title']}")
            lines.append(f"      {s['url'][:80]}{'…' if len(s['url']) > 80 else ''}")
            if s.get("years"):
                lines.append(f"      → extracted years: {sorted(s['years'])}")

    # Emissions coverage
    em_events = [e for e in events if e["type"] == "emissions"]
    if em_events:
        all_years = set()
        for e in em_events:
            all_years.update(e.get("years", []))
        lines.append(f"  Emissions: {len(all_years)} year(s) — {sorted(all_years)}")
        for e in em_events:
            scopes = []
            for s in ("scope_1", "scope_2", "scope_3"):
                if e.get(s):
                    scopes.append(s.replace("_", " ").title())
            if scopes:
                lines.append(f"    {e['year']}: {', '.join(scopes)}")

    # Financial coverage
    fin_events = [e for e in events if e["type"] == "financial"]
    if fin_events:
        all_years = set()
        for e in fin_events:
            all_years.update(e.get("years", []))
        lines.append(f"  Financials: {len(all_years)} year(s) — {sorted(all_years)}")

    # Gaps
    gap_events = [e for e in events if e["type"] == "gap"]
    for g in gap_events:
        lines.append(f"  ⚠ Missing {g['data_type']}: {sorted(g['years'])}")

    # Errors
    err_events = [e for e in events if e["type"] == "error"]
    if err_events:
        lines.append(f"  Errors ({len(err_events)}):")
        for e in err_events:
            lines.append(f"    ✗ {e['message'][:100]}")

    # Cost
    lines.append(f"  Cost: ${cost['total']:.4f} "
                 f"({cost['calls']} API calls, "
                 f"{cost['input_tokens']:,} in / {cost['output_tokens']:,} out)")
    lines.append(f"  Time: {elapsed_s:.0f}s")
    lines.append("═" * 70)
    lines.append("")

    for line in lines:
        log.info(line)


def _print_pipeline_summary(results, total_cost, total_elapsed_s):
    """Print an overall pipeline summary across all companies."""
    lines = [
        "",
        "╔" + "═" * 68 + "╗",
        "║" + "  PIPELINE RUN COMPLETE".center(68) + "║",
        "╚" + "═" * 68 + "╝",
        "",
    ]

    n_success = sum(1 for r in results if r["status"] == "success")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")
    n_failed = sum(1 for r in results if r["status"] == "failed")

    lines.append(f"  Companies: {len(results)} total — "
                 f"{n_success} processed, {n_skipped} skipped, {n_failed} failed")

    total_em = sum(r.get("emissions_records", 0) for r in results)
    total_fin = sum(r.get("financial_records", 0) for r in results)
    lines.append(f"  Records saved: {total_em} emissions, {total_fin} financial")

    # Per-company one-liners
    if len(results) > 1:
        lines.append("")
        for r in results:
            em = r.get("emissions_records", 0)
            fin = r.get("financial_records", 0)
            status_icon = {"success": "✓", "skipped": "–", "failed": "✗"}.get(r["status"], "?")
            cost_str = f"${r.get('cost_detail', {}).get('total', 0):.4f}"
            lines.append(f"  {status_icon} {r['company']:30s}  "
                         f"em={em:2d}  fin={fin:2d}  {cost_str}")

    lines.append("")
    lines.append(f"  Total cost:  ${total_cost['total']:.4f}")
    lines.append(f"  Total time:  {total_elapsed_s / 60:.1f} min")
    lines.append(f"  API calls:   {total_cost['calls']}")
    lines.append(f"  Tokens:      {total_cost['input_tokens']:,} in / "
                 f"{total_cost['output_tokens']:,} out")
    lines.append("")

    for line in lines:
        log.info(line)


# ══════════════════════════════════════════════════════════════════════════
# Main per-company pipeline
# ══════════════════════════════════════════════════════════════════════════

def process_company(
    company: Company,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    session,
) -> dict:
    """Process a single company: walk backwards from TARGET_END_YEAR to fill gaps.

    Pipeline:
    1. Walk backwards through years, searching sustainability reports for emissions
    2. Walk backwards through years, searching annual reports for financials
    3. yfinance → equity value at fiscal year-end for all financial records
    4. Claude → industry classification (NAICS/NACE/SIC)

    Returns a dict with status and details.
    """
    cost = _new_cost_tracker()
    raw_client = anthropic.Anthropic(api_key=anthropic_key)
    client = _TrackedClient(raw_client, cost)
    company_name = company.name
    events = []
    t_start = time.time()
    log.info(f"Processing: {company_name}")

    target = _target_years()
    has_industry = company.yfinance_sector is not None

    # ── PART 1: Emissions (backwards walk) ─────────────────────────────────

    em_covered = _get_covered_years(session, company.id, EmissionsRecord)
    em_pre_existing = set(em_covered)  # years in DB before this run
    em_searched_years = set()          # years explicitly targeted during this run
    em_searched_urls = set()
    total_em_saved = 0
    em_missing = target - em_pre_existing

    if em_missing:
        log.info(f"  Emissions: have {sorted(em_pre_existing) or 'none'}, "
                 f"missing {sorted(em_missing)}")

        search_count = 0
        consecutive_empty = 0
        # Walk backwards: newest year first, then year-1, year-2, ...
        walk_year = TARGET_END_YEAR
        while walk_year >= TARGET_START_YEAR and search_count < MAX_SEARCHES:
            # Skip years that existed before this run OR were explicitly searched
            if walk_year in em_pre_existing or walk_year in em_searched_years:
                log.info(f"  Year {walk_year}: already covered, skipping")
                walk_year -= 1
                continue

            search_count += 1
            em_searched_years.add(walk_year)
            log.info(f"  Emissions search {search_count}/{MAX_SEARCHES} "
                     f"(year {walk_year})")
            try:
                saved = _extract_emissions_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, em_covered, em_searched_urls, target_year=walk_year,
                    events=events,
                )
                total_em_saved += saved
                if saved == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        log.info("    Three consecutive empty searches, stopping")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                log.warning(f"    Emissions search failed: {e}")
                events.append({"type": "error", "message": f"Emissions {walk_year}: {e}"})
                session.rollback()
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break

            walk_year -= 1

        em_missing = target - em_covered
        if total_em_saved:
            log.info(f"  Emissions: saved {total_em_saved} records "
                     f"(covering {sorted(em_covered & target)})")
        if em_missing:
            log.info(f"  Emissions: no data found for years {sorted(em_missing)}")
            events.append({"type": "gap", "data_type": "emissions", "years": em_missing})

    # ── PART 2: Financials (backwards walk) ─────────────────────────────────

    fin_covered = _get_covered_years(session, company.id, FinancialRecord)
    fin_missing = _get_financial_needs(session, company.id, target)
    fin_searched_urls = set()
    total_fin_saved = 0

    if fin_missing:
        log.info(f"  Financials: have {sorted(fin_covered) or 'none'}, "
                 f"need data for {sorted(fin_missing)}")

        # Search 1: try a five-year financial summary first (covers most years in one hit)
        log.info(f"  Financial search 1 (five-year summary)")
        try:
            fin_history_search = search_for_financial_history(
                company_name, anthropic_key, exa_key,
                exclude_urls=list(fin_searched_urls),
            )
            candidates = fin_history_search.get("candidates", [fin_history_search])
            url, title, source_type, table_dicts, tables_md = _try_parse_candidates(
                candidates, fin_searched_urls, llama_key,
            )
            saved = _extract_financials_from_document(
                url, title, source_type, table_dicts, tables_md,
                company, company_name, client, session, fin_covered,
                events=events,
            )
            total_fin_saved += saved
            fin_missing = _get_financial_needs(session, company.id, target)
        except Exception as e:
            log.warning(f"    Five-year summary search failed: {e}")
            events.append({"type": "error", "message": f"Financial summary: {e}"})
            session.rollback()

        # Walk backwards through remaining years
        search_count = 1  # already used one search for five-year summary
        consecutive_empty = 0
        walk_year = TARGET_END_YEAR
        while walk_year >= TARGET_START_YEAR and search_count < MAX_SEARCHES:
            fin_missing = _get_financial_needs(session, company.id, target)
            if walk_year not in fin_missing:
                walk_year -= 1
                continue

            search_count += 1
            log.info(f"  Financial search {search_count}/{MAX_SEARCHES} "
                     f"(year {walk_year})")
            try:
                saved = _extract_financials_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, fin_covered, fin_searched_urls, target_year=walk_year,
                    events=events,
                )
                total_fin_saved += saved
                if saved == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 3:
                        log.info("    Three consecutive empty searches, stopping")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                log.warning(f"    Financial search failed: {e}")
                events.append({"type": "error", "message": f"Financial {walk_year}: {e}"})
                session.rollback()
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break

            walk_year -= 1

        if total_fin_saved:
            fin_covered = _get_covered_years(session, company.id, FinancialRecord)
            log.info(f"  Financials: saved/updated {total_fin_saved} records "
                     f"(covering {sorted(fin_covered & target)})")
        still_missing = _get_financial_needs(session, company.id, target)
        if still_missing:
            log.info(f"  Financials: still incomplete for years {sorted(still_missing)}")
            events.append({"type": "gap", "data_type": "financials", "years": still_missing})

    # ── PART 3: Market data from yfinance ─────────────────────────────────

    if company.ticker:
        fin_records = (
            session.query(FinancialRecord)
            .filter_by(company_id=company.id)
            .filter(FinancialRecord.equity_value.is_(None))
            .all()
        )
        if fin_records:
            log.info(f"  Fetching market data for {len(fin_records)} financial records...")
            try:
                for fr in fin_records:
                    target_date = fr.fiscal_year_end or date_type(fr.reporting_year, 12, 31)
                    equity_data = get_equity_value_at_date(company.ticker, target_date)
                    if equity_data:
                        fr.equity_value = equity_data["market_cap"]
                        fr.shares_outstanding = equity_data["shares_outstanding"]
                        fr.share_price_at_fy_end = equity_data["share_price"]
                        fr.equity_currency = equity_data["currency"]
                        if fr.outstanding_debt is not None and fr.cash_and_equivalents is not None:
                            fr.enterprise_value = (
                                fr.equity_value + fr.outstanding_debt - fr.cash_and_equivalents
                            )
                        log.info(f"    {fr.reporting_year}: equity={fr.equity_value:,.0f} "
                                 f"{fr.equity_currency}")
                session.commit()
            except Exception as e:
                log.warning(f"  Market data fetch failed: {e}")
                session.rollback()

    # ── PART 4: Industry classification ───────────────────────────────────

    if not has_industry and company.ticker:
        log.info("  Looking up industry classification...")
        try:
            industry_info = get_industry_info(company.ticker)
            if industry_info:
                company.yfinance_sector = industry_info["sector"]
                company.yfinance_industry = industry_info["industry"]
                log.info(f"  Industry: {industry_info['sector']} / {industry_info['industry']}")

                classification = classify_company(
                    company_name,
                    industry_info["sector"],
                    industry_info["industry"],
                    client,
                )
                company.sic_code = classification.get("sic_code")
                company.sic_description = classification.get("sic_description")
                company.naics_code = classification.get("naics_code")
                company.naics_description = classification.get("naics_description")
                company.nace_code = classification.get("nace_code")
                company.nace_description = classification.get("nace_description")
                company.industry_review_status = (
                    "approved" if classification.get("confidence") == "high" else "pending"
                )
                session.commit()
                log.info(f"  Classified: SIC={company.sic_code}, "
                         f"NAICS={company.naics_code}, NACE={company.nace_code}")
        except Exception as e:
            log.warning(f"  Industry classification failed: {e}")

    # ── Result ────────────────────────────────────────────────────────────

    elapsed = time.time() - t_start
    _print_company_summary(company_name, events, cost, elapsed)

    if total_em_saved == 0 and total_fin_saved == 0 and not em_missing and not fin_missing:
        return {"status": "skipped", "company": company_name, "cost_detail": cost}

    return {
        "status": "success",
        "company": company_name,
        "emissions_records": total_em_saved,
        "financial_records": total_fin_saved,
        "cost_detail": cost,
    }


# ══════════════════════════════════════════════════════════════════════════
# Pipeline orchestration
# ══════════════════════════════════════════════════════════════════════════

def run_pipeline(
    database_url: str,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    company_ids: list[int] = None,
    delay_between: float = 2.0,
):
    """Run the full pipeline across all (or specified) companies."""
    session = get_session(database_url)

    if company_ids:
        companies = session.query(Company).filter(Company.id.in_(company_ids)).all()
    else:
        companies = session.query(Company).all()

    log.info(f"Starting pipeline for {len(companies)} companies "
             f"(target years: {TARGET_START_YEAR}–{TARGET_END_YEAR})")

    run = PipelineRun(
        total_companies=len(companies),
        status="running",
    )
    session.add(run)
    session.commit()

    errors = []
    results = []
    total_cost = _new_cost_tracker()
    t_pipeline_start = time.time()

    for i, company in enumerate(companies):
        log.info(f"[{i + 1}/{len(companies)}] {company.name}")
        try:
            result = process_company(company, anthropic_key, exa_key, llama_key, session)
            results.append(result)
            cd = result.get("cost_detail", {})
            for k in ("total", "calls", "input_tokens", "output_tokens"):
                total_cost[k] += cd.get(k, 0)
        except Exception as e:
            log.error(f"  FAILED: {e}")
            errors.append(f"{company.name}: {e}")
            results.append({"status": "failed", "company": company.name, "cost": 0})
            session.rollback()

        if i < len(companies) - 1:
            time.sleep(delay_between)

        n_success = sum(1 for r in results if r["status"] == "success")
        n_failed = sum(1 for r in results if r["status"] == "failed")
        n_skipped = sum(1 for r in results if r["status"] == "skipped")

        if (i + 1) % 10 == 0:
            run.successful = n_success
            run.failed = n_failed
            run.skipped = n_skipped
            session.commit()

    n_success = sum(1 for r in results if r["status"] == "success")
    n_failed = sum(1 for r in results if r["status"] == "failed")
    n_skipped = sum(1 for r in results if r["status"] == "skipped")

    run.successful = n_success
    run.failed = n_failed
    run.skipped = n_skipped
    run.completed_at = datetime.utcnow()
    run.status = "completed"
    run.error_log = "\n".join(errors) if errors else None
    session.commit()

    _print_pipeline_summary(results, total_cost, time.time() - t_pipeline_start)

    return {
        "successful": n_success,
        "failed": n_failed,
        "skipped": n_skipped,
    }
