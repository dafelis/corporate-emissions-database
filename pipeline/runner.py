"""
Main pipeline runner — processes companies one by one, extracting emissions,
financial data, market data, and industry classifications.

Walks backwards from TARGET_END_YEAR to TARGET_START_YEAR, searching for
each missing year in turn. Bonus years found during a search are kept,
so earlier years get skipped if already covered.
"""

import json
import logging
import os
import re
import threading
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
    extract_emissions_from_pdf, verify_page_contains_values,
)
from pipeline.financial_extractor import find_financial_tables, extract_financials, normalise_to_units
from pipeline.financial_validator import validate_financial_entry, compute_evic
from pipeline.tier1_xbrl import extract_financials_from_xbrl
from pipeline.tier1_edgar import extract_financials_from_edgar
from pipeline.tier2_yfinance import extract_financials_from_yfinance
from pipeline.market_data import get_share_price_at_date, get_fallback_shares, get_industry_info
from pipeline.industry_classifier import classify_company
from pipeline.storage import upload_file
from pipeline.config import (
    TARGET_START_YEAR, TARGET_END_YEAR, MAX_SEARCHES,
    CONFIDENCE_THRESHOLD, MODEL_FAST, MODEL_STRONG,
    BUDGET_PER_COMPANY, BUDGET_GLOBAL, VERIFICATION_SKIP_THRESHOLD,
    CONCURRENT_COMPANIES, REGULATORY_EMISSIONS_ENABLED,
    ESEF_SECTION_MIN_CONFIDENCE,
)
from pipeline.regulatory_filings import (
    find_esef_filings, fetch_esef_report_text, find_ghg_sections,
    find_nsm_annual_reports,
)
import html as _html

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

# Cached input tokens are 90% cheaper
CACHE_DISCOUNT = 0.1


class BudgetExceeded(Exception):
    """Raised when a budget cap is hit."""

    def __init__(self, scope: str, spent: float, limit: float):
        self.scope = scope
        self.spent = spent
        self.limit = limit
        super().__init__(
            f"{scope} budget exceeded: ${spent:.4f} / ${limit:.2f}"
        )


def _new_cost_tracker():
    return {
        "total": 0.0, "calls": 0,
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "by_model": {},
    }


def _short_model(model: str) -> str:
    """Shorten a model ID for summary display."""
    if "haiku" in model:
        return "haiku"
    if "sonnet" in model:
        return "sonnet"
    if "opus" in model:
        return "opus"
    return model[:12]


