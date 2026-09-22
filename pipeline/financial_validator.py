"""Validation rules for PCAF EVIC financial records."""

import json
import logging

log = logging.getLogger(__name__)


def validate_financial_entry(entry, reporting_year=None):
    """Run validation rules on an extracted financial entry.

    Returns a list of flag strings. Empty list = clean.
    """
    flags = []

    def _val(field_name):
        obj = entry.get(field_name)
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get("value")
        return obj

    gross_debt = _val("gross_debt")
    lease_liab = _val("lease_liabilities")
    revenue = _val("revenue")
    shares = _val("shares_outstanding")
    nci = _val("non_controlling_interests")
    pref = _val("preference_shares")

    reporting_date = entry.get("reporting_date")

    # reporting_date within expected fiscal year
    if reporting_date and reporting_year:
        try:
            year_in_date = int(reporting_date[:4])
            if abs(year_in_date - reporting_year) > 1:
                flags.append(
                    f"reporting_date {reporting_date} outside expected "
                    f"year {reporting_year}"
                )
        except (ValueError, TypeError):
            flags.append(f"invalid reporting_date: {reporting_date}")

    # gross_debt >= lease_liabilities
    if gross_debt is not None and lease_liab is not None:
        if gross_debt < lease_liab:
            flags.append(
                f"gross_debt ({gross_debt}) < lease_liabilities ({lease_liab})"
            )

    # gross_debt >= 0
    if gross_debt is not None and gross_debt < 0:
        flags.append(f"gross_debt is negative ({gross_debt})")

    # revenue > 0
    if revenue is not None and revenue <= 0:
        flags.append(f"revenue is non-positive ({revenue})")

    # shares_outstanding > 0
    if shares is not None and shares <= 0:
        flags.append(f"shares_outstanding is non-positive ({shares})")

    # Any field with confidence "low" → flag for review
    for field_name in ("gross_debt", "lease_liabilities",
                       "non_controlling_interests", "revenue",
                       "shares_outstanding"):
        obj = entry.get(field_name)
        if isinstance(obj, dict) and obj.get("confidence") == "low":
            flags.append(f"{field_name}: low confidence")

    # Financial institution check
    if entry.get("is_financial_institution"):
        flags.append("FI — PCAF FI treatment required")

    return flags


def compute_evic(record):
    """Compute EVIC from a FinancialRecord (DB model instance).

    PCAF definition: EVIC = market_cap + total_debt (book value) + NCI
    Cash is NOT subtracted. Preference shares stored but not in EVIC.

    Returns the EVIC value or None if required inputs are missing.
    Sets record.evic and record.validation_flags.
    """
    flags = json.loads(record.validation_flags) if record.validation_flags else []

    if record.is_financial_institution:
        record.evic = None
        if "FI — PCAF FI treatment required" not in flags:
            flags.append("FI — PCAF FI treatment required")
        record.validation_flags = json.dumps(flags)
        return None

    market_cap = None
    if record.equity_value is not None:
        market_cap = record.equity_value
    elif (record.share_price_at_fy_end is not None
          and record.shares_outstanding is not None):
        market_cap = record.share_price_at_fy_end * record.shares_outstanding

    if market_cap is None:
        flags.append("EVIC: missing market_cap")
        record.validation_flags = json.dumps(flags)
        return None

    debt = record.gross_debt or 0
    nci = record.non_controlling_interests or 0

    evic = market_cap + debt + nci
    record.evic = evic

    if record.gross_debt is None:
        flags.append("EVIC: gross_debt missing, treated as 0")
    if record.non_controlling_interests is None:
        flags.append("EVIC: NCI missing, treated as 0")

    # Merge EVIC provenance into extraction_notes
    try:
        notes_data = json.loads(record.extraction_notes) if record.extraction_notes else {}
    except (json.JSONDecodeError, TypeError):
        notes_data = {}
    prov = notes_data.get("provenance", {})

    # Pull period info from existing provenance entries
    equity_prov = prov.get("equity_value", {})
    debt_prov = prov.get("gross_debt", {})
    nci_prov = prov.get("non_controlling_interests", {})
    equity_period = equity_prov.get("period", "")
    debt_period = debt_prov.get("period", "")
    nci_period = nci_prov.get("period", "")
    evic_unit = equity_prov.get("unit", debt_prov.get("unit", ""))

    evic_prov = {
        "concept": "EVIC",
        "value": evic,
        "unit": evic_unit,
        "calculated": True,
        "components": [],
    }
    if market_cap is not None:
        evic_prov["components"].append({
            "concept": "Equity",
            "value": market_cap,
            "calculated": False,
            "period": equity_period,
        })
    if debt:
        evic_prov["components"].append({
            "concept": "Gross Debt",
            "value": debt,
            "calculated": bool(record.gross_debt_ref and "+" in str(record.gross_debt_ref)),
            "period": debt_period,
        })
    if nci:
        evic_prov["components"].append({
            "concept": "Non-controlling Interests",
            "value": nci,
            "calculated": False,
            "period": nci_period,
        })

    prov["evic"] = evic_prov
    notes_data["provenance"] = prov
    record.extraction_notes = json.dumps(notes_data)

    record.validation_flags = json.dumps(flags) if flags else None
    return evic
