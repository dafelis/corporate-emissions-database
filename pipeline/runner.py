"""
Main pipeline runner — processes companies one by one, extracting emissions,
financial data, market data, and industry classifications.
"""

import logging
import os
import tempfile
import time
import traceback
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


def process_company(
    company: Company,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    session,
) -> dict:
    """Process a single company: search, parse, extract, store.

    Pipeline:
    1. Search for sustainability report → extract emissions
    2. Search for annual report → extract financials (revenue, debt, cash)
    3. yfinance → equity value at fiscal year-end, industry info
    4. Claude → map industry to NAICS/NACE/SIC

    Returns a dict with status and details.
    """
    client = anthropic.Anthropic(api_key=anthropic_key)
    company_name = company.name
    log.info(f"Processing: {company_name}")

    # Skip if we already have emissions data for this company
    existing_emissions = session.query(EmissionsRecord).filter_by(company_id=company.id).count()
    existing_financials = session.query(FinancialRecord).filter_by(company_id=company.id).count()
    has_industry = company.yfinance_sector is not None

    if existing_emissions > 0 and existing_financials > 0 and has_industry:
        log.info(f"  Skipping: already have all data")
        return {"status": "skipped"}

    # ── PART 1: Emissions ──────────────────────────────────────────────────

    emissions_saved = 0
    if existing_emissions == 0:
        log.info("  Searching for emissions report...")
        try:
            search_result = search_for_emissions_source(company_name, anthropic_key, exa_key)
            url = search_result["url"]
            title = search_result["title"]
            source_type = detect_source_type(url)
            log.info(f"  Found emissions source: {title} ({source_type})")

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
                            log.warning(f"  Extraction failed for table {candidate['index']}: {e}")

            # Fallback: extract from page text
            if not extraction or not extraction.get("emissions"):
                if source_type == "html":
                    log.info("  No tables, trying text fallback")
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
                    record = EmissionsRecord(
                        company_id=company.id,
                        reporting_year=entry["reporting_year"],
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
                    emissions_saved += 1

                session.commit()
                log.info(f"  Saved {emissions_saved} emissions records")
            else:
                log.warning(f"  No emissions data found for {company_name}")

        except Exception as e:
            log.warning(f"  Emissions extraction failed: {e}")
            session.rollback()

    # ── PART 2: Financial data (separate search) ───────────────────────────

    financials_saved = 0
    if existing_financials == 0:
        log.info("  Searching for annual report...")
        try:
            fin_search = search_for_annual_report(company_name, anthropic_key, exa_key)
            fin_url = fin_search["url"]
            fin_title = fin_search["title"]
            fin_source_type = detect_source_type(fin_url)
            log.info(f"  Found financial source: {fin_title} ({fin_source_type})")

            fin_table_dicts, fin_tables_md = _parse_document(fin_url, fin_source_type, llama_key)

            if fin_tables_md:
                fin_ranked = find_financial_tables(fin_tables_md, client)
                fin_top = [r for r in fin_ranked if r["score"] >= 30]

                if fin_top:
                    for candidate in fin_top[:3]:
                        try:
                            fin_extraction = extract_financials(
                                fin_tables_md[candidate["index"]], company_name, client
                            )
                            if fin_extraction.get("financials"):
                                # Save financial source
                                fin_source = Source(
                                    company_id=company.id, url=fin_url, title=fin_title,
                                    document_type=fin_source_type,
                                )
                                session.add(fin_source)
                                session.flush()

                                for entry in fin_extraction["financials"]:
                                    multiplier = entry.get("unit_multiplier", 1) or 1
                                    fy_end = None
                                    if entry.get("fiscal_year_end"):
                                        try:
                                            fy_end = date_type.fromisoformat(entry["fiscal_year_end"])
                                        except (ValueError, TypeError):
                                            pass

                                    revenue = normalise_to_units(entry.get("revenue"), multiplier)
                                    debt = normalise_to_units(entry.get("outstanding_debt"), multiplier)
                                    cash = normalise_to_units(entry.get("cash_and_equivalents"), multiplier)

                                    fin_record = FinancialRecord(
                                        company_id=company.id,
                                        reporting_year=entry["reporting_year"],
                                        fiscal_year_end=fy_end,
                                        revenue=revenue,
                                        outstanding_debt=debt,
                                        cash_and_equivalents=cash,
                                        currency=entry.get("currency"),
                                        source_id=fin_source.id,
                                        confidence_score=fin_extraction.get("confidence_score"),
                                        review_status="pending",
                                    )
                                    session.add(fin_record)
                                    financials_saved += 1
                                break
                        except Exception as e:
                            log.warning(f"  Financial extraction failed for table {candidate['index']}: {e}")

                session.commit()
                log.info(f"  Saved {financials_saved} financial records")

        except Exception as e:
            log.warning(f"  Financial data extraction failed: {e}")
            session.rollback()

    # ── PART 3: Market data from yfinance ──────────────────────────────────

    if company.ticker and financials_saved > 0:
        log.info("  Fetching market data from yfinance...")
        try:
            fin_records = (
                session.query(FinancialRecord)
                .filter_by(company_id=company.id)
                .filter(FinancialRecord.equity_value.is_(None))
                .all()
            )
            for fr in fin_records:
                target_date = fr.fiscal_year_end or date_type(fr.reporting_year, 12, 31)
                equity_data = get_equity_value_at_date(company.ticker, target_date)
                if equity_data:
                    fr.equity_value = equity_data["market_cap"]
                    fr.shares_outstanding = equity_data["shares_outstanding"]
                    fr.share_price_at_fy_end = equity_data["share_price"]
                    fr.equity_currency = equity_data["currency"]
                    # Calculate EV if we have all components
                    if fr.outstanding_debt is not None and fr.cash_and_equivalents is not None:
                        fr.enterprise_value = fr.equity_value + fr.outstanding_debt - fr.cash_and_equivalents
                    log.info(f"  Market data for {fr.reporting_year}: "
                             f"equity={fr.equity_value:,.0f} {fr.equity_currency}")

            session.commit()
        except Exception as e:
            log.warning(f"  Market data fetch failed: {e}")
            session.rollback()

    elif company.ticker and existing_financials > 0:
        # Backfill market data for existing financial records missing equity value
        try:
            fin_records = (
                session.query(FinancialRecord)
                .filter_by(company_id=company.id)
                .filter(FinancialRecord.equity_value.is_(None))
                .all()
            )
            if fin_records:
                log.info("  Backfilling market data for existing financial records...")
                for fr in fin_records:
                    target_date = fr.fiscal_year_end or date_type(fr.reporting_year, 12, 31)
                    equity_data = get_equity_value_at_date(company.ticker, target_date)
                    if equity_data:
                        fr.equity_value = equity_data["market_cap"]
                        fr.shares_outstanding = equity_data["shares_outstanding"]
                        fr.share_price_at_fy_end = equity_data["share_price"]
                        fr.equity_currency = equity_data["currency"]
                        if fr.outstanding_debt is not None and fr.cash_and_equivalents is not None:
                            fr.enterprise_value = fr.equity_value + fr.outstanding_debt - fr.cash_and_equivalents
                session.commit()
        except Exception as e:
            log.warning(f"  Market data backfill failed: {e}")
            session.rollback()

    # ── PART 4: Industry classification ────────────────────────────────────

    if not has_industry and company.ticker:
        log.info("  Looking up industry classification...")
        try:
            industry_info = get_industry_info(company.ticker)
            if industry_info:
                company.yfinance_sector = industry_info["sector"]
                company.yfinance_industry = industry_info["industry"]
                log.info(f"  Industry: {industry_info['sector']} / {industry_info['industry']}")

                # Map to NAICS/NACE/SIC using Claude
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

    return {
        "status": "success",
        "emissions_records": emissions_saved,
        "financial_records": financials_saved,
    }


def run_pipeline(
    database_url: str,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    company_ids: list[int] = None,
    delay_between: float = 2.0,
):
    """Run the full pipeline across all (or specified) companies.

    Args:
        database_url: PostgreSQL connection string
        anthropic_key: Anthropic API key
        exa_key: Exa API key
        llama_key: LlamaParse API key
        company_ids: Optional list of company IDs to process (default: all)
        delay_between: Seconds to wait between companies (rate limiting)
    """
    session = get_session(database_url)

    # Load companies
    if company_ids:
        companies = session.query(Company).filter(Company.id.in_(company_ids)).all()
    else:
        companies = session.query(Company).all()

    log.info(f"Starting pipeline for {len(companies)} companies")

    # Create pipeline run record
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

        # Rate limiting
        if i < len(companies) - 1:
            time.sleep(delay_between)

        # Update run record periodically
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
