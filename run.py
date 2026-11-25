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
    # Companies table — universe provenance
    ("companies", "isin", "VARCHAR(12)"),
    ("companies", "country", "VARCHAR(10)"),
    ("companies", "exchange", "VARCHAR(100)"),
    ("companies", "is_financial_institution", "BOOLEAN"),
    ("companies", "fi_basis", "VARCHAR(200)"),
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
                lei_result = lookup_lei(entry["name"], entry.get("ticker"))
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
                    result = lookup_lei(company.name, company.ticker)
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

    # --free-only drops Tier 3, the only financial tier that calls an LLM.
    if args.free_only:
        tiers &= {1, 2}
        print("Free-only mode: Tier S emissions + XBRL/yfinance financials. "
              "No API spend.")

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
        free_only=args.free_only,
    )

    print(f"\nPipeline run complete:")
    print(f"  Successful: {run['successful']}")
    print(f"  Failed:     {run['failed']}")
    print(f"  Skipped:    {run['skipped']}")
    if run.get("budget_paused"):
        print(f"  Budget paused: {run['budget_paused']}")


def cmd_fi(args):
    """Settle PCAF financial-institution status per company and recompute EVIC.

    FI status used to be decided per record by whichever tier supplied that
    year — Tier 1/2 hardcode False, Tier 3 asks the model — so a bank could
    have EVIC for some years and not others. Decide once per company from its
    industry classification and apply it to every year.
    """
    config = get_config()
    from db.models import get_session, Company, FinancialRecord
    from pipeline.financial_validator import determine_fi_status, compute_evic

    session = get_session(config["DATABASE_URL"])
    if args.id:
        companies = [session.get(Company, args.id)]
    else:
        companies = session.query(Company).order_by(Company.id).all()

    n_fi = withdrawn = added = 0
    for company in companies:
        if company is None:
            continue
        records = session.query(FinancialRecord).filter_by(company_id=company.id).all()
        if args.set is not None and args.id:
            is_fi, basis = (args.set == "yes"), "set manually"
        else:
            is_fi, basis = determine_fi_status(company, records)

        company.is_financial_institution = is_fi
        company.fi_basis = basis
        if is_fi:
            n_fi += 1

        changes = []
        for r in records:
            before = r.evic
            compute_evic(r, company)
            r.is_financial_institution = is_fi   # keep the record in step
            if before is not None and r.evic is None:
                withdrawn += 1
                changes.append(f"{r.reporting_year} withdrawn")
            elif before is None and r.evic is not None:
                added += 1
                changes.append(f"{r.reporting_year} added")
        if is_fi or changes:
            note = f" — EVIC {', '.join(changes)}" if changes else ""
            print(f"  {company.id:3d} {company.name:32s} FI={is_fi}  [{basis}]{note}")

    if not args.dry_run:
        session.commit()
    print(f"\n{n_fi} of {len(companies)} companies are PCAF financial institutions; "
          f"EVIC withdrawn from {withdrawn} record(s), added to {added}"
          + (" (dry run — nothing written)" if args.dry_run else ""))


def cmd_tickers(args):
    """Check every stored ticker still returns prices, and sync from the list.

    Tickers die on renames, mergers and listing moves — the same corporate
    actions that break LEIs. A dead ticker silently costs equity_value and
    therefore EVIC for every year of that company.
    """
    config = get_config()
    import warnings
    warnings.filterwarnings("ignore")
    from db.models import get_session, Company
    from data.ftse100 import FTSE_100
    import yfinance as yf

    import datetime as _dt
    session = get_session(config["DATABASE_URL"])
    listed = {e["name"]: e.get("ticker") for e in FTSE_100}
    synced = dead = ok = 0
    # A recent window: a newly listed line (e.g. SUNB.L from Mar 2026) has no
    # history before it existed, and a dead line has none now.
    _end = _dt.date.today()
    _start = _end - _dt.timedelta(days=30)

    for company in session.query(Company).order_by(Company.id).all():
        want = listed.get(company.name)
        if want and want != company.ticker:
            print(f"  {company.id:3d} {company.name}: ticker {company.ticker} -> {want} (from list)")
            synced += 1
            if not args.dry_run:
                company.ticker = want
            company.ticker = want  # check the new one below either way

        if not company.ticker:
            print(f"  {company.id:3d} {company.name}: no ticker")
            dead += 1
            continue
        try:
            hist = yf.Ticker(company.ticker).history(
                start=_start.isoformat(), end=_end.isoformat(), auto_adjust=False)
        except Exception:
            hist = []
        if len(hist):
            ok += 1
        else:
            dead += 1
            print(f"  {company.id:3d} {company.name}: NO PRICE DATA for {company.ticker}")

    if not args.dry_run:
        session.commit()
    print(f"\nTickers: {ok} live, {dead} dead, {synced} synced from data/ftse100.py"
          + (" (dry run — nothing written)" if args.dry_run else ""))


