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
from pipeline.searcher import search_for_emissions_source, search_for_annual_report
from pipeline.parser import (
    parse_pdf, extract_tables_from_documents, parse_html, parse_excel,
    extract_html_text, detect_source_type, download_to_tempfile,
    render_pdf_page,
)
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
MAX_SEARCHES_PER_TYPE = 3  # max Exa searches per document type per company


def _current_year():
    return datetime.utcnow().year


def _parse_document(url, source_type, llama_key):
    """Parse a document and return table dicts + markdown list."""
    table_dicts = []
    if source_type == "pdf":
        documents = parse_pdf(url, llama_key)
        table_dicts = extract_tables_from_documents(documents)
    elif source_type == "excel":
        table_dicts = parse_excel(url)
    else:
        table_dicts = parse_html(url)
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


# ══════════════════════════════════════════════════════════════════════════
# Emissions extraction (iterative)
# ══════════════════════════════════════════════════════════════════════════

def _extract_emissions_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None,
):
    """Run one search→parse→extract cycle for emissions. Returns count saved."""
    saved = 0

    search_result = search_for_emissions_source(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
    )
    url = search_result["url"]
    title = search_result["title"]
    searched_urls.add(url)
    source_type = detect_source_type(url)
    log.info(f"    Found: {title} ({source_type})")

    table_dicts, tables_md = _parse_document(url, source_type, llama_key)

    extraction = None
    matched_table_idx = None
    if tables_md:
        ranked = find_emissions_tables(tables_md, client)
        top_tables = [r for r in ranked if r["score"] >= 30]
        if top_tables:
            for candidate in top_tables[:3]:
                try:
                    extraction = extract_emissions(
                        tables_md[candidate["index"]], company_name, client
                    )
                    if extraction.get("emissions"):
                        matched_table_idx = candidate["index"]
                        break
                except Exception as e:
                    log.warning(f"    Extraction failed for table {candidate['index']}: {e}")

    # Fallback: extract from page text
    if not extraction or not extraction.get("emissions"):
        if source_type == "html":
            log.info("    No tables, trying text fallback")
            page_text = extract_html_text(url)
            extraction = extract_emissions_from_text(page_text, company_name, client)

    if extraction and extraction.get("emissions"):
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

        for entry in extraction["emissions"]:
            year = entry["reporting_year"]

            # Skip years outside target range or already covered
            if year < TARGET_START_YEAR or year in covered_years:
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
                methodology_notes=extraction.get("methodology_notes", ""),
                source_id=source.id,
                confidence_score=extraction.get("confidence_score"),
                review_status="pending",
            )
            session.add(record)
            covered_years.add(year)
            saved += 1

        session.commit()

    return saved


# ══════════════════════════════════════════════════════════════════════════
# Financial extraction (iterative)
# ══════════════════════════════════════════════════════════════════════════

def _extract_financials_round(
    company, company_name, client, anthropic_key, exa_key, llama_key,
    session, covered_years, searched_urls, target_year=None,
):
    """Run one search→parse→extract cycle for financials. Returns count saved."""
    saved = 0

    fin_search = search_for_annual_report(
        company_name, anthropic_key, exa_key,
        target_year=target_year, exclude_urls=list(searched_urls),
    )
    url = fin_search["url"]
    title = fin_search["title"]
    searched_urls.add(url)
    source_type = detect_source_type(url)
    log.info(f"    Found: {title} ({source_type})")

    table_dicts, tables_md = _parse_document(url, source_type, llama_key)

    if not tables_md:
        return 0

    ranked = find_financial_tables(tables_md, client)
    top = [r for r in ranked if r["score"] >= 30]

    if not top:
        return 0

    for candidate in top[:3]:
        try:
            fin_extraction = extract_financials(
                tables_md[candidate["index"]], company_name, client
            )
            if fin_extraction.get("financials"):
                fin_source = Source(
                    company_id=company.id, url=url, title=title,
                    document_type=source_type,
                )
                session.add(fin_source)
                session.flush()

                for entry in fin_extraction["financials"]:
                    year = entry["reporting_year"]

                    # Skip years outside target range or already covered
                    if year < TARGET_START_YEAR or year in covered_years:
                        continue

                    multiplier = entry.get("unit_multiplier", 1) or 1

                    fin_record = FinancialRecord(
                        company_id=company.id,
                        reporting_year=year,
                        fiscal_year_end=_parse_date(entry.get("fiscal_year_end")),
                        period_start=_parse_date(entry.get("period_start")),
                        period_end=_parse_date(entry.get("period_end")),
                        revenue=normalise_to_units(entry.get("revenue"), multiplier),
                        outstanding_debt=normalise_to_units(entry.get("outstanding_debt"), multiplier),
                        cash_and_equivalents=normalise_to_units(entry.get("cash_and_equivalents"), multiplier),
                        currency=entry.get("currency"),
                        source_id=fin_source.id,
                        confidence_score=fin_extraction.get("confidence_score"),
                        review_status="pending",
                    )
                    session.add(fin_record)
                    covered_years.add(year)
                    saved += 1

                session.commit()
                break  # found a good table, stop trying others
        except Exception as e:
            log.warning(f"    Financial extraction failed for table {candidate['index']}: {e}")

    return saved


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
        while em_missing and search_count < MAX_SEARCHES_PER_TYPE:
            # First search: broad (latest). Subsequent: target oldest missing year.
            target_year = min(em_missing) if search_count > 0 else None
            year_label = f" (targeting {target_year})" if target_year else " (latest)"

            log.info(f"  Emissions search {search_count + 1}/{MAX_SEARCHES_PER_TYPE}{year_label}")
            try:
                saved = _extract_emissions_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, em_covered, em_searched_urls, target_year=target_year,
                )
                total_em_saved += saved
                em_missing = target - em_covered
                if saved == 0:
                    break  # search found nothing new, stop
            except Exception as e:
                log.warning(f"    Emissions search failed: {e}")
                session.rollback()
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
    fin_missing = target - fin_covered
    fin_searched_urls = set()
    total_fin_saved = 0

    if fin_missing:
        log.info(f"  Financials: have {sorted(fin_covered) or 'none'}, "
                 f"missing {sorted(fin_missing)}")

        search_count = 0
        while fin_missing and search_count < MAX_SEARCHES_PER_TYPE:
            target_year = min(fin_missing) if search_count > 0 else None
            year_label = f" (targeting {target_year})" if target_year else " (latest)"

            log.info(f"  Financial search {search_count + 1}/{MAX_SEARCHES_PER_TYPE}{year_label}")
            try:
                saved = _extract_financials_round(
                    company, company_name, client, anthropic_key, exa_key, llama_key,
                    session, fin_covered, fin_searched_urls, target_year=target_year,
                )
                total_fin_saved += saved
                fin_missing = target - fin_covered
                if saved == 0:
                    break
            except Exception as e:
                log.warning(f"    Financial search failed: {e}")
                session.rollback()
                break

            search_count += 1

        if total_fin_saved:
            log.info(f"  Financials: saved {total_fin_saved} records "
                     f"(covering {sorted(fin_covered & target)})")
        still_missing = target - fin_covered
        if still_missing:
            log.info(f"  Financials: no data found for years {sorted(still_missing)}")

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