def _record_model_usage(tracker, model, cost, usage):
    """Accumulate per-model cost so summaries can show what drove spend."""
    bucket = tracker["by_model"].setdefault(
        model, {"cost": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
    )
    bucket["cost"] += cost
    bucket["calls"] += 1
    bucket["input_tokens"] += usage.input_tokens
    bucket["output_tokens"] += usage.output_tokens


_global_lock = threading.Lock()


class _TrackedMessages:
    """Proxy for client.messages that records token usage and cost."""

    def __init__(self, messages, tracker, budget_limit=None, global_tracker=None, global_limit=None):
        self._messages = messages
        self._tracker = tracker
        self._budget_limit = budget_limit
        self._global_tracker = global_tracker
        self._global_limit = global_limit

    def create(self, **kwargs):
        response = self._messages.create(**kwargs)
        model = kwargs.get("model", "unknown")
        usage = response.usage
        in_rate, out_rate = MODEL_COSTS.get(model, (15.0, 75.0))

        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_create = getattr(usage, "cache_creation_input_tokens", 0) or 0
        regular_input = usage.input_tokens - cache_read - cache_create

        cost = (
            regular_input * in_rate
            + cache_read * in_rate * CACHE_DISCOUNT
            + cache_create * in_rate * 1.25
            + usage.output_tokens * out_rate
        ) / 1_000_000

        self._tracker["total"] += cost
        self._tracker["calls"] += 1
        self._tracker["input_tokens"] += usage.input_tokens
        self._tracker["output_tokens"] += usage.output_tokens
        self._tracker["cache_read_tokens"] += cache_read
        self._tracker["cache_creation_tokens"] += cache_create
        _record_model_usage(self._tracker, model, cost, usage)

        if self._global_tracker is not None:
            with _global_lock:
                self._global_tracker["total"] += cost
                self._global_tracker["calls"] += 1
                self._global_tracker["input_tokens"] += usage.input_tokens
                self._global_tracker["output_tokens"] += usage.output_tokens
                self._global_tracker["cache_read_tokens"] += cache_read
                self._global_tracker["cache_creation_tokens"] += cache_create
                _record_model_usage(self._global_tracker, model, cost, usage)
                global_total = self._global_tracker["total"]
        else:
            global_total = 0

        if self._budget_limit and self._tracker["total"] > self._budget_limit:
            raise BudgetExceeded("Per-company", self._tracker["total"], self._budget_limit)
        if self._global_limit and global_total > self._global_limit:
            raise BudgetExceeded("Global", global_total, self._global_limit)

        return response


class _TrackedClient:
    """Wraps an Anthropic client to track API costs transparently."""

    def __init__(self, client, tracker, budget_limit=None, global_tracker=None, global_limit=None):
        self._client = client
        self.messages = _TrackedMessages(
            client.messages, tracker,
            budget_limit=budget_limit,
            global_tracker=global_tracker,
            global_limit=global_limit,
        )

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


_YFINANCE_EXCHANGE_MAP = {
    ".L": "LON", ".AS": "AMS", ".PA": "EPA", ".DE": "ETR",
    ".MI": "BIT", ".MC": "BME", ".SW": "SWX", ".TO": "TSE",
    ".AX": "ASX", ".HK": "HKG", ".SI": "SGX", ".NS": "NSE",
}


def _google_finance_url(ticker: str) -> str:
    """Convert a yfinance ticker to a Google Finance URL."""
    for suffix, exchange in _YFINANCE_EXCHANGE_MAP.items():
        if ticker.upper().endswith(suffix.upper()):
            symbol = ticker[:-len(suffix)]
            return f"https://www.google.com/finance/quote/{symbol}:{exchange}"
    return f"https://www.google.com/finance/quote/{ticker}"


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

    # Years where the record exists but PCAF-required fields are still null
    incomplete_rows = (
        session.query(FinancialRecord.reporting_year)
        .filter(
            FinancialRecord.company_id == company_id,
            FinancialRecord.reporting_year.in_(list(target_years)),
            or_(
                FinancialRecord.revenue.is_(None),
                FinancialRecord.gross_debt.is_(None),
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


_NON_MASS_UNIT = re.compile(
    r"%|percent|intensit|\bper\b|change|index|ratio|target|baseline", re.I)


def _plausible_emissions_entry(entry) -> tuple[bool, str]:
    """Reject extraction artefacts that are not absolute emissions.

    Seen in practice: a percentage-change row returned with the unit
    'Percentage change, not absolute emissions'; a 2020 baseline total
    copied into every scope field.
    """
    unit = entry.get("unit") or ""
    if _NON_MASS_UNIT.search(unit):
        return False, f"unit '{unit}' is not an absolute mass"
    vals = [entry.get(k) for k in
            ("scope_1", "scope_2_location", "scope_2_market", "scope_3")]
    present = [v for v in vals if v is not None]
    if len(present) >= 3 and len(set(present)) == 1:
        return False, f"value {present[0]} repeated in every scope (total/baseline misassigned)"
    return True, ""


def _rank_evidence_pages(entry, filtered_pages, page_texts, top_n=3):
    """Rank filtered pages by likelihood of containing an extracted entry's values.

    Returns the top_n original page indices, best first. Used to narrow
    candidates before sending individual pages to Claude for verification.
    """

    def _format_variants(value):
        if value is None or value == 0:
            return []
        variants = []
        if isinstance(value, float):
            variants.append(str(value))
            variants.append(str(value).replace(".", ","))
            if value == int(value):
                variants.append(str(int(value)))
        elif isinstance(value, int):
            variants.append(str(value))
            variants.append(f"{value:,}")
        return variants

    s1 = entry.get("scope_1")
    s2l = entry.get("scope_2_location")
    s3 = entry.get("scope_3")
    year = entry.get("reporting_year")

    search_values = []
    for val in (s1, s2l, s3):
        search_values.extend(_format_variants(val))

    scored = []
    for pg_idx in filtered_pages:
        text = page_texts.get(pg_idx, "")
        if not text:
            continue
        score = 0
        if str(year) not in text:
            continue
        score += 1
        for val_str in search_values:
            if val_str in text:
                score += 2
        text_lower = text.lower()
        for kw in ("scope 1", "scope 2", "scope 3"):
            if kw in text_lower:
                score += 1
        if score > 0:
            scored.append((score, pg_idx))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = [(score, pg) for score, pg in scored[:top_n]]
    if not results:
        results = [(0, filtered_pages[0])]
    return results


# ══════════════════════════════════════════════════════════════════════════
# Emissions extraction (iterative)
# ══════════════════════════════════════════════════════════════════════════

def _extract_emissions_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None, events=None,
):
    """Run one search→parse→extract cycle for emissions. Returns count saved."""
    search_result = search_for_emissions_source(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
        client=client,
    )
    candidates = search_result.get("candidates", [search_result])
    url, title, source_type, table_dicts, tables_md = _try_parse_candidates(
        candidates, searched_urls, llama_key,
    )
    return _extract_emissions_from_document(
        url, title, source_type, table_dicts, tables_md,
        company, company_name, client, session, covered_years,
        target_year=target_year, events=events,
    )


def _extract_emissions_from_document(
    url, title, source_type, table_dicts, tables_md,
    company, company_name, client, session, covered_years,
    target_year=None, events=None,
):
    """Extract emissions from one parsed document and save them. Returns count saved.

    source_type is 'pdf' (filtered pages sent to Claude, evidence pages
    verified), 'html' (tables ranked, then text fallback) or 'esef'
    (pre-ranked text sections in tables_md from a regulatory XHTML filing).
    Tries ALL high-scoring tables/sections, accumulating unique years across
    them (a trend table, a detailed scope table, and a Scope 3 breakdown may
    each contribute different years).
    """
    saved = 0

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
            _SCOPE_KEYWORDS = ["scope 1", "scope 2", "scope 3"]
            for page_idx in range(len(pdf_doc)):
                text = pdf_doc[page_idx].get_text()
                if text and len(text.strip()) > 100:
                    lower = text.lower()
                    has_scope = any(kw in lower for kw in _SCOPE_KEYWORDS)
                    has_emissions = any(kw in lower for kw in _EMISSIONS_KEYWORDS)
                    if has_scope and has_emissions:
                        kw_page_indices.append(page_idx)
                        page_texts[page_idx] = text

            if kw_page_indices:
                log.info(f"    PDF extract: {len(kw_page_indices)} emissions-related "
                         f"pages (of {len(pdf_doc)} total)")

                # Build a filtered PDF with only the relevant pages
                filtered_pages = kw_page_indices[:15]
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

                    # Only escalate to Opus when Haiku found no
                    # usable scope values — not just low confidence
                    has_any_values = any(
                        e.get("scope_1") is not None
                        or e.get("scope_2_location") is not None
                        or e.get("scope_3") is not None
                        for e in extraction["emissions"]
                    )
                    if confidence < CONFIDENCE_THRESHOLD and not has_any_values:
                        log.info(f"    PDF extract: low confidence ({confidence})"
                                 " and no values, re-extracting with Opus")
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

                        if s1 is None and s2l is None and s2m is None and s3 is None:
                            log.info(f"    Year {year}: all scopes null, "
                                     f"skipping (not real data)")
                            continue

                        log.info(f"    Year {year}: S1={s1} S2L={s2l} "
                                 f"S2M={s2m} S3={s3} "
                                 f"unit={entry.get('unit')} conf={confidence}")

                        # Find the evidence page: text search narrows
                        # to candidates; skip Claude verification if
                        # the text match is strong (score >= 7 means
                        # year + multiple values + scope keywords)
                        ranked = _rank_evidence_pages(
                            entry, filtered_pages, page_texts, top_n=3)
                        best_score, best_pg = ranked[0]
                        original_pg = best_pg

                        verified = False
                        if best_score >= VERIFICATION_SKIP_THRESHOLD:
                            log.info(f"    Year {year}: strong text match "
                                     f"(score={best_score}) on page "
                                     f"{best_pg}, skipping verification")
                            verified = True
                        else:
                            for _, cand_pg in ranked:
                                try:
                                    confirmed = verify_page_contains_values(
                                        pdf_path, cand_pg, year,
                                        scope_1=s1, scope_2=s2l,
                                        scope_3=s3,
                                        unit=entry.get("unit",
                                                        "tonnes CO2e"),
                                        client=client,
                                    )
                                    if confirmed:
                                        original_pg = cand_pg
                                        log.info(f"    Year {year}: Claude"
                                                 f" confirmed page "
                                                 f"{cand_pg}")
                                        verified = True
                                        break
                                    log.info(f"    Year {year}: page "
                                             f"{cand_pg} not confirmed")
                                except Exception as ve:
                                    log.warning(f"    Year {year}: verify"
                                                f" page {cand_pg}: {ve}")

                        if not verified:
                            log.info(f"    Year {year}: no evidence page "
                                     f"confirmed, discarding "
                                     f"(likely not in this document)")
                            continue

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
        except BudgetExceeded:
            raise
        except Exception as e:
            log.warning(f"    PDF extraction failed: {e}")

    # ── ESEF path: pre-ranked text sections from a regulatory filing ────
    if not all_entries and source_type == "esef":
        for idx, section in enumerate(tables_md[:6]):
            try:
                extraction = extract_emissions_from_text(
                    section, company_name, client, model=MODEL_FAST)
                if not (extraction and extraction.get("emissions")):
                    continue
                confidence = extraction.get("confidence_score", 0) or 0
                has_any_values = any(
                    e.get("scope_1") is not None
                    or e.get("scope_2_location") is not None
                    or e.get("scope_3") is not None
                    for e in extraction["emissions"]
                )
                if confidence < CONFIDENCE_THRESHOLD and not has_any_values:
                    log.info(f"    ESEF section {idx}: low confidence ({confidence}) "
                             "and no values, re-extracting with Opus")
                    stronger = extract_emissions_from_text(
                        section, company_name, client, model=MODEL_STRONG)
                    if stronger and stronger.get("emissions"):
                        extraction = stronger
                        confidence = extraction.get("confidence_score", 0) or 0
                if confidence < ESEF_SECTION_MIN_CONFIDENCE:
                    log.info(f"    ESEF section {idx}: conf={confidence} < "
                             f"{ESEF_SECTION_MIN_CONFIDENCE}, ignored")
                    continue
                if matched_table_idx is None:
                    matched_table_idx = idx
                    methodology_notes = extraction.get("methodology_notes", "")
                best_confidence = max(best_confidence, confidence)
                # A market-based figure can only come from a section that
                # talks about market-based reporting; otherwise it's invented
                # (seen: 0, or a copy of the location-based value).
                section_has_market = "market" in section.lower()
                new_years = []
                for entry in extraction["emissions"]:
                    if not section_has_market:
                        entry["scope_2_market"] = None
                    year = entry["reporting_year"]
                    if year in seen_years:
                        continue
                    if (entry.get("scope_1") is None
                            and entry.get("scope_2_location") is None
                            and entry.get("scope_2_market") is None
                            and entry.get("scope_3") is None):
                        continue
                    entry["_table_idx"] = idx
                    all_entries.append(entry)
                    seen_years.add(year)
                    new_years.append(year)
                    if year == target_year:
                        target_year_table_idx = idx
                log.info(f"    ESEF section {idx}: conf={confidence} "
                         f"years={sorted(new_years)}")
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning(f"    ESEF section {idx} extraction failed: {e}")

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
                            log.info(f"    Escalating to Opus: table "
                                     f"{candidate_tbl['index']} confidence "
                                     f"{confidence} < {CONFIDENCE_THRESHOLD}")
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
                except BudgetExceeded:
                    raise
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
                    log.info(f"    Escalating to Opus: text fallback confidence "
                             f"{confidence} < {CONFIDENCE_THRESHOLD}")
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
            page_number=page_number, fetched_at=datetime.utcnow(),
        )
        session.add(source)
        session.flush()

        for entry in all_entries:
            year = entry["reporting_year"]
            if year < TARGET_START_YEAR:
                continue

            # Skip entries with no actual scope values
            if (entry.get("scope_1") is None
                    and entry.get("scope_2_location") is None
                    and entry.get("scope_2_market") is None
                    and entry.get("scope_3") is None):
                continue

            plausible, why = _plausible_emissions_entry(entry)
            if not plausible:
                log.info(f"    Year {year}: rejected — {why}")
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
                        extraction_date=datetime.utcnow(),
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
                extraction_date=datetime.utcnow(),
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


def _run_regulatory_emissions_tier(
    company, company_name, client, session, covered_years, target, events=None,
):
    """Tier 0: emissions from regulatory annual-report filings, by LEI.

    ESEF XHTML reports (filings.xbrl.org, EU+UK) first, newest report first —
    each usually carries the prior year as a comparative, so a report for FY
    y is fetched only if y or y-1 is still missing. Then UK NSM PDFs for any
    remaining years that predate ESEF. Returns count saved.
    """
    if not company.lei:
        log.info("  Tier 0 regulatory: no LEI on record, skipping")
        return 0
    missing = target - covered_years
    if not missing:
        return 0

    saved = 0
    safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
    debug_dir = os.path.join(os.path.dirname(__file__), "..", "debug")
    os.makedirs(debug_dir, exist_ok=True)

    # ── ESEF (filings.xbrl.org) ─────────────────────────────────────────
    try:
        filings = find_esef_filings(company.lei)
    except Exception as e:
        log.warning(f"  Tier 0 ESEF: lookup failed: {e}")
        filings = []
    if filings:
        log.info(f"  Tier 0 ESEF: {len(filings)} filing(s) for LEI {company.lei}: "
                 f"{[f['year'] for f in filings]}")
    else:
        log.info(f"  Tier 0 ESEF: no filings for LEI {company.lei}")

    for f in filings:
        year = f["year"]
        if year < TARGET_START_YEAR:
            break
        if year not in missing and (year - 1) not in missing:
            continue
        try:
            log.info(f"  Tier 0 ESEF: FY{year} report ({f['country']}) "
                     f"{f['report_url']}")
            text = fetch_esef_report_text(f["report_url"])
            sections = find_ghg_sections(text)
            log.info(f"    {len(text):,} chars of text, "
                     f"{len(sections)} GHG section(s)")
            if not sections:
                continue
            with open(os.path.join(debug_dir, f"{safe_name}_esef_{year}_sections.txt"),
                      "w", encoding="utf-8") as _sf:
                _sf.write("\n\n==== SECTION ====\n\n".join(sections))
            table_dicts = [{
                "markdown": s,
                "html_snippet": "<pre style='white-space:pre-wrap'>"
                                + _html.escape(s[:4000]) + "</pre>",
            } for s in sections]
            n = _extract_emissions_from_document(
                f["report_url"],
                f"{company_name} Annual Report FY{year} (ESEF filing, {f['country']})",
                "esef", table_dicts, list(sections),
                company, company_name, client, session, covered_years,
                target_year=year, events=events,
            )
            saved += n
            log.info(f"  Tier 0 ESEF: FY{year}: saved {n} record(s)")
        except BudgetExceeded:
            raise
        except Exception as e:
            log.warning(f"  Tier 0 ESEF: FY{year} failed: {e}")
            session.rollback()
        missing = target - covered_years
        if not missing:
            break

    # ── UK NSM PDFs for years still missing (pre-ESEF) ──────────────────
    if missing:
        try:
            reports = [r for r in find_nsm_annual_reports(company.lei) if r["format"] == "pdf"]
        except Exception as e:
            log.warning(f"  Tier 0 NSM: lookup failed: {e}")
            reports = []
        if reports:
            log.info(f"  Tier 0 NSM: {len(reports)} PDF annual report(s): "
                     f"{[r['year'] for r in reports]}")
        for r in reports:
            year = r["year"]
            if year < TARGET_START_YEAR:
                break
            if year not in missing and (year - 1) not in missing:
                continue
            try:
                log.info(f"  Tier 0 NSM: FY{year} '{r['title']}' {r['url']}")
                n = _extract_emissions_from_document(
                    r["url"], f"{r['title']} (FCA NSM filing)", "pdf", [], [],
                    company, company_name, client, session, covered_years,
                    target_year=year, events=events,
                )
                saved += n
                log.info(f"  Tier 0 NSM: FY{year}: saved {n} record(s)")
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning(f"  Tier 0 NSM: FY{year} failed: {e}")
                session.rollback()
            missing = target - covered_years
            if not missing:
                break

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
    fin_extraction_notes = ""

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
                    notes = fin_extraction.get("methodology_notes", "")
                    if notes:
                        fin_extraction_notes = notes

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
        except BudgetExceeded:
            raise
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
        page_number=page_number, fetched_at=datetime.utcnow(),
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

        # Helper to extract value from nested {value, ref, ...} or flat number
        def _fval(field_name):
            obj = entry.get(field_name)
            if obj is None:
                return None
            if isinstance(obj, dict):
                return normalise_to_units(obj.get("value"), multiplier)
            return normalise_to_units(obj, multiplier)

        def _fref(field_name):
            obj = entry.get(field_name)
            return obj.get("ref") if isinstance(obj, dict) else None

        def _fconf(field_name):
            obj = entry.get(field_name)
            return obj.get("confidence") if isinstance(obj, dict) else None

        def _flabel(field_name):
            obj = entry.get(field_name)
            return obj.get("label") if isinstance(obj, dict) else None

        new_revenue = _fval("revenue")
        new_gross_debt = _fval("gross_debt")
        new_lease_liab = _fval("lease_liabilities")
        new_nci = _fval("non_controlling_interests")
        new_pref = _fval("preference_shares")
        new_shares = _fval("shares_outstanding")

        # Legacy fields for backwards compatibility
        new_debt_legacy = new_gross_debt
        new_cash = normalise_to_units(
            entry.get("cash_and_equivalents"), multiplier)

        # Validation
        val_flags = validate_financial_entry(entry, reporting_year=year)
        if val_flags:
            log.info(f"    Year {year}: validation flags: {val_flags}")

        # Upsert logic
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
                ("gross_debt", new_gross_debt),
                ("outstanding_debt", new_debt_legacy),
                ("cash_and_equivalents", new_cash),
                ("lease_liabilities", new_lease_liab),
                ("non_controlling_interests", new_nci),
                ("preference_shares", new_pref),
                ("shares_outstanding", new_shares),
            ]:
                old_val = getattr(existing_record, attr, None)
                if old_val is None and new_val is not None:
                    setattr(existing_record, attr, new_val)
                    updated = True
                elif upgrade and new_val is not None and old_val != new_val:
                    setattr(existing_record, attr, new_val)
                    updated = True

            # Fill metadata regardless of source quality
            if existing_record.currency is None and entry.get("currency"):
                existing_record.currency = entry["currency"]
            if existing_record.fiscal_year_end is None:
                existing_record.fiscal_year_end = _parse_date(
                    entry.get("reporting_date") or entry.get("fiscal_year_end"))
            if existing_record.period_start is None:
                existing_record.period_start = _parse_date(entry.get("period_start"))
            if existing_record.period_end is None:
                existing_record.period_end = _parse_date(entry.get("period_end"))

            # Update provenance fields on upgrade
            if updated and upgrade:
                existing_record.source_id = fin_source.id
                existing_record.confidence_score = best_confidence
                existing_record.source_tier = 3
                existing_record.source_type = source_type
                existing_record.revenue_ref = _fref("revenue")
                existing_record.revenue_label = _flabel("revenue")
                existing_record.revenue_confidence = _fconf("revenue")
                existing_record.gross_debt_ref = _fref("gross_debt")
                existing_record.gross_debt_confidence = _fconf("gross_debt")
                existing_record.nci_ref = _fref("non_controlling_interests")
                existing_record.nci_confidence = _fconf("non_controlling_interests")
                existing_record.shares_outstanding_ref = _fref("shares_outstanding")
                existing_record.shares_outstanding_confidence = _fconf("shares_outstanding")
                existing_record.lease_liabilities_ref = _fref("lease_liabilities")
                existing_record.lease_liabilities_confidence = _fconf("lease_liabilities")
                debt_obj = entry.get("gross_debt")
                if isinstance(debt_obj, dict) and debt_obj.get("components"):
                    existing_record.gross_debt_components = json.dumps(
                        debt_obj["components"])
                pref_obj = entry.get("preference_shares")
                if isinstance(pref_obj, dict):
                    existing_record.preference_shares_classification = pref_obj.get("classification")
                    existing_record.preference_shares_listed = pref_obj.get("listed")
                    existing_record.preference_shares_ref = pref_obj.get("ref")
                shares_obj = entry.get("shares_outstanding")
                if isinstance(shares_obj, dict):
                    existing_record.shares_outstanding_share_class = shares_obj.get("share_class")
                existing_record.is_financial_institution = entry.get("is_financial_institution")
                if val_flags:
                    existing_record.validation_flags = json.dumps(val_flags)
                notes = entry.get("notes", [])
                if notes:
                    existing_record.extraction_notes = json.dumps(notes)
                old_title = old_source.title if old_source else "unknown"
                log.info(f"    Year {year}: UPGRADED source "
                         f"'{old_title}' (q={old_quality}) → "
                         f"'{title}' (q={new_quality})")
                saved += 1
            elif updated:
                log.info(f"    Updated year {year}: filled in missing fields")
                saved += 1

            covered_years.add(year)
            continue

        # New record
        debt_obj = entry.get("gross_debt")
        pref_obj = entry.get("preference_shares")
        shares_obj = entry.get("shares_outstanding")
        notes = entry.get("notes", [])

        fin_record = FinancialRecord(
            company_id=company.id,
            reporting_year=year,
            fiscal_year_end=_parse_date(
                entry.get("reporting_date") or entry.get("fiscal_year_end")),
            period_start=_parse_date(entry.get("period_start")),
            period_end=_parse_date(entry.get("period_end")),
            currency=entry.get("currency"),
            units=entry.get("units"),
            # Core PCAF fields
            revenue=new_revenue,
            revenue_label=_flabel("revenue"),
            revenue_ref=_fref("revenue"),
            revenue_confidence=_fconf("revenue"),
            gross_debt=new_gross_debt,
            gross_debt_components=(
                json.dumps(debt_obj["components"])
                if isinstance(debt_obj, dict) and debt_obj.get("components")
                else None),
            gross_debt_ref=_fref("gross_debt"),
            gross_debt_confidence=_fconf("gross_debt"),
            lease_liabilities=new_lease_liab,
            lease_liabilities_ref=_fref("lease_liabilities"),
            lease_liabilities_confidence=_fconf("lease_liabilities"),
            non_controlling_interests=new_nci,
            nci_ref=_fref("non_controlling_interests"),
            nci_confidence=_fconf("non_controlling_interests"),
            preference_shares=new_pref,
            preference_shares_classification=(
                pref_obj.get("classification")
                if isinstance(pref_obj, dict) else None),
            preference_shares_listed=(
                pref_obj.get("listed")
                if isinstance(pref_obj, dict) else None),
            preference_shares_ref=(
                pref_obj.get("ref")
                if isinstance(pref_obj, dict) else None),
            shares_outstanding=new_shares,
            shares_outstanding_share_class=(
                shares_obj.get("share_class")
                if isinstance(shares_obj, dict) else None),
            shares_outstanding_ref=_fref("shares_outstanding"),
            shares_outstanding_confidence=_fconf("shares_outstanding"),
            is_financial_institution=entry.get("is_financial_institution"),
            # Legacy fields
            outstanding_debt=new_debt_legacy,
            cash_and_equivalents=new_cash,
            # Metadata
            source_id=fin_source.id,
            source_tier=3,
            source_type=source_type,
            confidence_score=best_confidence,
            methodology_notes=fin_extraction_notes,
            validation_flags=json.dumps(val_flags) if val_flags else None,
            extraction_notes=json.dumps(notes) if notes else None,
            review_status="flagged" if val_flags else "pending",
            extraction_date=datetime.utcnow(),
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


def _debt_components_json(entry: dict) -> str | None:
    """Extract gross_debt component details from provenance for storage."""
    prov = entry.get("provenance", {}).get("gross_debt", {})
    if isinstance(prov, dict) and prov.get("components"):
        return json.dumps(prov["components"])
    # Fallback to entry-level components
    debt_obj = entry.get("gross_debt")
    if isinstance(debt_obj, dict) and debt_obj.get("components"):
        return json.dumps(debt_obj["components"])
    return None


def _save_api_financial_entries(
    entries: list[dict],
    company,
    session,
    covered_years: set,
    source_url: str,
    source_title: str,
    tier: int,
    events=None,
) -> int:
    """Save structured financial entries from Tier 1/2 APIs to the database.

    Returns count of records created or updated.
    """
    if not entries:
        return 0

    # Default source for the batch (used when entries have no per-filing URL)
    batch_source = Source(
        company_id=company.id,
        url=source_url,
        title=source_title,
        document_type="api",
        fetched_at=datetime.utcnow(),
    )
    session.add(batch_source)
    session.flush()

    saved = 0
    for entry in entries:
        year = entry["reporting_year"]
        if year < TARGET_START_YEAR:
            continue

        # Per-filing source with year-specific viewer URL
        viewer_url = entry.get("viewer_url")
        if viewer_url:
            api_source = Source(
                company_id=company.id,
                url=viewer_url,
                title=f"{source_title} — {year}",
                document_type="api",
                fetched_at=datetime.utcnow(),
            )
            session.add(api_source)
            session.flush()
        else:
            api_source = batch_source

        multiplier = entry.get("unit_multiplier", 1) or 1

        def _fval(field_name):
            obj = entry.get(field_name)
            if obj is None:
                return None
            if isinstance(obj, dict):
                return normalise_to_units(obj.get("value"), multiplier)
            return normalise_to_units(obj, multiplier)

        def _fref(field_name):
            obj = entry.get(field_name)
            return obj.get("ref") if isinstance(obj, dict) else None

        def _fconf(field_name):
            obj = entry.get(field_name)
            return obj.get("confidence") if isinstance(obj, dict) else None

        def _flabel(field_name):
            obj = entry.get(field_name)
            return obj.get("label") if isinstance(obj, dict) else None

        new_revenue = _fval("revenue")
        new_gross_debt = _fval("gross_debt")
        new_lease_liab = _fval("lease_liabilities")
        new_nci = _fval("non_controlling_interests")
        new_pref = _fval("preference_shares")
        new_shares = _fval("shares_outstanding")
        new_cash = normalise_to_units(entry.get("cash_and_equivalents"), multiplier)

        val_flags = validate_financial_entry(entry, reporting_year=year)
        if val_flags:
            log.info(f"    Year {year}: validation flags: {val_flags}")

        existing = (
            session.query(FinancialRecord)
            .filter_by(company_id=company.id, reporting_year=year)
            .first()
        )

        if existing:
            # API data (Tier 1/2) is high-confidence structured data —
            # fill nulls, and upgrade from lower tiers
            upgrade = (existing.source_tier or 99) > tier
            updated = False
            for attr, new_val in [
                ("revenue", new_revenue),
                ("gross_debt", new_gross_debt),
                ("outstanding_debt", new_gross_debt),
                ("cash_and_equivalents", new_cash),
                ("lease_liabilities", new_lease_liab),
                ("non_controlling_interests", new_nci),
                ("preference_shares", new_pref),
                ("shares_outstanding", new_shares),
            ]:
                old_val = getattr(existing, attr, None)
                if old_val is None and new_val is not None:
                    setattr(existing, attr, new_val)
                    updated = True
                elif upgrade and new_val is not None and old_val != new_val:
                    setattr(existing, attr, new_val)
                    updated = True

            if existing.currency is None and entry.get("currency"):
                existing.currency = entry["currency"]
            if existing.fiscal_year_end is None:
                existing.fiscal_year_end = _parse_date(entry.get("reporting_date"))

            if updated:
                existing.source_id = api_source.id
                existing.source_tier = tier
                existing.source_type = "api"
                existing.revenue_ref = _fref("revenue")
                existing.revenue_label = _flabel("revenue")
                existing.revenue_confidence = _fconf("revenue")
                existing.gross_debt_ref = _fref("gross_debt")
                existing.gross_debt_confidence = _fconf("gross_debt")
                existing.nci_ref = _fref("non_controlling_interests")
                existing.nci_confidence = _fconf("non_controlling_interests")
                existing.shares_outstanding_ref = _fref("shares_outstanding")
                existing.shares_outstanding_confidence = _fconf("shares_outstanding")
                existing.lease_liabilities_ref = _fref("lease_liabilities")
                existing.lease_liabilities_confidence = _fconf("lease_liabilities")
                existing.is_financial_institution = entry.get("is_financial_institution")
                notes = entry.get("notes", [])
                existing.extraction_notes = json.dumps({
                    "notes": notes,
                    "provenance": entry.get("provenance", {}),
                    "entity_name": company.name,
                    "lei": getattr(company, "lei", None),
                })
                if val_flags:
                    existing.validation_flags = json.dumps(val_flags)
                log.info(f"    Year {year}: updated from Tier {tier} API")
                saved += 1

            covered_years.add(year)
            continue

        # New record
        debt_obj = entry.get("gross_debt")
        pref_obj = entry.get("preference_shares")
        shares_obj = entry.get("shares_outstanding")
        notes = entry.get("notes", [])

        fin_record = FinancialRecord(
            company_id=company.id,
            reporting_year=year,
            fiscal_year_end=_parse_date(entry.get("reporting_date")),
            currency=entry.get("currency"),
            revenue=new_revenue,
            revenue_label=_flabel("revenue"),
            revenue_ref=_fref("revenue"),
            revenue_confidence=_fconf("revenue"),
            gross_debt=new_gross_debt,
            gross_debt_components=_debt_components_json(entry),
            gross_debt_ref=_fref("gross_debt"),
            gross_debt_confidence=_fconf("gross_debt"),
            lease_liabilities=new_lease_liab,
            lease_liabilities_ref=_fref("lease_liabilities"),
            lease_liabilities_confidence=_fconf("lease_liabilities"),
            non_controlling_interests=new_nci,
            nci_ref=_fref("non_controlling_interests"),
            nci_confidence=_fconf("non_controlling_interests"),
            preference_shares=new_pref,
            preference_shares_classification=(
                pref_obj.get("classification")
                if isinstance(pref_obj, dict) else None),
            preference_shares_listed=(
                pref_obj.get("listed")
                if isinstance(pref_obj, dict) else None),
            preference_shares_ref=(
                pref_obj.get("ref")
                if isinstance(pref_obj, dict) else None),
            shares_outstanding=new_shares,
            shares_outstanding_share_class=(
                shares_obj.get("share_class")
                if isinstance(shares_obj, dict) else None),
            shares_outstanding_ref=_fref("shares_outstanding"),
            shares_outstanding_confidence=_fconf("shares_outstanding"),
            is_financial_institution=entry.get("is_financial_institution"),
            outstanding_debt=new_gross_debt,
            cash_and_equivalents=new_cash,
            source_id=api_source.id,
            source_tier=tier,
            source_type="api",
            validation_flags=json.dumps(val_flags) if val_flags else None,
            extraction_notes=json.dumps({
                "notes": notes,
                "provenance": entry.get("provenance", {}),
                "entity_name": company.name,
                "lei": getattr(company, "lei", None),
            }),
            review_status="flagged" if val_flags else "pending",
            extraction_date=datetime.utcnow(),
        )
        session.add(fin_record)
        covered_years.add(year)
        saved += 1

    session.commit()

    if events is not None and saved:
        years_saved = {e["reporting_year"] for e in entries
                       if e["reporting_year"] >= TARGET_START_YEAR}
        events.append({
            "type": "source", "url": source_url,
            "title": source_title, "years": years_saved,
        })
        events.append({"type": "financial", "years": years_saved})

    return saved


def _run_tier1_and_tier2(
    company, company_name, session, fin_covered, target, events=None,
    run_tier1: bool = True, run_tier2: bool = True,
) -> int:
    """Run Tier 1 (XBRL APIs) and Tier 2 (yfinance) for financial data.

    Returns total count of records saved/updated.
    """
    total_saved = 0
    fin_missing = _get_financial_needs(session, company.id, target)

    if not fin_missing:
        return 0

    # ── Tier 1a: filings.xbrl.org (UK ESEF/UKSEF) ────────────────────
    if company.lei and run_tier1:
        log.info(f"  Tier 1 XBRL: searching filings.xbrl.org (LEI {company.lei})")
        try:
            xbrl_entries = extract_financials_from_xbrl(
                company.lei, company_name, fin_missing)
            if xbrl_entries:
                saved = _save_api_financial_entries(
                    xbrl_entries, company, session, fin_covered,
                    source_url=f"https://filings.xbrl.org/{company.lei}/",
                    source_title=f"XBRL IFRS filing ({company.lei})",
                    tier=1, events=events,
                )
                total_saved += saved
                fin_missing = _get_financial_needs(session, company.id, target)
                log.info(f"  Tier 1 XBRL: saved {saved} record(s), "
                         f"still need {sorted(fin_missing) or 'nothing'}")
        except Exception as e:
            log.warning(f"  Tier 1 XBRL: failed: {e}")

    # ── Tier 1b: SEC EDGAR (dual-listed / US filers) ─────────────────
    if company.ticker and fin_missing and run_tier1:
        log.info(f"  Tier 1 EDGAR: searching SEC filings ({company.ticker})")
        try:
            edgar_entries = extract_financials_from_edgar(
                company.ticker, company_name, fin_missing)
            if edgar_entries:
                ticker_clean = company.ticker.upper().replace(".L", "")
                saved = _save_api_financial_entries(
                    edgar_entries, company, session, fin_covered,
                    source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company={ticker_clean}&type=10-K&output=atom",
                    source_title=f"SEC EDGAR XBRL ({company.ticker})",
                    tier=1, events=events,
                )
                total_saved += saved
                fin_missing = _get_financial_needs(session, company.id, target)
                log.info(f"  Tier 1 EDGAR: saved {saved} record(s), "
                         f"still need {sorted(fin_missing) or 'nothing'}")
        except Exception as e:
            log.warning(f"  Tier 1 EDGAR: failed: {e}")

    # ── Tier 2: yfinance structured data ─────────────────────────────
    if company.ticker and fin_missing and run_tier2:
        log.info(f"  Tier 2 yfinance: fetching financial data ({company.ticker})")
        try:
            yf_entries = extract_financials_from_yfinance(
                company.ticker, company_name)
            if yf_entries:
                # Filter to only years we still need
                yf_filtered = [e for e in yf_entries
                               if e["reporting_year"] in fin_missing]
                if yf_filtered:
                    saved = _save_api_financial_entries(
                        yf_filtered, company, session, fin_covered,
                        source_url=_google_finance_url(company.ticker),
                        source_title=f"Yahoo Finance ({company.ticker})",
                        tier=2, events=events,
                    )
                    total_saved += saved
                    fin_missing = _get_financial_needs(session, company.id, target)
                    log.info(f"  Tier 2 yfinance: saved {saved} record(s), "
                             f"still need {sorted(fin_missing) or 'nothing'}")
        except Exception as e:
            log.warning(f"  Tier 2 yfinance: failed: {e}")

    return total_saved


def _extract_financials_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None, events=None,
):
    """Run one search→parse→extract cycle for financials. Returns count saved."""
    fin_search = search_for_annual_report(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
        client=client,
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
    cache_pct = (
        f" ({cost.get('cache_read_tokens', 0):,} cached)"
        if cost.get("cache_read_tokens") else ""
    )
    lines.append(f"  Cost: ${cost['total']:.4f} "
                 f"({cost['calls']} API calls, "
                 f"{cost['input_tokens']:,} in / {cost['output_tokens']:,} out"
                 f"{cache_pct})")
    for model, b in sorted(cost.get("by_model", {}).items(),
                           key=lambda kv: -kv[1]["cost"]):
        share = (b["cost"] / cost["total"] * 100) if cost["total"] else 0
        lines.append(f"    · {_short_model(model):12s} ${b['cost']:7.4f} "
                     f"({share:4.1f}%)  {b['calls']:3d} calls  "
                     f"{b['input_tokens']:,} in / {b['output_tokens']:,} out")
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
    n_budget = sum(1 for r in results if r.get("status") == "budget_paused")

    summary_parts = [f"{n_success} processed", f"{n_skipped} skipped", f"{n_failed} failed"]
    if n_budget:
        summary_parts.append(f"{n_budget} budget-paused")
    lines.append(f"  Companies: {len(results)} total — " + ", ".join(summary_parts))

    total_em = sum(r.get("emissions_records", 0) for r in results)
    total_fin = sum(r.get("financial_records", 0) for r in results)
    lines.append(f"  Records saved: {total_em} emissions, {total_fin} financial")

    # Per-company one-liners
    if len(results) > 1:
        lines.append("")
        for r in results:
            em = r.get("emissions_records", 0)
            fin = r.get("financial_records", 0)
            status_icon = {"success": "✓", "skipped": "–", "failed": "✗", "budget_paused": "$"}.get(r["status"], "?")
            cost_str = f"${r.get('cost_detail', {}).get('total', 0):.4f}"
            lines.append(f"  {status_icon} {r['company']:30s}  "
                         f"em={em:2d}  fin={fin:2d}  {cost_str}")

    lines.append("")
    lines.append(f"  Total cost:  ${total_cost['total']:.4f}")
    lines.append(f"  Total time:  {total_elapsed_s / 60:.1f} min")
    lines.append(f"  API calls:   {total_cost['calls']}")
    cache_read = total_cost.get("cache_read_tokens", 0)
    cache_info = f" ({cache_read:,} cached)" if cache_read else ""
    lines.append(f"  Tokens:      {total_cost['input_tokens']:,} in / "
                 f"{total_cost['output_tokens']:,} out{cache_info}")

    by_model = total_cost.get("by_model", {})
    if by_model:
        lines.append("")
        lines.append("  Cost by model:")
        for model, b in sorted(by_model.items(), key=lambda kv: -kv[1]["cost"]):
            share = (b["cost"] / total_cost["total"] * 100) if total_cost["total"] else 0
            lines.append(f"    {_short_model(model):8s} ${b['cost']:8.4f}  "
                         f"({share:5.1f}%)  {b['calls']:4d} calls  "
                         f"{b['input_tokens']:>10,} in / {b['output_tokens']:>8,} out")
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
    skip_emissions: bool = False,
    skip_financial: bool = False,
    tiers: set[int] | None = None,
    budget_per_company: float | None = None,
    global_tracker: dict | None = None,
    budget_global: float | None = None,
) -> dict:
    """Process a single company: walk backwards from TARGET_END_YEAR to fill gaps.

    Pipeline:
    1. Walk backwards through years, searching sustainability reports for emissions
    2. Walk backwards through years, searching annual reports for financials
    3. yfinance → equity value at fiscal year-end for all financial records
    4. Claude → industry classification (NAICS/NACE/SIC)

    Returns a dict with status and details.
    """
    if tiers is None:
        tiers = {1, 2, 3}
    cost = _new_cost_tracker()
    raw_client = anthropic.Anthropic(api_key=anthropic_key)
    client = _TrackedClient(
        raw_client, cost,
        budget_limit=budget_per_company,
        global_tracker=global_tracker,
        global_limit=budget_global,
    )
    company_name = company.name
    events = []
    t_start = time.time()
    tier_label = "all" if tiers == {1, 2, 3} else ",".join(str(t) for t in sorted(tiers))
    log.info(f"Processing: {company_name} (tiers: {tier_label})")

    target = _target_years()
    has_industry = company.yfinance_sector is not None

    # ── PART 1: Emissions (backwards walk) ─────────────────────────────────

    if skip_emissions:
        log.info("  Skipping emissions (--financial only)")
    em_covered = _get_covered_years(session, company.id, EmissionsRecord)
    em_pre_existing = set(em_covered)  # years in DB before this run
    em_searched_years = set()          # years explicitly targeted during this run
    em_searched_urls = set()
    total_em_saved = 0
    em_missing = target - em_pre_existing

    if em_missing and not skip_emissions:
        log.info(f"  Emissions: have {sorted(em_pre_existing) or 'none'}, "
                 f"missing {sorted(em_missing)}")

        if REGULATORY_EMISSIONS_ENABLED:
            try:
                reg_saved = _run_regulatory_emissions_tier(
                    company, company_name, client, session, em_covered, target,
                    events=events,
                )
                total_em_saved += reg_saved
                em_missing = target - em_covered
                if reg_saved:
                    log.info(f"  Tier 0 regulatory: saved {reg_saved} record(s); "
                             f"still missing {sorted(em_missing) or 'nothing'}")
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning(f"  Tier 0 regulatory failed: {e}")
                session.rollback()

        search_count = 0
        consecutive_empty = 0
        # Walk backwards: newest year first, then year-1, year-2, ...
        walk_year = TARGET_END_YEAR
        while walk_year >= TARGET_START_YEAR and search_count < MAX_SEARCHES:
            # Skip years that existed before this run OR were explicitly searched
            if walk_year in em_covered or walk_year in em_searched_years:
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
            except BudgetExceeded:
                raise
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

    # ── PART 2: Financials — Tier 1 (XBRL APIs) → Tier 2 (yfinance) → Tier 3 (Exa+PDF) ──

    if skip_financial:
        log.info("  Skipping financials (--emissions only)")

    fin_covered = _get_covered_years(session, company.id, FinancialRecord)
    fin_missing = _get_financial_needs(session, company.id, target)
    fin_searched_urls = set()
    total_fin_saved = 0

    if fin_missing and not skip_financial:
        log.info(f"  Financials: have {sorted(fin_covered) or 'none'}, "
                 f"need data for {sorted(fin_missing)}")

        # ── Tier 1 + Tier 2: structured API sources (free, fast, reliable) ──
        if tiers & {1, 2}:
            api_saved = _run_tier1_and_tier2(
                company, company_name, session, fin_covered, target,
                events=events, run_tier1=(1 in tiers), run_tier2=(2 in tiers))
            total_fin_saved += api_saved

            fin_missing = _get_financial_needs(session, company.id, target)
            if fin_missing:
                log.info(f"  After Tier 1+2: still need {sorted(fin_missing)}")
            else:
                log.info(f"  Tier 1+2 covered all target years — skipping Tier 3")

        # ── Tier 3: Exa search + PDF/HTML extraction (expensive fallback) ──
        if fin_missing and 3 in tiers:
            log.info(f"  Tier 3: searching for remaining years {sorted(fin_missing)}")

            # Search 1: try a five-year financial summary first
            log.info(f"  Tier 3 search 1 (five-year summary)")
            try:
                fin_history_search = search_for_financial_history(
                    company_name, anthropic_key, exa_key,
                    exclude_urls=list(fin_searched_urls),
                    client=client,
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
            except BudgetExceeded:
                raise
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
                log.info(f"  Tier 3 search {search_count}/{MAX_SEARCHES} "
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
                except BudgetExceeded:
                    raise
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

    if company.ticker and not skip_financial:
        fin_records = (
            session.query(FinancialRecord)
            .filter_by(company_id=company.id)
            .filter(FinancialRecord.equity_value.is_(None))
            .all()
        )
        if fin_records:
            log.info(f"  Fetching market data for {len(fin_records)} financial records...")
            try:
                yf_source = Source(
                    company_id=company.id,
                    url=_google_finance_url(company.ticker),
                    title=f"Market data via yfinance ({company.ticker})",
                    document_type="api",
                    fetched_at=datetime.utcnow(),
                )
                session.add(yf_source)
                session.flush()

                for fr in fin_records:
                    target_date = fr.fiscal_year_end or date_type(fr.reporting_year, 12, 31)
                    price_data = get_share_price_at_date(company.ticker, target_date)
                    if not price_data:
                        continue
                    price = price_data["share_price"]
                    price_date = price_data["price_date"]
                    currency = price_data["currency"]

                    # Shares: prefer filing data, then yfinance fallbacks
                    shares = fr.shares_outstanding
                    shares_source = "filing"
                    if shares is None:
                        shares = get_fallback_shares(company.ticker, target_date)
                        shares_source = "yfinance"
                    if shares is None:
                        log.warning(f"    {fr.reporting_year}: no shares data, skipping equity")
                        continue

                    market_cap = price * shares
                    fr.equity_value = market_cap
                    fr.share_price_at_fy_end = price
                    fr.equity_currency = currency
                    fr.market_data_source_id = yf_source.id
                    if shares_source == "yfinance":
                        fr.shares_outstanding = shares

                    # Store equity provenance
                    try:
                        notes_data = json.loads(fr.extraction_notes) if fr.extraction_notes else {}
                    except (json.JSONDecodeError, TypeError):
                        notes_data = {}
                    prov = notes_data.get("provenance", {})
                    prov["equity_value"] = {
                        "concept": "market_cap",
                        "value": market_cap,
                        "unit": f"iso4217:{currency}",
                        "period": str(price_date),
                        "calculated": True,
                        "ticker": company.ticker,
                        "share_price": price,
                        "shares": shares,
                        "shares_source": shares_source,
                        "components": [
                            {"concept": "Share price", "value": price,
                             "period": str(price_date), "calculated": False},
                            {"concept": "Shares outstanding", "value": shares,
                             "period": prov.get("shares_outstanding", {}).get("period", ""),
                             "calculated": False, "source": shares_source},
                        ],
                    }
                    notes_data["provenance"] = prov
                    notes_data["entity_name"] = notes_data.get("entity_name", company.name)
                    fr.extraction_notes = json.dumps(notes_data)
                    # Legacy enterprise_value
                    if fr.outstanding_debt is not None and fr.cash_and_equivalents is not None:
                        fr.enterprise_value = (
                            fr.equity_value + fr.outstanding_debt - fr.cash_and_equivalents
                        )
                    # PCAF EVIC
                    evic = compute_evic(fr)
                    evic_str = f", EVIC={evic:,.0f}" if evic else ""
                    log.info(f"    {fr.reporting_year}: equity={market_cap:,.0f} "
                             f"{currency} (price={price:.2f} × shares={shares:,} "
                             f"[{shares_source}]){evic_str}")
                session.commit()
            except BudgetExceeded:
                raise
            except Exception as e:
                log.warning(f"  Market data fetch failed: {e}")
                session.rollback()

    # ── PART 4: Industry classification ───────────────────────────────────

    if not has_industry and company.ticker and not skip_financial:
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
        except BudgetExceeded:
            raise
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

def _ask_continue(scope: str, spent: float, limit: float, company: str = "") -> bool:
    """Prompt the user when a budget cap is hit. Returns True to continue."""
    ctx = f" (during {company})" if company else ""
    print(f"\n{'='*60}")
    print(f"  ⚠  {scope} budget exceeded{ctx}")
    print(f"     Spent: ${spent:.4f}  /  Limit: ${limit:.2f}")
    print(f"{'='*60}")
    try:
        answer = input("  Continue? [y/N] ").strip().lower()
        return answer in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run_pipeline(
    database_url: str,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    company_ids: list[int] = None,
    delay_between: float = 2.0,
    skip_emissions: bool = False,
    skip_financial: bool = False,
    tiers: set[int] | None = None,
    budget_per_company: float | None = None,
    budget_global: float | None = None,
    max_concurrent: int = 1,
):
    """Run the full pipeline across all (or specified) companies."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if budget_per_company is None:
        budget_per_company = BUDGET_PER_COMPANY
    if budget_global is None:
        budget_global = BUDGET_GLOBAL

    session = get_session(database_url)

    if company_ids:
        companies = session.query(Company).filter(Company.id.in_(company_ids)).all()
    else:
        companies = session.query(Company).all()

    log.info(f"Starting pipeline for {len(companies)} companies "
             f"(target years: {TARGET_START_YEAR}–{TARGET_END_YEAR})")
    log.info(f"  Budget: ${budget_per_company:.2f}/company, "
             f"${budget_global:.2f} global, "
             f"concurrency: {max_concurrent}")

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
    budget_stop = False

    def _process_one(company):
        """Process a single company with its own DB session (thread-safe)."""
        thread_session = get_session(database_url)
        thread_company = thread_session.query(Company).get(company.id)
        try:
            result = process_company(
                thread_company, anthropic_key, exa_key, llama_key, thread_session,
                skip_emissions=skip_emissions, skip_financial=skip_financial,
                tiers=tiers or {1, 2, 3},
                budget_per_company=budget_per_company,
                global_tracker=total_cost,
                budget_global=budget_global,
            )
            thread_session.commit()
            return result
        except BudgetExceeded as be:
            thread_session.commit()
            raise be
        except Exception as e:
            thread_session.rollback()
            raise e
        finally:
            thread_session.close()

    if max_concurrent > 1:
        # Concurrent processing
        futures = {}
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            for i, company in enumerate(companies):
                if budget_stop:
                    break
                log.info(f"[{i + 1}/{len(companies)}] Submitting {company.name}")
                future = executor.submit(_process_one, company)
                futures[future] = (i, company)

            for future in as_completed(futures):
                i, company = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                    cd = result.get("cost_detail", {})
                    # total_cost already updated by global_tracker in _TrackedMessages
                except BudgetExceeded as be:
                    log.warning(f"  Budget hit during {company.name}: {be}")
                    results.append({
                        "status": "budget_paused", "company": company.name,
                        "cost_detail": _new_cost_tracker(),
                    })
                    if _ask_continue(be.scope, be.spent, be.limit, company.name):
                        budget_per_company = be.limit * 2
                        budget_global = be.limit * 2
                        log.info(f"  Continuing with doubled limits: "
                                 f"${budget_per_company:.2f}/company, ${budget_global:.2f} global")
                    else:
                        budget_stop = True
                        executor.shutdown(wait=False, cancel_futures=True)
                except Exception as e:
                    log.error(f"  FAILED {company.name}: {e}")
                    errors.append(f"{company.name}: {e}")
                    results.append({"status": "failed", "company": company.name,
                                    "cost_detail": _new_cost_tracker()})
    else:
        # Sequential processing (original behaviour)
        for i, company in enumerate(companies):
            if budget_stop:
                break
            log.info(f"[{i + 1}/{len(companies)}] {company.name}")
            try:
                result = _process_one(company)
                results.append(result)
            except BudgetExceeded as be:
                log.warning(f"  Budget hit: {be}")
                results.append({
                    "status": "budget_paused", "company": company.name,
                    "cost_detail": _new_cost_tracker(),
                })
                if _ask_continue(be.scope, be.spent, be.limit, company.name):
                    budget_per_company = be.limit * 2
                    budget_global = be.limit * 2
                    log.info(f"  Continuing with doubled limits: "
                             f"${budget_per_company:.2f}/company, ${budget_global:.2f} global")
                else:
                    budget_stop = True
                    break
            except Exception as e:
                log.error(f"  FAILED: {e}")
                errors.append(f"{company.name}: {e}")
                results.append({"status": "failed", "company": company.name,
                                "cost_detail": _new_cost_tracker()})
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
    n_budget = sum(1 for r in results if r.get("status") == "budget_paused")

    run.successful = n_success
    run.failed = n_failed
    run.skipped = n_skipped
    run.completed_at = datetime.utcnow()
    run.status = "completed" if not budget_stop else "budget_stopped"
    run.error_log = "\n".join(errors) if errors else None
    session.commit()

    _print_pipeline_summary(results, total_cost, time.time() - t_pipeline_start)
    if budget_stop:
        log.info(f"  ⚠ Pipeline stopped by budget cap "
                 f"({len(companies) - len(results)} companies remaining)")

    return {
        "successful": n_success,
        "failed": n_failed,
        "skipped": n_skipped,
        "budget_paused": n_budget,
    }
