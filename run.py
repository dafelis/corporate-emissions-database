"""
Main entry point for the corporate emissions database pipeline.

Usage:
    python run.py init          # Initialise database and load FTSE 100 companies
    python run.py extract       # Run extraction for all companies
    python run.py extract --id 5  # Run extraction for a single company
    python run.py check         # Run sanity checks and flag issues
    python run.py status        # Show pipeline status
"""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def get_config():
    """Load configuration from environment variables."""
    required = {
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY"),
        "EXA_API_KEY": os.environ.get("EXA_API_KEY"),
        "LLAMA_CLOUD_API_KEY": os.environ.get("LLAMA_CLOUD_API_KEY"),
    }

    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"Error: missing environment variables: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in the values.")
        sys.exit(1)

    return required


def _terminate_other_connections(database_url: str):
    """Kill other connections to the database to avoid lock contention.

    This prevents 'CREATE TABLE' and 'ALTER TABLE' from hanging when
    Streamlit (or another process) holds open connections.
    """
    from sqlalchemy import text
    from db.models import get_engine

    # Connect to the 'postgres' maintenance database to terminate others
    admin_url = database_url.rsplit("/", 1)[0] + "/postgres"
    db_name = database_url.rsplit("/", 1)[1].split("?")[0]

    try:
        engine = get_engine(admin_url)
        with engine.connect() as conn:
            result = conn.execute(text(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity "
                "WHERE datname = :db AND pid <> pg_backend_pid()"
            ), {"db": db_name})
            terminated = sum(1 for row in result if row[0])
            if terminated:
                print(f"  Terminated {terminated} blocking connection(s)")
            conn.commit()
        engine.dispose()
    except Exception as e:
        print(f"  Warning: could not clear connections: {e}")
        print("  If init hangs, manually run: sudo -u postgres psql -c \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'emissions' AND pid <> pg_backend_pid();\"")


_MIGRATIONS = [
    # Companies table — LEI fields
    ("companies", "lei_legal_name", "VARCHAR(500)"),
    ("companies", "lei_country", "VARCHAR(10)"),
    ("companies", "lei_confidence", "VARCHAR(20)"),
    ("companies", "lei_flag_reason", "TEXT"),
    ("companies", "lei_review_status", "VARCHAR(20) DEFAULT 'pending'"),
    # Companies table — industry classification
    ("companies", "yfinance_sector", "VARCHAR(200)"),
    ("companies", "yfinance_industry", "VARCHAR(200)"),
    ("companies", "sic_code", "VARCHAR(20)"),
    ("companies", "sic_description", "VARCHAR(500)"),
    ("companies", "naics_code", "VARCHAR(20)"),
    ("companies", "naics_description", "VARCHAR(500)"),
    ("companies", "nace_code", "VARCHAR(20)"),
    ("companies", "nace_description", "VARCHAR(500)"),
    ("companies", "industry_review_status", "VARCHAR(20) DEFAULT 'pending'"),
    # Emissions table — reporting period
    ("emissions_records", "period_start", "DATE"),
    ("emissions_records", "period_end", "DATE"),
    # Financial table — reporting period
    ("financial_records", "period_start", "DATE"),
    ("financial_records", "period_end", "DATE"),
    # Financial table — PCAF EVIC fields
    ("financial_records", "units", "VARCHAR(20)"),
    ("financial_records", "gross_debt", "DOUBLE PRECISION"),
    ("financial_records", "gross_debt_components", "TEXT"),
    ("financial_records", "gross_debt_ref", "TEXT"),
    ("financial_records", "gross_debt_confidence", "VARCHAR(10)"),
    ("financial_records", "lease_liabilities", "DOUBLE PRECISION"),
    ("financial_records", "lease_liabilities_ref", "TEXT"),
    ("financial_records", "lease_liabilities_confidence", "VARCHAR(10)"),
    ("financial_records", "non_controlling_interests", "DOUBLE PRECISION"),
    ("financial_records", "nci_ref", "TEXT"),
    ("financial_records", "nci_confidence", "VARCHAR(10)"),
    ("financial_records", "preference_shares", "DOUBLE PRECISION"),
    ("financial_records", "preference_shares_classification", "VARCHAR(20)"),
    ("financial_records", "preference_shares_listed", "BOOLEAN"),
    ("financial_records", "preference_shares_ref", "TEXT"),
    ("financial_records", "shares_outstanding_share_class", "VARCHAR(100)"),
    ("financial_records", "shares_outstanding_ref", "TEXT"),
    ("financial_records", "shares_outstanding_confidence", "VARCHAR(10)"),
    ("financial_records", "revenue_label", "VARCHAR(200)"),
    ("financial_records", "revenue_ref", "TEXT"),
    ("financial_records", "revenue_confidence", "VARCHAR(10)"),
    ("financial_records", "is_financial_institution", "BOOLEAN"),
    ("financial_records", "evic", "DOUBLE PRECISION"),
    ("financial_records", "source_tier", "INTEGER"),
    ("financial_records", "source_type", "VARCHAR(50)"),
    ("financial_records", "methodology_notes", "TEXT"),
    ("financial_records", "validation_flags", "TEXT"),
    ("financial_records", "extraction_notes", "TEXT"),
    ("financial_records", "market_data_source_id", "INTEGER REFERENCES sources(id)"),
    # Sources table — preview fields
    ("sources", "screenshot_path", "TEXT"),
    ("sources", "html_snippet", "TEXT"),
]