def cmd_lei(args):
    """Re-resolve LEIs (ISIN-first) and report every change.

    A wrong LEI silently disables every regulatory data source, and the old
    name-only lookup matched pension schemes, share plans and subsidiaries
    for about one company in ten.
    """
    config = get_config()
    from db.models import get_session, Company
    from pipeline.lei_lookup import lookup_lei

    session = get_session(config["DATABASE_URL"])

    if args.set:
        if not args.id:
            print("--set requires --id")
            sys.exit(1)
        import httpx
        company = session.get(Company, args.id)
        resp = httpx.get(f"https://api.gleif.org/api/v1/lei-records/{args.set}", timeout=30)
        resp.raise_for_status()
        entity = resp.json()["data"]["attributes"]["entity"]
        company.lei = args.set
        company.lei_legal_name = entity["legalName"]["name"]
        company.lei_country = entity.get("legalAddress", {}).get("country")
        company.lei_confidence = "high"
        company.lei_flag_reason = "set manually"
        company.lei_review_status = "approved"
        session.commit()
        print(f"  {company.id:3d} {company.name}: set to {args.set} ({company.lei_legal_name}, {company.lei_country})")
        return

    if args.id:
        companies = [session.get(Company, args.id)]
    else:
        companies = session.query(Company).order_by(Company.id).all()

    changed = unchanged = failed = 0
    for company in companies:
        if company is None:
            continue
        try:
            result = lookup_lei(company.name, company.ticker)
        except Exception as e:
            failed += 1
            print(f"  {company.id:3d} {company.name}: lookup failed: {e}")
            continue
        if not result:
            failed += 1
            print(f"  {company.id:3d} {company.name}: no plausible LEI")
            continue

        method = result.get("method", "name")
        if result["lei"] == company.lei:
            unchanged += 1
            if args.verbose:
                print(f"  {company.id:3d} {company.name}: unchanged {company.lei} "
                      f"({result['legal_name']}) via {method}")
            continue

        changed += 1
        print(f"  {company.id:3d} {company.name}: {company.lei} ({company.lei_legal_name}) "
              f"-> {result['lei']} ({result['legal_name']}) via {method}")
        if not args.dry_run:
            company.lei = result["lei"]
            company.lei_legal_name = result["legal_name"]
            company.lei_country = result["country"]
            company.lei_confidence = result["confidence"]
            company.lei_flag_reason = result.get("flag_reason")
            company.lei_review_status = "approved" if result["confidence"] == "high" else "pending"

    if not args.dry_run:
        session.commit()
    print(f"\nLEI refresh: {changed} changed, {unchanged} unchanged, {failed} unresolved"
          + (" (dry run — nothing written)" if args.dry_run else ""))


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
    elif args.ids:
        id_list = []
        for part in args.ids.split(","):
            part = part.strip()
            if "-" in part:
                lo, hi = part.split("-", 1)
                id_list.extend(range(int(lo), int(hi) + 1))
            else:
                id_list.append(int(part))
        companies = session.query(Company).filter(Company.id.in_(sorted(set(id_list)))).all()
        if not companies:
            print(f"No companies found for IDs: {args.ids}")
            sys.exit(1)
    elif args.all:
        companies = session.query(Company).all()
    else:
        print("Specify --id <company_id>, --ids <range>, or --all")
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


# The universe files scripts/build_universe.py writes, and the
# index_membership tag each one's rows get. The tag is what lets a later run
# target one market without hunting through ids.
_UNIVERSE_SOURCES = {
    "esma": ("universe_esma.csv", "UNIV-EU"),
    "sec":  ("universe_sec.csv",  "UNIV-US"),
    "nse":  ("universe_nse.csv",  "UNIV-IN"),
    "twse": ("universe_twse.csv", "UNIV-TW"),
    "hkex": ("universe_hkex.csv", "UNIV-HK"),
    "jpx":  ("universe_jpx.csv",  "UNIV-JP"),
    "asx":  ("universe_asx.csv",  "UNIV-AU"),
    "tsx":  ("universe_tsx.csv",  "UNIV-CA"),
    "b3":   ("universe_b3.csv",   "UNIV-BR"),
    "sse":  ("universe_sse.csv",  "UNIV-CN"),
    "szse": ("universe_szse.csv", "UNIV-CNSZ"),
    "six":  ("universe_six.csv",  "UNIV-CH"),
    "krx":  ("universe_krx.csv",  "UNIV-KR"),
}

