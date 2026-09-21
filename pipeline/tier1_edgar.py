"""Tier 1: Extract PCAF EVIC financial inputs from SEC EDGAR XBRL API.

Free, no API key needed. Works for companies that file 10-K/20-F with
the SEC (US-listed or foreign private issuers with ADRs).

Rate limit: 10 req/s per IP. We add a polite delay.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

_USER_AGENT = "CorporateEmissionsDB dafelis@hotmail.com"
_BASE = "https://data.sec.gov/api/xbrl"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_HEADERS = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip, deflate"}

# Cache the ticker→CIK mapping in memory
_cik_cache: dict[str, int] = {}


def _load_cik_map() -> dict[str, int]:
    """Load the SEC ticker→CIK map (cached after first call)."""
    if _cik_cache:
        return _cik_cache
    try:
        resp = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        for entry in resp.json().values():
            ticker = entry["ticker"].upper()
            _cik_cache[ticker] = entry["cik_str"]
        log.info(f"  Tier 1 EDGAR: loaded {len(_cik_cache)} ticker→CIK mappings")
    except Exception as e:
        log.warning(f"  Tier 1 EDGAR: failed to load CIK map: {e}")
    return _cik_cache


def _get_cik(ticker: str) -> int | None:
    """Look up a company's CIK from its ticker symbol."""
    cik_map = _load_cik_map()

    # Try the raw ticker first (e.g. "AZN" for AstraZeneca)
    clean = ticker.upper().replace(".L", "")
    if clean in cik_map:
        return cik_map[clean]

    # Some UK companies use different US tickers
    # e.g., Shell → SHEL, Unilever → UL, BP → BP
    return cik_map.get(clean)


def _edgar_get(url: str) -> dict | None:
    """Make a rate-limited GET to EDGAR."""
    time.sleep(0.15)  # stay well under 10 req/s
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=30)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"  Tier 1 EDGAR: request failed: {e}")
        return None


def _get_concept_values(cik: int, taxonomy: str, tag: str) -> list[dict]:
    """Fetch all values for a single XBRL concept."""
    padded = str(cik).zfill(10)
    url = f"{_BASE}/companyconcept/CIK{padded}/{taxonomy}/{tag}.json"
    data = _edgar_get(url)
    if not data:
        return []

    results = []
    for unit_key, entries in data.get("units", {}).items():
        for e in entries:
            e["_unit"] = unit_key
            results.append(e)
    return results


def _annual_value(entries: list[dict], fiscal_year: int) -> float | None:
    """Extract the annual (10-K/20-F) value for a given fiscal year."""
    candidates = [
        e for e in entries
        if e.get("fy") == fiscal_year
        and e.get("fp") == "FY"
        and e.get("form") in ("10-K", "10-K/A", "20-F", "20-F/A")
    ]
    if not candidates:
        return None
    # Prefer the most recently filed
    candidates.sort(key=lambda e: e.get("filed", ""), reverse=True)
    return candidates[0].get("val")


# Tag priority lists — try each in order
_REVENUE_TAGS = [
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "Revenues"),
    ("us-gaap", "SalesRevenueNet"),
]

_DEBT_TAGS = [
    ("us-gaap", "DebtLongtermAndShorttermCombinedAmount"),
    ("us-gaap", "LongTermDebt"),
]

_DEBT_COMPONENT_TAGS = [
    ("us-gaap", "LongTermDebtNoncurrent"),
    ("us-gaap", "LongTermDebtCurrent"),
    ("us-gaap", "ShortTermBorrowings"),
    ("us-gaap", "CommercialPaper"),
]

_LEASE_TAGS = [
    ("us-gaap", "OperatingLeaseLiability"),
    ("us-gaap", "FinanceLeaseLiability"),
]

_NCI_TAGS = [
    ("us-gaap", "MinorityInterest"),
]

_PREF_TAGS = [
    ("us-gaap", "PreferredStockValue"),
    ("us-gaap", "PreferredStockIncludingAdditionalPaidInCapital"),
]