def _run_migrations(database_url):
    from sqlalchemy import text
    from db.models import get_engine

    _terminate_other_connections(database_url)
    engine = get_engine(database_url)
    added = 0
    for table, col, coltype in _MIGRATIONS:
        with engine.connect() as conn:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                conn.commit()
                print(f"  Added column: {col}")
                added += 1
            except Exception:
                conn.rollback()
    if added:
        print(f"  {added} new column(s) added")


def cmd_init(args):
    """Initialise the database and load FTSE 100 companies with LEI lookup."""
    config = get_config()

    from db.models import create_tables, get_session, Company
    from data.ftse100 import FTSE_100
    from pipeline.lei_lookup import lookup_lei

    print("Creating database tables...")
    _terminate_other_connections(config["DATABASE_URL"])
    create_tables(config["DATABASE_URL"])

    _run_migrations(config["DATABASE_URL"])

    session = get_session(config["DATABASE_URL"])
    existing = {c.name for c in session.query(Company).all()}

    added = 0
    for entry in FTSE_100:
        if entry["name"] in existing:
            print(f"  Skipping {entry['name']} (already exists)")
            continue

        # Look up LEI
        lei_result = None
        if not args.skip_lei:
            try:
                lei_result = lookup_lei(entry["name"])
                if lei_result:
                    flag = f" ⚑ {lei_result['flag_reason']}" if lei_result.get("flag_reason") else ""
                    print(f"  {entry['name']} -> LEI: {lei_result['lei']} "
                          f"({lei_result['legal_name']}, {lei_result['country']}) "
                          f"[{lei_result['confidence']}]{flag}")
                else:
                    print(f"  {entry['name']} -> LEI not found")
            except Exception as e:
                print(f"  {entry['name']} -> LEI lookup failed: {e}")

        company = Company(
            name=entry["name"],
            ticker=entry.get("ticker"),
            lei=lei_result["lei"] if lei_result else None,
            lei_legal_name=lei_result["legal_name"] if lei_result else None,
            lei_country=lei_result["country"] if lei_result else None,
            lei_confidence=lei_result["confidence"] if lei_result else None,
            lei_flag_reason=lei_result.get("flag_reason") if lei_result else None,
            lei_review_status="approved" if lei_result and lei_result["confidence"] == "high" else "pending",
            index_membership="FTSE100",
        )
        session.add(company)
        added += 1

    session.commit()
    print(f"\nAdded {added} companies to database ({len(existing)} already existed)")

    # Backfill LEIs for existing companies that don't have one (or have LEI but missing metadata)
    if not args.skip_lei:
        from sqlalchemy import or_
        no_lei = session.query(Company).filter(
            or_(Company.lei.is_(None), Company.lei_legal_name.is_(None))
        ).all()
        if no_lei:
            print(f"\nBackfilling LEIs for {len(no_lei)} companies...")
            for company in no_lei:
                try:
                    result = lookup_lei(company.name)
                    if result:
                        company.lei = result["lei"]
                        company.lei_legal_name = result["legal_name"]
                        company.lei_country = result["country"]
                        company.lei_confidence = result["confidence"]
                        company.lei_flag_reason = result.get("flag_reason")
                        company.lei_review_status = (
                            "approved" if result["confidence"] == "high" else "pending"
                        )
                        flag = f" ⚑ {result['flag_reason']}" if result.get("flag_reason") else ""
                        print(f"  {company.name} -> LEI: {result['lei']} "
                              f"({result['legal_name']}, {result['country']}) "
                              f"[{result['confidence']}]{flag}")
                    else:
                        print(f"  {company.name} -> LEI not found")
                except Exception as e:
                    print(f"  {company.name} -> LEI lookup failed: {e}")
            session.commit()
            print("LEI backfill complete")


