"""Tier 1: Extract PCAF EVIC financial inputs from filings.xbrl.org.

Free JSON-API for UK ESEF/UKSEF filings. Searchable by LEI.
Returns IFRS-tagged XBRL data covering balance sheet and income statement.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

_BASE = "https://filings.xbrl.org/api/filings"
_HEADERS = {
    "User-Agent": "CorporateEmissionsDB dafelis@hotmail.com",
    "Accept": "application/json",
}


def _api_get(url: str, params: dict | None = None) -> dict | list | None:
    """Rate-limited GET to filings.xbrl.org."""
    time.sleep(0.2)
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"  Tier 1 XBRL: request failed ({url}): {e}")
        return None


# IFRS concept names → our PCAF fields.
# filings.xbrl.org XBRL-JSON uses the full prefixed concept name.
_REVENUE_CONCEPTS = [
    "ifrs-full:Revenue",
    "ifrs-full:RevenueFromContractsWithCustomers",
]

_DEBT_CONCEPTS = [
    "ifrs-full:NoncurrentBorrowings",
    "ifrs-full:CurrentBorrowings",
    "ifrs-full:Borrowings",
]

_LEASE_CONCEPTS = [
    "ifrs-full:LeaseLiabilities",
    "ifrs-full:NoncurrentLeaseLiabilities",
    "ifrs-full:CurrentLeaseLiabilities",
]

_NCI_CONCEPTS = [
    "ifrs-full:NoncontrollingInterests",
    "ifrs-full:EquityAttributableToNoncontrollingInterests",
]

_PREF_CONCEPTS = [
    "ifrs-full:PreferenceShareCapital",
]

_SHARES_CONCEPTS = [
    "ifrs-full:NumberOfSharesOutstanding",
    "ifrs-full:NumberOfSharesIssued",
]


def _extract_fact_value(facts: dict, concept_list: list[str], instant: bool = True) -> float | None:
    """Find the first matching concept in the facts dict and return its value.

    XBRL-JSON facts are keyed by concept name.  Each maps to a list of
    fact instances (different periods, dimensions, etc.).  We pick the
    one that looks like the primary (no dimensional qualifier) instant
    or duration fact.
    """
    for concept in concept_list:
        fact_list = facts.get(concept)
        if not fact_list:
            continue
        for f in (fact_list if isinstance(fact_list, list) else [fact_list]):
            dims = f.get("dimensions", {})
            # Skip dimensionally qualified facts (segment breakdowns)
            if len(dims) > 3:
                continue
            val = f.get("value")
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    continue
    return None


def _sum_facts(facts: dict, concept_list: list[str]) -> float | None:
    """Sum values across multiple concepts (e.g. current + noncurrent)."""
    total = 0.0
    found = False
    for concept in concept_list:
        v = _extract_fact_value(facts, [concept])
        if v is not None:
            total += v
            found = True
    return total if found else None


def _get_filing_year(filing: dict) -> int | None:
    """Extract the reporting year from a filing's period information."""
    period_end = filing.get("period_end") or filing.get("periodEnd")
    if period_end:
        try:
            return date.fromisoformat(period_end[:10]).year
        except (ValueError, TypeError):
            pass
    date_str = filing.get("date") or filing.get("filing_date")
    if date_str:
        try:
            return date.fromisoformat(date_str[:10]).year
        except (ValueError, TypeError):
            pass
    return None


