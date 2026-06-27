"""
Main pipeline runner — processes companies one by one, extracting emissions data.
"""

import logging
import os
import tempfile
import time
import traceback
from datetime import datetime

import anthropic

from db.models import Company, EmissionsRecord, Source, PipelineRun, get_session, create_tables
from pipeline.searcher import search_for_emissions_source
from pipeline.parser import (
    parse_pdf, extract_tables_from_documents, parse_html, parse_excel,
    extract_html_text, detect_source_type, download_to_tempfile,
)
from pipeline.extractor import find_emissions_tables, extract_emissions, extract_emissions_from_text
from pipeline.storage import upload_file

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


def process_company(
    company: Company,
    anthropic_key: str,
    exa_key: str,
    llama_key: str,
    session,
) -> dict:
    """Process a single company: search, parse, extract, store.

    Returns a dict with status and details.
    """
    client = anthropic.Anthropic(api_key=anthropic_key)
    company_name = company.name
    log.info(f"Processing: {company_name}")

    # Step 1: Search for emissions source
    search_result = search_for_emissions_source(company_name, anthropic_key, exa_key)
    url = search_result["url"]
    title = search_result["title"]
    source_type = detect_source_type(url)
    log.info(f"  Found source: {title} ({source_type})")

    # Step 2: Parse the document
    tables = []
    if source_type == "pdf":
        documents = parse_pdf(url, llama_key)
        tables = extract_tables_from_documents(documents)
    elif source_type == "excel":
        tables = parse_excel(url)
    else:
        tables = parse_html(url)

    # Step 3: Find and extract emissions data
    extraction = None
    if tables:
        ranked = find_emissions_tables(tables, client)
        top_tables = [r for r in ranked if r["score"] >= 30]

        if top_tables:
            # Try top-ranked tables until we get valid data
            for candidate in top_tables[:3]:
                try:
                    extraction = extract_emissions(
                        tables[candidate["index"]], company_name, client
                    )
                    if extraction.get("emissions"):
                        break
                except Exception as e:
                    log.warning(f"  Extraction failed for table {candidate['index']}: {e}")
                    continue

    # Fallback: extract from page text
    if not extraction or not extraction.get("emissions"):
        if source_type == "html":
            log.info("  No tables found, trying text extraction fallback")
            page_text = extract_html_text(url)
            extraction = extract_emissions_from_text(page_text, company_name, client)

    if not extraction or not extraction.get("emissions"):
        raise ValueError(f"No emissions data found for {company_name}")

    # Step 4: Store source document in S3
    s3_pdf_key = None
    if source_type == "pdf":
        try:
            local_path = download_to_tempfile(url)
            safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
            s3_key = f"sources/{safe_name}/report.pdf"
            s3_pdf_key = upload_file(local_path, s3_key)
            os.unlink(local_path)
        except Exception as e:
            log.warning(f"  Failed to upload PDF to S3: {e}")

    # Step 5: Save to database
    source = Source(
        company_id=company.id,
        url=url,
        title=title,
        document_type=source_type,
        s3_pdf_key=s3_pdf_key,
    )
    session.add(source)
    session.flush()

    records_saved = 0
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
        records_saved += 1

    session.commit()
    log.info(f"  Saved {records_saved} emissions records")

    return {
        "status": "success",
        "records": records_saved,
        "source_url": url,
        "confidence": extraction.get("confidence_score"),
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

    for i, company in enumerate(companies):
        log.info(f"[{i + 1}/{len(companies)}] {company.name}")
        try:
            result = process_company(company, anthropic_key, exa_key, llama_key, session)
            run.successful += 1
        except Exception as e:
            log.error(f"  FAILED: {e}")
            errors.append(f"{company.name}: {e}")
            run.failed += 1
            session.rollback()

        # Rate limiting
        if i < len(companies) - 1:
            time.sleep(delay_between)

        # Update run record periodically
        if (i + 1) % 10 == 0:
            session.commit()

    run.completed_at = datetime.utcnow()
    run.status = "completed"
    run.error_log = "\n".join(errors) if errors else None
    session.commit()

    log.info(
        f"Pipeline complete: {run.successful} succeeded, "
        f"{run.failed} failed, {run.skipped} skipped"
    )

    return run
