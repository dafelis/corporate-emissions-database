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


def cmd_init(args):
    """Initialise the database and load FTSE 100 companies with LEI lookup."""
    config = get_config()

    from db.models import create_tables, get_session, Company
    from data.ftse100 import FTSE_100
    from pipeline.lei_lookup import lookup_lei

    print("Creating database tables...")
    create_tables(config["DATABASE_URL"])

    # Add new LEI columns if they don't exist (migration for existing databases)
    from db.models import get_engine
    engine = get_engine(config["DATABASE_URL"])
    with engine.connect() as conn:
        for col, coltype in [
            ("lei_legal_name", "VARCHAR(500)"),
            ("lei_country", "VARCHAR(10)"),
            ("lei_confidence", "VARCHAR(20)"),
            ("lei_flag_reason", "TEXT"),
            ("lei_review_status", "VARCHAR(20) DEFAULT 'pending'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE companies ADD COLUMN {col} {coltype}")
                conn.commit()
            except Exception:
                conn.rollback()  # column already exists

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

    # Backfill LEIs for existing companies that don't have one
    if not args.skip_lei:
        no_lei = session.query(Company).filter(Company.lei.is_(None)).all()
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

    from pipeline.runner import run_pipeline

    company_ids = [args.id] if args.id else None

    run = run_pipeline(
        database_url=config["DATABASE_URL"],
        anthropic_key=config["ANTHROPIC_API_KEY"],
        exa_key=config["EXA_API_KEY"],
        llama_key=config["LLAMA_CLOUD_API_KEY"],
        company_ids=company_ids,
        delay_between=args.delay,
    )

    print(f"\nPipeline run complete:")
    print(f"  Successful: {run.successful}")
    print(f"  Failed:     {run.failed}")
    print(f"  Skipped:    {run.skipped}")


def cmd_check(args):
    """Run sanity checks on the extracted data."""
    config = get_config()

    from pipeline.checks import run_all_checks

    run_all_checks(config["DATABASE_URL"])


def cmd_status(args):
    """Show database status."""
    config = get_config()

    from db.models import get_session, Company, EmissionsRecord, PipelineRun

    session = get_session(config["DATABASE_URL"])

    n_companies = session.query(Company).count()
    n_records = session.query(EmissionsRecord).count()
    n_pending = session.query(EmissionsRecord).filter_by(review_status="pending").count()
    n_flagged = session.query(EmissionsRecord).filter_by(review_status="flagged").count()
    n_approved = session.query(EmissionsRecord).filter_by(review_status="approved").count()

    last_run = session.query(PipelineRun).order_by(PipelineRun.started_at.desc()).first()

    print(f"Companies:        {n_companies}")
    print(f"Emissions records: {n_records}")
    print(f"  Pending review: {n_pending}")
    print(f"  Flagged:        {n_flagged}")
    print(f"  Approved:       {n_approved}")

    if last_run:
        print(f"\nLast pipeline run: {last_run.started_at}")
        print(f"  Status: {last_run.status}")
        print(f"  Success/Failed/Skipped: {last_run.successful}/{last_run.failed}/{last_run.skipped}")


def main():
    parser = argparse.ArgumentParser(description="Corporate Emissions Database Pipeline")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise database and load companies")
    init_parser.add_argument("--skip-lei", action="store_true", help="Skip LEI lookup")

    # extract
    extract_parser = subparsers.add_parser("extract", help="Run extraction pipeline")
    extract_parser.add_argument("--id", type=int, help="Process a single company by ID")
    extract_parser.add_argument("--delay", type=float, default=2.0,
                                help="Seconds between companies (rate limiting)")

    # check
    subparsers.add_parser("check", help="Run sanity checks")

    # status
    subparsers.add_parser("status", help="Show database status")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    commands = {
        "init": cmd_init,
        "extract": cmd_extract,
        "check": cmd_check,
        "status": cmd_status,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