def cmd_extract(args):
    """Run the extraction pipeline."""
    config = get_config()

    _run_migrations(config["DATABASE_URL"])

    from pipeline.runner import run_pipeline

    company_ids = None
    if args.id:
        company_ids = [args.id]
    elif args.ids:
        company_ids = []
        for part in args.ids.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                company_ids.extend(range(int(lo), int(hi) + 1))
            else:
                company_ids.append(int(part))
        company_ids = sorted(set(company_ids))

    # If neither --emissions nor --financial given, run both
    skip_emissions = args.financial and not args.emissions
    skip_financial = args.emissions and not args.financial

    # Tier selection: if any --tierN flag is set, run only those tiers.
    # If none (or --tierall), run all tiers.
    tiers = set()
    if args.tier1:
        tiers.add(1)
    if args.tier2:
        tiers.add(2)
    if args.tier3:
        tiers.add(3)
    if not tiers or args.tierall:
        tiers = {1, 2, 3}

    run = run_pipeline(
        database_url=config["DATABASE_URL"],
        anthropic_key=config["ANTHROPIC_API_KEY"],
        exa_key=config["EXA_API_KEY"],
        llama_key=config["LLAMA_CLOUD_API_KEY"],
        company_ids=company_ids,
        delay_between=args.delay,
        skip_emissions=skip_emissions,
        skip_financial=skip_financial,
        tiers=tiers,
        budget_per_company=args.company_budget,
        budget_global=args.budget,
        max_concurrent=args.concurrent,
    )

    print(f"\nPipeline run complete:")
    print(f"  Successful: {run['successful']}")
    print(f"  Failed:     {run['failed']}")
    print(f"  Skipped:    {run['skipped']}")
    if run.get("budget_paused"):
        print(f"  Budget paused: {run['budget_paused']}")


def cmd_check(args):
    """Run sanity checks on the extracted data."""
    config = get_config()

    from pipeline.checks import run_all_checks

    run_all_checks(config["DATABASE_URL"])


def cmd_status(args):
    """Show database status."""
    config = get_config()

    from db.models import get_session, Company, EmissionsRecord, FinancialRecord, PipelineRun

    session = get_session(config["DATABASE_URL"])

    n_companies = session.query(Company).count()
    n_records = session.query(EmissionsRecord).count()
    n_pending = session.query(EmissionsRecord).filter_by(review_status="pending").count()
    n_flagged = session.query(EmissionsRecord).filter_by(review_status="flagged").count()
    n_approved = session.query(EmissionsRecord).filter_by(review_status="approved").count()

    last_run = session.query(PipelineRun).order_by(PipelineRun.started_at.desc()).first()

    n_fin_records = session.query(FinancialRecord).count()
    n_classified = session.query(Company).filter(Company.naics_code.isnot(None)).count()

    print(f"Companies:          {n_companies}")
    print(f"  With industry:    {n_classified}")
    print(f"Emissions records:  {n_records}")
    print(f"  Pending review:   {n_pending}")
    print(f"  Flagged:          {n_flagged}")
    print(f"  Approved:         {n_approved}")
    print(f"Financial records:  {n_fin_records}")

    if last_run:
        print(f"\nLast pipeline run: {last_run.started_at}")
        print(f"  Status: {last_run.status}")
        print(f"  Success/Failed/Skipped: {last_run.successful}/{last_run.failed}/{last_run.skipped}")