def extract_financials_from_xbrl(
    lei: str,
    company_name: str,
    target_years: set[int],
) -> list[dict]:
    """Fetch PCAF EVIC inputs from filings.xbrl.org for the given years.

    Returns a list of dicts matching the PCAF extraction schema.
    Returns an empty list if no filings are found.
    """
    if not lei:
        log.info(f"  Tier 1 XBRL: no LEI for {company_name}, skipping")
        return []

    # Search for filings by LEI
    data = _api_get(_BASE, params={"lei": lei})
    if not data:
        log.info(f"  Tier 1 XBRL: no filings found for {company_name} (LEI {lei})")
        return []

    # The API returns either a dict with a "data" key or a list directly
    filings = data if isinstance(data, list) else data.get("data", data.get("filings", []))
    if not isinstance(filings, list):
        filings = [filings] if isinstance(filings, dict) else []

    if not filings:
        log.info(f"  Tier 1 XBRL: no filings for {company_name}")
        return []

    log.info(f"  Tier 1 XBRL: found {len(filings)} filing(s) for {company_name}")

    results = []
    seen_years = set()

    for filing in filings:
        fy = _get_filing_year(filing)
        if fy is None or fy not in target_years or fy in seen_years:
            continue

        # Get the XBRL-JSON data URL
        json_url = (
            filing.get("json_url")
            or filing.get("viewer_url", "").replace("/viewer", "/json")
            or filing.get("report_url")
        )
        if not json_url:
            # Try constructing from filing ID
            filing_id = filing.get("id") or filing.get("filing_id")
            if filing_id:
                json_url = f"https://filings.xbrl.org/api/filings/{filing_id}/facts"
            else:
                continue

        facts_data = _api_get(json_url)
        if not facts_data:
            continue

        # Facts may be nested under a key or be top-level
        facts = facts_data
        if isinstance(facts_data, dict):
            facts = facts_data.get("facts", facts_data.get("data", facts_data))

        if not isinstance(facts, dict):
            log.warning(f"  Tier 1 XBRL: unexpected facts format for {fy}")
            continue

        revenue = _extract_fact_value(facts, _REVENUE_CONCEPTS)

        # Gross debt: try single concept first, then sum current+noncurrent
        gross_debt = _extract_fact_value(facts, ["ifrs-full:Borrowings"])
        if gross_debt is None:
            gross_debt = _sum_facts(facts, [
                "ifrs-full:NoncurrentBorrowings",
                "ifrs-full:CurrentBorrowings",
            ])

        lease_liab = _extract_fact_value(facts, ["ifrs-full:LeaseLiabilities"])
        if lease_liab is None:
            lease_liab = _sum_facts(facts, [
                "ifrs-full:NoncurrentLeaseLiabilities",
                "ifrs-full:CurrentLeaseLiabilities",
            ])

        nci = _extract_fact_value(facts, _NCI_CONCEPTS)
        pref = _extract_fact_value(facts, _PREF_CONCEPTS)
        shares = _extract_fact_value(facts, _SHARES_CONCEPTS)

        if all(v is None for v in [revenue, gross_debt, shares]):
            log.info(f"    Tier 1 XBRL: {fy} — no PCAF fields found")
            continue

        period_end = filing.get("period_end") or filing.get("periodEnd", "")
        currency = filing.get("currency") or filing.get("reporting_currency", "GBP")

        entry = {
            "reporting_year": fy,
            "reporting_date": period_end[:10] if period_end else f"{fy}-12-31",
            "currency": currency,
            "unit_multiplier": 1,
            "gross_debt": {"value": gross_debt, "components": [], "ref": "XBRL IFRS", "confidence": "high"},
            "lease_liabilities": {"value": lease_liab, "label": "LeaseLiabilities", "ref": "XBRL IFRS", "confidence": "high"},
            "non_controlling_interests": {"value": nci, "label": "NoncontrollingInterests", "ref": "XBRL IFRS", "confidence": "high"},
            "preference_shares": {"value": pref, "classification": "unknown", "listed": None, "ref": "XBRL IFRS"},
            "shares_outstanding": {"value": shares, "share_class": "", "ref": "XBRL IFRS", "confidence": "high"},
            "revenue": {"value": revenue, "label": "Revenue", "ref": "XBRL IFRS", "confidence": "high"},
            "is_financial_institution": False,
            "notes": [f"Tier 1: extracted from XBRL IFRS filing (LEI {lei})"],
        }

        parts = [f"Tier 1 XBRL: {fy}"]
        if revenue is not None:
            parts.append(f"revenue={revenue:,.0f}")
        if gross_debt is not None:
            parts.append(f"debt={gross_debt:,.0f}")
        log.info(f"    {' — '.join(parts)}")

        results.append(entry)
        seen_years.add(fy)

    if results:
        log.info(f"  Tier 1 XBRL: got {len(results)} year(s) for {company_name}")

    return results