# Markets whose emissions already come as structured data, so a --free-only
# run over them actually returns figures rather than nothing.
_TIER_S_SOURCES = ("twse", "nse")


def cmd_load_universe(args):
    """Load companies from the CSVs that scripts/build_universe.py writes.

    Deliberately does no LEI lookup: outside the UK/EU the LEI is not the
    key, and resolving tens of thousands of them would take days and mostly
    fail. Rows keep whatever LEI and ISIN their source supplied, and
    `run.py lei` can fill gaps later for the markets where it helps.
    """
    import csv as _csv

    config = get_config()
    _run_migrations(config["DATABASE_URL"])

    from db.models import get_session, Company

    if args.sources == "tier-s":
        keys = list(_TIER_S_SOURCES)
    elif args.sources == "all":
        keys = list(_UNIVERSE_SOURCES)
    else:
        keys = [k.strip().lower() for k in args.sources.split(",") if k.strip()]

    unknown = [k for k in keys if k not in _UNIVERSE_SOURCES]
    if unknown:
        print(f"Unknown source(s): {', '.join(unknown)}")
        print(f"Known: {', '.join(sorted(_UNIVERSE_SOURCES))} — or 'all' / 'tier-s'")
        return

    session = get_session(config["DATABASE_URL"])

    # One pass over what is already stored, so re-running is idempotent.
    seen_tickers, seen_isins, seen_names, used_leis = set(), set(), set(), set()
    for c in session.query(Company).all():
        if c.ticker:
            seen_tickers.add(c.ticker.strip().upper())
        if getattr(c, "isin", None):
            seen_isins.add(c.isin.strip().upper())
        if c.name:
            seen_names.add(c.name.strip().lower())
        if c.lei:
            used_leis.add(c.lei.strip().upper())
    print(f"Already in database: {len(seen_names):,} companies")

    grand = {"added": 0, "dupe": 0, "no_ticker": 0, "no_name": 0}
    for key in keys:
        fname, default_tag = _UNIVERSE_SOURCES[key]
        path = os.path.join(args.dir, fname)
        if not os.path.exists(path):
            print(f"\n{key}: {path} not found — "
                  f"run scripts/build_universe.py --source {key} first")
            continue
        tag = args.tag or default_tag

        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(_csv.DictReader(fh))
        if args.limit:
            rows = rows[:args.limit]

        stats = {"added": 0, "dupe": 0, "no_ticker": 0, "no_name": 0}
        pending = 0
        for r in rows:
            name = (r.get("name") or "").strip()
            if not name:
                stats["no_name"] += 1
                continue
            ticker = (r.get("ticker") or "").strip().upper()
            if len(ticker) > 20:
                ticker = ""
            isin = (r.get("isin") or "").strip().upper()
            if args.require_ticker and not ticker:
                stats["no_ticker"] += 1
                continue

            # Dedupe on the strongest identifier the row carries. Names are
            # only used when there is nothing better, since the same company
            # is spelled differently by different exchanges.
            if ticker and ticker in seen_tickers:
                stats["dupe"] += 1
                continue
            if isin and isin in seen_isins:
                stats["dupe"] += 1
                continue
            if not ticker and not isin and name.lower() in seen_names:
                stats["dupe"] += 1
                continue

            # `lei` is unique, so a blank has to be NULL rather than "", and a
            # LEI another row already holds has to be dropped instead of
            # aborting the load.
            lei = (r.get("lei") or "").strip().upper() or None
            if lei and lei in used_leis:
                lei = None

            if not args.dry_run:
                session.add(Company(
                    name=name[:500],
                    ticker=ticker or None,
                    lei=lei,
                    isin=isin or None,
                    country=(r.get("country") or "").strip()[:10] or None,
                    exchange=(r.get("exchange") or "").strip()[:100] or None,
                    index_membership=tag,
                ))
                pending += 1
                if pending >= 500:
                    session.commit()
                    pending = 0

            if ticker:
                seen_tickers.add(ticker)
            if isin:
                seen_isins.add(isin)
            if lei:
                used_leis.add(lei)
            seen_names.add(name.lower())
            stats["added"] += 1

        if not args.dry_run and pending:
            session.commit()

        print(f"\n{key} ({tag}) — {len(rows):,} rows in {fname}")
        print(f"   added {stats['added']:,}   already present {stats['dupe']:,}"
              f"   no ticker {stats['no_ticker']:,}   no name {stats['no_name']:,}")
        for k in grand:
            grand[k] += stats[k]

    verb = "Would add" if args.dry_run else "Added"
    print(f"\n{verb} {grand['added']:,} companies "
          f"({grand['dupe']:,} already present, "
          f"{grand['no_ticker']:,} skipped for having no ticker)")
    if args.dry_run:
        print("Dry run — nothing written.")
    else:
        print(f"Database now holds {session.query(Company).count():,} companies")


