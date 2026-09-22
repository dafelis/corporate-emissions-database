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

# Cache the ticker→(CIK, SEC title) mapping in memory
_cik_cache: dict[str, tuple[int, str]] = {}


def _load_cik_map() -> dict[str, tuple[int, str]]:
    """Load the SEC ticker→(CIK, title) map (cached after first call)."""
    if _cik_cache:
        return _cik_cache
    try:
        resp = requests.get(_TICKERS_URL, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        for entry in resp.json().values():
            ticker = entry["ticker"].upper()
            _cik_cache[ticker] = (entry["cik_str"], entry.get("title", ""))
        log.info(f"  Tier 1 EDGAR: loaded {len(_cik_cache)} ticker→CIK mappings")
    except Exception as e:
        log.warning(f"  Tier 1 EDGAR: failed to load CIK map: {e}")
    return _cik_cache


def _name_matches(sec_title: str, company_name: str) -> bool:
    """Check whether the SEC filer name plausibly matches our company name."""
    a = sec_title.upper().strip()
    b = company_name.upper().strip()
    if not a or not b:
        return False
    # Strip common suffixes for comparison
    for suffix in (" PLC", " LTD", " LIMITED", " INC", " INC.", " CORP",
                   " CORP.", " GROUP", " HOLDINGS", " CO", " CO."):
        a = a.removesuffix(suffix)
        b = b.removesuffix(suffix)
    # Exact match after cleanup
    if a == b:
        return True
    # One name contains the other (e.g. "BP" in "BP PLC")
    if a in b or b in a:
        return True
    # First significant word matches (e.g. "SHELL" in "SHELL PLC" vs "Shell")
    a_words = a.split()
    b_words = b.split()
    if a_words and b_words and a_words[0] == b_words[0] and len(a_words[0]) >= 3:
        return True
    return False


def _get_cik(ticker: str, company_name: str) -> int | None:
    """Look up a company's CIK from its ticker, verifying the name matches."""
    cik_map = _load_cik_map()

    clean = ticker.upper().replace(".L", "")
    entry = cik_map.get(clean)
    if entry:
        cik, sec_title = entry
        if _name_matches(sec_title, company_name):
            return cik
        log.info(f"  Tier 1 EDGAR: ticker {clean} maps to '{sec_title}' "
                 f"(CIK {cik}), not '{company_name}' — skipping")
    return None


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


def _fetch_first_match(cik: int, tag_list: list[tuple], fy: int) -> tuple[float | None, dict | None]:
    """Try each (taxonomy, tag) pair until one returns a value for the given FY."""
    for taxonomy, tag in tag_list:
        entries = _get_concept_values(cik, taxonomy, tag)
        val = _annual_value(entries, fy)
        if val is not None:
            return val, {
                "concept": f"{taxonomy}:{tag}",
                "value": val,
                "unit": "iso4217:USD",
                "period": f"{fy}-01-01/{fy + 1}-01-01",
                "calculated": False,
            }
    return None, None


def _fetch_sum(cik: int, tag_list: list[tuple], fy: int) -> tuple[float | None, dict | None]:
    """Sum values from multiple tags for the given FY."""
    total = 0
    found_any = False
    components = []
    for taxonomy, tag in tag_list:
        entries = _get_concept_values(cik, taxonomy, tag)
        val = _annual_value(entries, fy)
        if val is not None:
            total += val
            found_any = True
            components.append({
                "concept": f"{taxonomy}:{tag}",
                "value": val,
                "calculated": False,
                "period": f"{fy}-01-01/{fy + 1}-01-01",
            })
    if not found_any:
        return None, None
    concept_str = " + ".join(c["concept"] for c in components)
    return total, {
        "concept": concept_str,
        "value": total,
        "unit": "iso4217:USD",
        "period": f"{fy}-01-01/{fy + 1}-01-01",
        "calculated": True,
        "components": components,
    }


def extract_financials_from_edgar(
    ticker: str,
    company_name: str,
    target_years: set[int],
) -> list[dict]:
    """Fetch PCAF EVIC inputs from SEC EDGAR for the given years.

    Returns a list of dicts matching the PCAF extraction schema.
    Returns an empty list if the company doesn't file with SEC.
    """
    cik = _get_cik(ticker, company_name)
    if cik is None:
        log.info(f"  Tier 1 EDGAR: {company_name} ({ticker}) — no matching SEC filer")
        return []

    log.info(f"  Tier 1 EDGAR: found CIK {cik} for {company_name}")

    padded_cik = str(cik).zfill(10)
    results = []
    for fy in sorted(target_years):
        revenue, rev_tag = _fetch_first_match(cik, _REVENUE_TAGS, fy)

        # Gross debt: try combined tag first, then sum components
        gross_debt, debt_tag = _fetch_first_match(cik, _DEBT_TAGS, fy)
        if gross_debt is None:
            gross_debt, debt_tag = _fetch_sum(cik, _DEBT_COMPONENT_TAGS, fy)

        # Lease liabilities: sum operating + finance
        lease_liab, lease_tag = _fetch_sum(cik, _LEASE_TAGS, fy)

        nci, nci_tag = _fetch_first_match(cik, _NCI_TAGS, fy)
        pref, pref_tag = _fetch_first_match(cik, _PREF_TAGS, fy)
        shares, shares_tag = _fetch_first_match(cik, _SHARES_TAGS, fy)

        # Skip if we got nothing
        if all(v is None for v in [revenue, gross_debt, shares]):
            log.info(f"    Tier 1 EDGAR: {fy} — no data")
            continue

        def _edgar_url(concept_str):
            if not concept_str or "+" in concept_str:
                return None
            taxonomy, tag = concept_str.split(":", 1)
            return f"{_BASE}/companyconcept/CIK{padded_cik}/{taxonomy}/{tag}.json"

        def _ref(prov):
            return prov["concept"] if prov else None

        # Build per-field provenance for the popup
        provenance = {}
        for field, prov in [("revenue", rev_tag), ("gross_debt", debt_tag),
                            ("lease_liabilities", lease_tag),
                            ("non_controlling_interests", nci_tag),
                            ("preference_shares", pref_tag),
                            ("shares_outstanding", shares_tag)]:
            if prov:
                provenance[field] = prov

        entry = {
            "reporting_year": fy,
            "reporting_date": f"{fy}-12-31",
            "currency": "USD",
            "unit_multiplier": 1,
            "viewer_url": f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={padded_cik}&type=10-K&dateb=&owner=include&count=10",
            "provenance": provenance,
            "gross_debt": {"value": gross_debt, "components": [], "ref": _ref(debt_tag), "confidence": "high", "url": _edgar_url(_ref(debt_tag))},
            "lease_liabilities": {"value": lease_liab, "label": _ref(lease_tag), "ref": _ref(lease_tag), "confidence": "high", "url": _edgar_url(_ref(lease_tag))},
            "non_controlling_interests": {"value": nci, "label": _ref(nci_tag), "ref": _ref(nci_tag), "confidence": "high", "url": _edgar_url(_ref(nci_tag))},
            "preference_shares": {"value": pref, "classification": "unknown", "listed": None, "ref": _ref(pref_tag), "url": _edgar_url(_ref(pref_tag))},
            "shares_outstanding": {"value": shares, "share_class": "", "ref": _ref(shares_tag), "confidence": "high", "url": _edgar_url(_ref(shares_tag))},
            "revenue": {"value": revenue, "label": _ref(rev_tag), "ref": _ref(rev_tag), "confidence": "high", "url": _edgar_url(_ref(rev_tag))},
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