_SHARES_TAGS = [
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
]


def _fetch_first_match(cik: int, tag_list: list[tuple], fy: int) -> float | None:
    """Try each (taxonomy, tag) pair until one returns a value for the given FY."""
    for taxonomy, tag in tag_list:
        entries = _get_concept_values(cik, taxonomy, tag)
        val = _annual_value(entries, fy)
        if val is not None:
            return val
    return None


def _fetch_sum(cik: int, tag_list: list[tuple], fy: int) -> float | None:
    """Sum values from multiple tags for the given FY."""
    total = 0
    found_any = False
    for taxonomy, tag in tag_list:
        entries = _get_concept_values(cik, taxonomy, tag)
        val = _annual_value(entries, fy)
        if val is not None:
            total += val
            found_any = True
    return total if found_any else None


def extract_financials_from_edgar(
    ticker: str,
    company_name: str,
    target_years: set[int],
) -> list[dict]:
    """Fetch PCAF EVIC inputs from SEC EDGAR for the given years.

    Returns a list of dicts matching the PCAF extraction schema.
    Returns an empty list if the company doesn't file with SEC.
    """
    cik = _get_cik(ticker)
    if cik is None:
        log.info(f"  Tier 1 EDGAR: {company_name} ({ticker}) not found in SEC filings")
        return []

    log.info(f"  Tier 1 EDGAR: found CIK {cik} for {company_name}")

    results = []
    for fy in sorted(target_years):
        revenue = _fetch_first_match(cik, _REVENUE_TAGS, fy)

        # Gross debt: try combined tag first, then sum components
        gross_debt = _fetch_first_match(cik, _DEBT_TAGS, fy)
        if gross_debt is None:
            gross_debt = _fetch_sum(cik, _DEBT_COMPONENT_TAGS, fy)

        # Lease liabilities: sum operating + finance
        lease_liab = _fetch_sum(cik, _LEASE_TAGS, fy)

        nci = _fetch_first_match(cik, _NCI_TAGS, fy)
        pref = _fetch_first_match(cik, _PREF_TAGS, fy)
        shares = _fetch_first_match(cik, _SHARES_TAGS, fy)

        # Skip if we got nothing
        if all(v is None for v in [revenue, gross_debt, shares]):
            log.info(f"    Tier 1 EDGAR: {fy} — no data")
            continue

        entry = {
            "reporting_year": fy,
            "reporting_date": f"{fy}-12-31",
            "currency": "USD",
            "unit_multiplier": 1,
            "gross_debt": {"value": gross_debt, "components": [], "ref": "SEC EDGAR XBRL", "confidence": "high"},
            "lease_liabilities": {"value": lease_liab, "label": "OperatingLeaseLiability+FinanceLeaseLiability", "ref": "SEC EDGAR XBRL", "confidence": "high"},
            "non_controlling_interests": {"value": nci, "label": "MinorityInterest", "ref": "SEC EDGAR XBRL", "confidence": "high"},
            "preference_shares": {"value": pref, "classification": "unknown", "listed": None, "ref": "SEC EDGAR XBRL"},
            "shares_outstanding": {"value": shares, "share_class": "", "ref": "SEC EDGAR XBRL", "confidence": "high"},
            "revenue": {"value": revenue, "label": "Revenue", "ref": "SEC EDGAR XBRL", "confidence": "high"},
            "is_financial_institution": False,
            "notes": [f"Tier 1: extracted from SEC EDGAR XBRL (CIK {cik})"],
        }

        parts = [f"Tier 1 EDGAR: {fy}"]
        if revenue is not None:
            parts.append(f"revenue={revenue:,.0f}")
        if gross_debt is not None:
            parts.append(f"debt={gross_debt:,.0f}")
        log.info(f"    {' — '.join(parts)}")

        results.append(entry)

    if results:
        log.info(f"  Tier 1 EDGAR: got {len(results)} year(s) for {company_name}")

    return results