def main():
    parser = argparse.ArgumentParser(description="Corporate Emissions Database Pipeline")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    init_parser = subparsers.add_parser("init", help="Initialise database and load companies")
    init_parser.add_argument("--skip-lei", action="store_true", help="Skip LEI lookup")

    # extract
    # load-universe
    univ_parser = subparsers.add_parser(
        "load-universe",
        help="Load companies from the data/universe CSVs (no LEI lookup)")
    univ_parser.add_argument(
        "--sources", default="tier-s",
        help="Comma list of source keys, or 'all', or 'tier-s' (twse,nse — the "
             "markets whose emissions are free). Default: tier-s")
    univ_parser.add_argument("--dir", default="data/universe",
                             help="Directory holding the universe CSVs")
    univ_parser.add_argument("--tag", default=None,
                             help="Override index_membership for every loaded row")
    univ_parser.add_argument("--limit", type=int, default=None,
                             help="Load at most N rows per source (for testing)")
    univ_parser.add_argument("--require-ticker", action="store_true",
                             help="Skip rows with no ticker — they can reach "
                                  "neither Tier S nor yfinance")
    univ_parser.add_argument("--dry-run", action="store_true",
                             help="Report what would be loaded without writing")

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
    extract_parser.add_argument(
        "--free-only", action="store_true",
        help="Only run steps that cost nothing: Tier S emissions and the "
             "XBRL/yfinance financial tiers. Skips the emissions search "
             "walk, Tier 0, Tier 3 and industry classification.")
    extract_parser.add_argument("--concurrent", type=int, default=4,
                                help="Max companies to process concurrently (default: 4)")

    # lei
    lei_parser = subparsers.add_parser("lei", help="Re-resolve LEIs (ISIN-first) and report changes")
    lei_parser.add_argument("--id", type=int, help="Refresh a single company by ID")
    lei_parser.add_argument("--set", type=str, metavar="LEI",
                            help="With --id: set this LEI manually (verified against GLEIF)")
    lei_parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    lei_parser.add_argument("--verbose", action="store_true", help="Also list unchanged companies")

    # fi
    fi_parser = subparsers.add_parser(
        "fi", help="Settle PCAF financial-institution status per company, recompute EVIC")
    fi_parser.add_argument("--id", type=int, help="A single company by ID")
    fi_parser.add_argument("--set", choices=["yes", "no"],
                           help="With --id: set FI status manually")
    fi_parser.add_argument("--dry-run", action="store_true", help="Report without writing")

    # tickers
    tick_parser = subparsers.add_parser(
        "tickers", help="Verify tickers still return prices; sync from data/ftse100.py")
    tick_parser.add_argument("--dry-run", action="store_true", help="Report without writing")

    # check
    subparsers.add_parser("check", help="Run sanity checks")

    # status
    subparsers.add_parser("status", help="Show database status")

    # reset
    reset_parser = subparsers.add_parser("reset", help="Delete data for re-extraction")
    reset_parser.add_argument("--id", type=int, help="Reset a single company by ID")
    reset_parser.add_argument("--ids", type=str,
                                help="Company IDs: comma-separated (1,2,3) or range (1-20) or both (1-10,15,20)")
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
        "load-universe": cmd_load_universe,
        "extract": cmd_extract,
        "lei": cmd_lei,
        "tickers": cmd_tickers,
        "fi": cmd_fi,
        "check": cmd_check,
        "status": cmd_status,
        "reset": cmd_reset,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
