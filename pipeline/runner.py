"""
Main pipeline runner — processes companies one by one, extracting emissions,
financial data, market data, and industry classifications.

Searches iteratively for reports covering 2019 to present, filling gaps
with year-targeted queries (up to MAX_SEARCHES per document type).
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
from pipeline.extractor import find_emissions_tables, extract_emissions, extract_emissions_from_text
from pipeline.financial_extractor import find_financial_tables, extract_financials, normalise_to_units
from pipeline.market_data import get_equity_value_at_date, get_industry_info
from pipeline.industry_classifier import classify_company
from pipeline.storage import upload_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────
TARGET_START_YEAR = 2019
MAX_SEARCHES_PER_TYPE = 5  # max Exa searches per document type per company
CONFIDENCE_THRESHOLD = 70  # below this, re-extract with a stronger model
MODEL_FAST = "claude-haiku-4-5-20251001"  # bulk extraction (cheap)
MODEL_STRONG = "claude-opus-4-6"          # re-extraction for low-confidence results


def _current_year():
    return datetime.utcnow().year


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
        try:
            pdf_local_path = download_to_tempfile(url)
            if page_number is not None:
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
    return set(range(TARGET_START_YEAR, _current_year() + 1))


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
            log.info(f"    Found: {title} ({source_type})")
            return url, title, source_type, table_dicts, tables_md
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


# ══════════════════════════════════════════════════════════════════════════
# Emissions extraction (iterative)
# ══════════════════════════════════════════════════════════════════════════

def _extract_emissions_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None,
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

    # Accumulate emissions from ALL high-scoring tables
    all_entries = []
    seen_years = set()
    matched_table_idx = None
    best_confidence = 0
    methodology_notes = ""

    if tables_md:
        ranked = find_emissions_tables(tables_md, client)
        top_tables = [r for r in ranked if r["score"] >= 30]
        if top_tables:
            for candidate_tbl in top_tables[:10]:
                try:
                    extraction = extract_emissions(
                        tables_md[candidate_tbl["index"]], company_name, client,
                        model=MODEL_FAST,
                    )
                    if extraction.get("emissions"):
                        confidence = extraction.get("confidence_score", 0) or 0

                        # Re-extract with stronger model if confidence is low
                        if confidence < CONFIDENCE_THRESHOLD:
                            log.info(f"    Table {candidate_tbl['index']}: "
                                     f"low confidence ({confidence}), re-extracting with Opus")
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

                        new_this_table = 0
                        for entry in extraction["emissions"]:
                            year = entry["reporting_year"]
                            if year not in seen_years:
                                all_entries.append(entry)
                                seen_years.add(year)
                                new_this_table += 1

                        log.info(f"    Table {candidate_tbl['index']}: "
                                 f"{new_this_table} new year(s), "
                                 f"total so far {sorted(seen_years)}")
                except Exception as e:
                    log.warning(f"    Extraction failed for table {candidate_tbl['index']}: {e}")

    # Fallback: extract from page text
    if not all_entries and source_type == "html":
        log.info("    No table results, trying text fallback")
        page_text = extract_html_text(url)
        extraction = extract_emissions_from_text(page_text, company_name, client,
                                                  model=MODEL_FAST)
        if extraction and extraction.get("emissions"):
            confidence = extraction.get("confidence_score", 0) or 0
            if confidence < CONFIDENCE_THRESHOLD:
                log.info(f"    Low confidence ({confidence}), re-extracting with Opus")
                stronger = extract_emissions_from_text(page_text, company_name, client,
                                                       model=MODEL_STRONG)
                if stronger.get("emissions"):
                    extraction = stronger
            all_entries = extraction["emissions"]
            best_confidence = extraction.get("confidence_score", 0) or 0
            methodology_notes = extraction.get("methodology_notes", "")

    if all_entries:
        # Capture source preview
        screenshot_path, html_snippet, page_number, s3_pdf_key = (
            _capture_source_preview(url, source_type, table_dicts, matched_table_idx, company_name)
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

            # Upsert: if a record exists, fill in any null scope fields
            existing_record = (
                session.query(EmissionsRecord)
                .filter_by(company_id=company.id, reporting_year=year)
                .first()
            ) if year in covered_years else None

            if existing_record:
                updated = False
                for field in ("scope_1", "scope_2_location", "scope_2_market", "scope_3"):
                    if getattr(existing_record, field) is None and entry.get(field) is not None:
                        setattr(existing_record, field, entry[field])
                        updated = True
                if existing_record.scope_3_categories is None and entry.get("scope_3_categories"):
                    existing_record.scope_3_categories = entry["scope_3_categories"]
                if existing_record.boundary is None and entry.get("boundary"):
                    existing_record.boundary = entry["boundary"]
                if existing_record.period_start is None:
                    existing_record.period_start = _parse_date(entry.get("period_start"))
                if existing_record.period_end is None:
                    existing_record.period_end = _parse_date(entry.get("period_end"))
                if updated:
                    log.info(f"    Updated year {year}: filled in missing emissions fields")
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

    return saved


# ══════════════════════════════════════════════════════════════════════════
# Financial extraction (shared helper + iterative round)
# ══════════════════════════════════════════════════════════════════════════

def _extract_financials_from_document(
    url, title, source_type, table_dicts, tables_md,
    company, company_name, client, session, covered_years,
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
    return saved


def _extract_financials_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None,
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
    )


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
    """Process a single company: iteratively fill 2019–present data.

    Pipeline:
    1. Iteratively search sustainability reports → extract emissions for all years
    2. Iteratively search annual reports → extract financials for all years
    3. yfinance → equity value at fiscal year-end for all financial records
    4. Claude → industry classification (NAICS/NACE/SIC)

    Returns a dict with status and details.
    """
    client = anthropic.Anthropic(api_key=anthropic_key)
    company_name = company.name
    log.info(f"Processing: {company_name}")

    target = _target_years()
    has_industry = company.yfinance_sector is not None

    # ── PART 1: Emissions (iterative) ─────────────────────────────────────

    em_covered = _get_covered_years(session, company.id, EmissionsRecord)
    em_missing = target - em_covered
    em_searched_urls = set()
    total_em_saved = 0

    if em_missing:
        log.info(f"  Emissions: have {sorted(em_covered) or 'none'}, "
                 f"missing {sorted(em_missing)}")

        search_count = 0
        consecutive_empty = 0
        while em_missing and search_count < MAX_SEARCHES_PER_TYPE:
            # First search: broad (latest).
            # Subsequent: alternate oldest / newest missing year to attack gaps
            # from both ends.
            if search_count == 0:
                target_year = None
                year_label = " (latest)"
            elif search_count % 2 == 1:
                target_year = min(em_missing)
                year_label = f" (oldest missing: {target_year})"
            else:
                target_year = max(em_missing)
                year_label = f" (newest missing: {target_year})"

            log.info(f"  Emissions search {search_count + 1}/{MAX_SEARCHES_PER_TYPE}{year_label}")
            try:
                saved = _extract_emissions_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, em_covered, em_searched_urls, target_year=target_year,
                )
                total_em_saved += saved
                em_missing = target - em_covered
                if saved == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        log.info("    Two consecutive empty searches, stopping")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                log.warning(f"    Emissions search failed: {e}")
                session.rollback()
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break

            search_count += 1

        if total_em_saved:
            log.info(f"  Emissions: saved {total_em_saved} records "
                     f"(covering {sorted(em_covered & target)})")
        still_missing = target - em_covered
        if still_missing:
            log.info(f"  Emissions: no data found for years {sorted(still_missing)}")

    # ── PART 2: Financials (iterative) ────────────────────────────────────

    fin_covered = _get_covered_years(session, company.id, FinancialRecord)
    fin_missing = _get_financial_needs(session, company.id, target)
    fin_searched_urls = set()
    total_fin_saved = 0

    if fin_missing:
        log.info(f"  Financials: have {sorted(fin_covered) or 'none'}, "
                 f"need data for {sorted(fin_missing)}")

        # Search 1: try a five-year financial summary first (covers most years in one hit)
        log.info(f"  Financial search 1/{MAX_SEARCHES_PER_TYPE + 1} (five-year summary)")
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
            )
            total_fin_saved += saved
            fin_missing = _get_financial_needs(session, company.id, target)
        except Exception as e:
            log.warning(f"    Five-year summary search failed: {e}")
            session.rollback()

        # Searches 2+: targeted annual reports for remaining gaps.
        # Prioritize years with NO record over years with just incomplete fields,
        # then alternate oldest / newest to attack gaps from both ends.
        search_count = 0
        consecutive_empty = 0
        while fin_missing and search_count < MAX_SEARCHES_PER_TYPE:
            # Prefer years with no record at all — incomplete years can be
            # filled as a side effect when documents happen to cover them.
            no_record = target - _get_covered_years(session, company.id, FinancialRecord)
            pick_from = no_record if no_record else fin_missing

            if search_count % 2 == 0:
                target_year = min(pick_from)
                label = "no data" if no_record else "incomplete"
                year_label = f"{label}: {target_year}"
            else:
                target_year = max(pick_from)
                label = "no data" if no_record else "incomplete"
                year_label = f"{label}: {target_year}"

            log.info(f"  Financial search {search_count + 2}/{MAX_SEARCHES_PER_TYPE + 1} "
                     f"({year_label})")
            try:
                saved = _extract_financials_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, fin_covered, fin_searched_urls, target_year=target_year,
                )
                total_fin_saved += saved
                fin_missing = _get_financial_needs(session, company.id, target)
                if saved == 0:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        log.info("    Two consecutive empty searches, stopping")
                        break
                else:
                    consecutive_empty = 0
            except Exception as e:
                log.warning(f"    Financial search failed: {e}")
                session.rollback()
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break

            search_count += 1

        if total_fin_saved:
            fin_covered = _get_covered_years(session, company.id, FinancialRecord)
            log.info(f"  Financials: saved/updated {total_fin_saved} records "
                     f"(covering {sorted(fin_covered & target)})")
        still_missing = _get_financial_needs(session, company.id, target)
        if still_missing:
            log.info(f"  Financials: still incomplete for years {sorted(still_missing)}")

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

    if total_em_saved == 0 and total_fin_saved == 0 and not em_missing and not fin_missing:
        return {"status": "skipped"}

    return {
        "status": "success",
        "emissions_records": total_em_saved,
        "financial_records": total_fin_saved,
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
             f"(target years: {TARGET_START_YEAR}–{_current_year()})")

    run = PipelineRun(
        total_companies=len(companies),
        status="running",
    )
    session.add(run)
    session.commit()

    errors = []
    n_success = 0
    n_failed = 0
    n_skipped = 0

    for i, company in enumerate(companies):
        log.info(f"[{i + 1}/{len(companies)}] {company.name}")
        try:
            result = process_company(company, anthropic_key, exa_key, llama_key, session)
            if result.get("status") == "skipped":
                n_skipped += 1
            else:
                n_success += 1
        except Exception as e:
            log.error(f"  FAILED: {e}")
            errors.append(f"{company.name}: {e}")
            n_failed += 1
            session.rollback()

        if i < len(companies) - 1:
            time.sleep(delay_between)

        if (i + 1) % 10 == 0:
            run.successful = n_success
            run.failed = n_failed
            run.skipped = n_skipped
            session.commit()

    run.successful = n_success
    run.failed = n_failed
    run.skipped = n_skipped
    run.completed_at = datetime.utcnow()
    run.status = "completed"
    run.error_log = "\n".join(errors) if errors else None
    session.commit()

    log.info(
        f"Pipeline complete: {run.successful} succeeded, "
        f"{run.failed} failed, {run.skipped} skipped"
    )

    return run