def cmd_reset(args):
    """Delete data for a company (so it can be re-extracted).

    With no category flags, resets everything. With one or more of
    --emissions, --financial, --industry, resets only those categories.
    Orphaned sources (no longer referenced by any record) are cleaned up
    automatically.
    """
    config = get_config()

    from db.models import get_session, Company, EmissionsRecord, FinancialRecord, Source

    _run_migrations(config["DATABASE_URL"])
    session = get_session(config["DATABASE_URL"])

    if args.id:
        company = session.query(Company).get(args.id)
        if not company:
            print(f"Company with ID {args.id} not found")
            sys.exit(1)
        companies = [company]
    elif args.all:
        companies = session.query(Company).all()
    else:
        print("Specify --id <company_id> or --all")
        sys.exit(1)

    # If no category flags given, reset everything (backwards compatible)
    reset_all = not (args.emissions or args.financial or args.industry)

    for company in companies:
        parts = []

        if reset_all or args.emissions:
            n = session.query(EmissionsRecord).filter_by(company_id=company.id).delete()
            parts.append(f"{n} emissions")

        if reset_all or args.financial:
            n = session.query(FinancialRecord).filter_by(company_id=company.id).delete()
            parts.append(f"{n} financial")

        if reset_all or args.industry:
            company.yfinance_sector = None
            company.yfinance_industry = None
            company.sic_code = None
            company.naics_code = None
            company.nace_code = None
            parts.append("industry")

        # Clean up orphaned sources for this company
        referenced_by_emissions = (
            session.query(EmissionsRecord.source_id)
            .filter(EmissionsRecord.company_id == company.id,
                    EmissionsRecord.source_id.isnot(None))
        )
        referenced_by_financial = (
            session.query(FinancialRecord.source_id)
            .filter(FinancialRecord.company_id == company.id,
                    FinancialRecord.source_id.isnot(None))
        )
        referenced_by_market = (
            session.query(FinancialRecord.market_data_source_id)
            .filter(FinancialRecord.company_id == company.id,
                    FinancialRecord.market_data_source_id.isnot(None))
        )
        referenced_ids = (
            {r[0] for r in referenced_by_emissions}
            | {r[0] for r in referenced_by_financial}
            | {r[0] for r in referenced_by_market}
        )

        orphaned = (
            session.query(Source)
            .filter(
                Source.company_id == company.id,
                ~Source.id.in_(referenced_ids) if referenced_ids else Source.id.isnot(None),
            )
            .all()
        )
        if orphaned:
            for s in orphaned:
                session.delete(s)
            parts.append(f"{len(orphaned)} orphaned sources")

        print(f"  {company.name}: deleted {', '.join(parts)}")

    session.commit()
    print("Reset complete")


def main():
    parser = argparse.ArgumentParser(description="Corporate Emissions Database Pipeline")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise database and load companies")
    init_parser.add_argument("--skip-lei", action="store_true", help="Skip LEI lookup")

    # extract
    extract_parser = subparsers.add_parser("extract", help="Run extraction pipeline")
    extract_parser.add_argument("--id", type=int, help="Process a single company by ID")
    extract_parser.add_argument("--ids", type=str,
                                help="Company IDs: comma-separated (1,2,3) or range (1-20) or both (1-10,15,20)")
    extract_parser.add_argument("--delay", type=float, default=2.0,
                                help="Seconds between companies (rate limiting)")
    extract_parser.add_argument("--emissions", action="store_true",
                                help="Extract emissions data only")
    extract_parser.add_argument("--financial", action="store_true",
                                help="Extract financial data only")
    extract_parser.add_argument("--tier1", action="store_true",
                                help="Financial: run only Tier 1 (XBRL APIs)")
    extract_parser.add_argument("--tier2", action="store_true",
                                help="Financial: run only Tier 2 (yfinance)")
    extract_parser.add_argument("--tier3", action="store_true",
                                help="Financial: run only Tier 3 (Exa + PDF + LLM)")
    extract_parser.add_argument("--tierall", action="store_true",
                                help="Financial: run all tiers (default)")
    extract_parser.add_argument("--budget", type=float, default=None,
                                help="Global budget cap in USD (default: $20)")
    extract_parser.add_argument("--company-budget", type=float, default=None,
                                help="Per-company budget cap in USD (default: $5)")
    extract_parser.add_argument("--concurrent", type=int, default=1,
                                help="Max companies to process concurrently (default: 1)")

    # check
    subparsers.add_parser("check", help="Run sanity checks")

    # status
    subparsers.add_parser("status", help="Show database status")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Delete data for re-extraction")
    reset_parser.add_argument("--id", type=int, help="Reset a single company by ID")
    reset_parser.add_argument("--all", action="store_true", help="Reset all companies")
    reset_parser.add_argument("--emissions", action="store_true", help="Delete emissions data only")
    reset_parser.add_argument("--financial", action="store_true", help="Delete financial data only")
    reset_parser.add_argument("--industry", action="store_true", help="Reset industry classification")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "init": cmd_init,
        "extract": cmd_extract,
        "check": cmd_check,
        "status": cmd_status,
        "reset": cmd_reset,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
