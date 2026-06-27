"""
Sanity checks and automated flagging for emissions data.

Runs after extraction to catch errors before human review.
"""

import logging
from datetime import datetime

from sqlalchemy import func

from db.models import Company, EmissionsRecord, get_session

log = logging.getLogger(__name__)


# Plausibility ranges by sector (tonnes CO2e)
# These are loose bounds — just catching obvious order-of-magnitude errors
SCOPE_1_MAX = 500_000_000   # 500Mt — only the very largest emitters (oil majors)
SCOPE_1_MIN = 0
SCOPE_3_MAX = 2_000_000_000  # 2Gt — upper bound for Scope 3

# Maximum plausible year-on-year change (as a fraction)
MAX_YOY_CHANGE = 0.5  # 50% — flag anything more than this


def _flag_record(record: EmissionsRecord, reason: str):
    """Flag a record for human review."""
    record.review_status = "flagged"
    if record.flag_reason:
        record.flag_reason += f"; {reason}"
    else:
        record.flag_reason = reason


def check_plausible_ranges(session) -> int:
    """Flag records with values outside plausible ranges."""
    flagged = 0
    records = session.query(EmissionsRecord).filter_by(review_status="pending").all()

    for r in records:
        reasons = []

        if r.scope_1 is not None:
            if r.scope_1 < SCOPE_1_MIN:
                reasons.append(f"Scope 1 is negative ({r.scope_1})")
            if r.scope_1 > SCOPE_1_MAX:
                reasons.append(f"Scope 1 implausibly high ({r.scope_1:,.0f})")

        if r.scope_2_location is not None and r.scope_2_location < 0:
            reasons.append(f"Scope 2 (location) is negative ({r.scope_2_location})")

        if r.scope_2_market is not None and r.scope_2_market < 0:
            reasons.append(f"Scope 2 (market) is negative ({r.scope_2_market})")

        if r.scope_3 is not None:
            if r.scope_3 < 0:
                reasons.append(f"Scope 3 is negative ({r.scope_3})")
            if r.scope_3 > SCOPE_3_MAX:
                reasons.append(f"Scope 3 implausibly high ({r.scope_3:,.0f})")

        if r.reporting_year is not None:
            current_year = datetime.utcnow().year
            if r.reporting_year < 2000 or r.reporting_year > current_year:
                reasons.append(f"Reporting year out of range ({r.reporting_year})")

        if reasons:
            _flag_record(r, "; ".join(reasons))
            flagged += 1

    session.commit()
    return flagged


def check_unit_consistency(session) -> int:
    """Flag records where units might indicate unconverted values (kt, Mt)."""
    flagged = 0
    records = session.query(EmissionsRecord).filter_by(review_status="pending").all()

    for r in records:
        if r.unit and r.unit.lower() in ("kt co2e", "kt", "mt co2e", "mt"):
            _flag_record(r, f"Unit is '{r.unit}' — values may not be in tonnes CO2e")
            flagged += 1

    session.commit()
    return flagged


def check_yoy_consistency(session) -> int:
    """Flag records with large year-on-year changes."""
    flagged = 0
    companies = session.query(Company).all()

    for company in companies:
        records = (
            session.query(EmissionsRecord)
            .filter_by(company_id=company.id)
            .order_by(EmissionsRecord.reporting_year)
            .all()
        )

        for i in range(1, len(records)):
            prev = records[i - 1]
            curr = records[i]

            # Only compare consecutive years
            if curr.reporting_year != prev.reporting_year + 1:
                continue

            for scope, attr in [
                ("Scope 1", "scope_1"),
                ("Scope 2 (location)", "scope_2_location"),
                ("Scope 3", "scope_3"),
            ]:
                prev_val = getattr(prev, attr)
                curr_val = getattr(curr, attr)

                if prev_val and curr_val and prev_val > 0:
                    change = abs(curr_val - prev_val) / prev_val
                    if change > MAX_YOY_CHANGE:
                        direction = "increase" if curr_val > prev_val else "decrease"
                        _flag_record(
                            curr,
                            f"{scope}: {change:.0%} {direction} vs {prev.reporting_year} "
                            f"({prev_val:,.0f} -> {curr_val:,.0f})"
                        )
                        flagged += 1

    session.commit()
    return flagged


def check_low_confidence(session, threshold: int = 50) -> int:
    """Flag records with low confidence scores."""
    flagged = 0
    records = (
        session.query(EmissionsRecord)
        .filter_by(review_status="pending")
        .filter(EmissionsRecord.confidence_score < threshold)
        .all()
    )

    for r in records:
        _flag_record(r, f"Low confidence score ({r.confidence_score}/100)")
        flagged += 1

    session.commit()
    return flagged


def check_missing_scopes(session) -> int:
    """Flag records where key scopes are missing (informational, not necessarily wrong)."""
    flagged = 0
    records = session.query(EmissionsRecord).filter_by(review_status="pending").all()

    for r in records:
        missing = []
        if r.scope_1 is None:
            missing.append("Scope 1")
        if r.scope_2_location is None and r.scope_2_market is None:
            missing.append("Scope 2")
        if r.scope_3 is None:
            missing.append("Scope 3")

        if missing:
            _flag_record(r, f"Missing: {', '.join(missing)}")
            flagged += 1

    session.commit()
    return flagged


def run_all_checks(database_url: str):
    """Run all sanity checks and print results."""
    session = get_session(database_url)

    print("Running sanity checks...")

    n = check_plausible_ranges(session)
    print(f"  Plausible ranges:    {n} flagged")

    n = check_unit_consistency(session)
    print(f"  Unit consistency:    {n} flagged")

    n = check_yoy_consistency(session)
    print(f"  Year-on-year:        {n} flagged")

    n = check_low_confidence(session)
    print(f"  Low confidence:      {n} flagged")

    n = check_missing_scopes(session)
    print(f"  Missing scopes:      {n} flagged")

    total_flagged = (
        session.query(EmissionsRecord)
        .filter_by(review_status="flagged")
        .count()
    )
    total_pending = (
        session.query(EmissionsRecord)
        .filter_by(review_status="pending")
        .count()
    )

    print(f"\nTotal flagged: {total_flagged}")
    print(f"Total pending (unflagged): {total_pending}")
