"""Tier 1: Extract PCAF EVIC financial inputs from filings.xbrl.org.

Free JSON-API for UK ESEF/UKSEF filings. Searchable by LEI.
Returns IFRS-tagged XBRL-JSON data covering balance sheet and income statement.

API docs: https://filings.xbrl.org
Endpoint: /api/entities/{LEI}/filings  (JSON:API v1.0 format)
XBRL-JSON: each filing has a json_url pointing to structured fact data.
"""

import logging
import time

import requests

log = logging.getLogger(__name__)

_BASE = "https://filings.xbrl.org"
_HEADERS = {
    "User-Agent": "CorporateEmissionsDB dafelis@hotmail.com",
    "Accept": "application/json",
}


def _api_get(url: str, params: dict | None = None) -> dict | list | None:
    """Rate-limited GET to filings.xbrl.org."""
    time.sleep(0.2)
    try:
        resp = requests.get(url, headers=_HEADERS, params=params, timeout=60)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"  Tier 1 XBRL: request failed ({url}): {e}")
        return None


# IFRS concept names we look for in facts.
# Facts are keyed by arbitrary IDs; we match on dimensions.concept.
_REVENUE_CONCEPTS = {
    "ifrs-full:Revenue",
    "ifrs-full:RevenueFromContractsWithCustomers",
}

_DEBT_SINGLE_CONCEPTS = {
    "ifrs-full:Borrowings",
}

# Pairs to try in order — sum each pair, take first that works
_DEBT_SUM_PAIRS = [
    ("ifrs-full:NoncurrentBorrowings", "ifrs-full:CurrentBorrowings"),
    ("ifrs-full:LongtermBorrowings", "ifrs-full:ShorttermBorrowings"),
]

_LEASE_SINGLE_CONCEPTS = {
    "ifrs-full:LeaseLiabilities",
    "ifrs-full:FinanceLeaseLiabilities",
}

_LEASE_SUM_PAIRS = [
    ("ifrs-full:NoncurrentLeaseLiabilities", "ifrs-full:CurrentLeaseLiabilities"),
]

_NCI_CONCEPTS = {
    "ifrs-full:NoncontrollingInterests",
    "ifrs-full:EquityAttributableToNoncontrollingInterests",
}

_PREF_CONCEPTS = {
    "ifrs-full:PreferenceShareCapital",
}

_SHARES_CONCEPTS = {
    "ifrs-full:NumberOfSharesOutstanding",
    "ifrs-full:NumberOfSharesIssued",
}


def _build_concept_index(facts: dict) -> dict[str, list[dict]]:
    """Index facts by concept name for fast lookup.

    XBRL-JSON facts are keyed by arbitrary IDs (f-1, f-2, ...).
    Each has dimensions.concept = "ifrs-full:Revenue" etc.
    We invert this into concept → [fact, ...].
    """
    index: dict[str, list[dict]] = {}
    for _fid, fact in facts.items():
        dims = fact.get("dimensions", {})
        concept = dims.get("concept")
        if concept:
            index.setdefault(concept, []).append(fact)
    return index


def _pick_value(index: dict, concept_set: set[str]) -> tuple[float | None, dict | None]:
    """Return (value, provenance_dict) for the first numeric match.

    Prefers facts with fewer dimensional qualifiers (= consolidated totals).
    """
    for concept in concept_set:
        fact_list = index.get(concept, [])
        for f in sorted(fact_list, key=lambda x: len(x.get("dimensions", {}))):
            val = f.get("value")
            if val is None:
                continue
            try:
                fval = float(val)
            except (ValueError, TypeError):
                continue
            dims = f.get("dimensions", {})
            prov = {
                "concept": concept,
                "value": fval,
                "unit": dims.get("unit", ""),
                "period": dims.get("period", ""),
                "decimals": f.get("decimals"),
                "entity": dims.get("entity", ""),
            }
            return fval, prov
    return None, None


def _sum_pairs(index: dict, pairs: list[tuple[str, str]]) -> tuple[float | None, dict | None]:
    """Try each (long, short) pair; return (sum, provenance) for first hit."""
    for a, b in pairs:
        va, pa = _pick_value(index, {a})
        vb, pb = _pick_value(index, {b})
        if va is not None or vb is not None:
            parts = []
            if pa:
                parts.append(pa)
            if pb:
                parts.append(pb)
            concept_str = " + ".join(p["concept"] for p in parts)
            prov = {
                "concept": concept_str,
                "value": (va or 0) + (vb or 0),
                "unit": (pa or pb or {}).get("unit", ""),
                "period": (pa or pb or {}).get("period", ""),
                "decimals": (pa or pb or {}).get("decimals"),
                "entity": (pa or pb or {}).get("entity", ""),
                "components": parts,
            }
            return (va or 0) + (vb or 0), prov
    return None, None


def _detect_currency(index: dict) -> str:
    """Try to detect the reporting currency from fact units."""
    for concept_name in ("ifrs-full:Revenue", "ifrs-full:Borrowings",
                         "ifrs-full:NoncurrentBorrowings",
                         "ifrs-full:LongtermBorrowings"):
        facts = index.get(concept_name, [])
        for f in facts:
            unit = f.get("dimensions", {}).get("unit", "")
            if unit.startswith("iso4217:"):
                return unit.split(":")[1]
    return "USD"


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

    # Correct endpoint: /api/entities/{LEI}/filings
    url = f"{_BASE}/api/entities/{lei}/filings"
    data = _api_get(url)
    if not data:
        log.info(f"  Tier 1 XBRL: no filings found for {company_name} (LEI {lei})")
        return []

    # JSON:API format: data is under the "data" key
    filings = data.get("data", [])
    if not filings:
        log.info(f"  Tier 1 XBRL: no filings for {company_name}")
        return []

    log.info(f"  Tier 1 XBRL: found {len(filings)} filing(s) for {company_name}")

    results = []
    seen_years = set()

    for filing in filings:
        attrs = filing.get("attributes", {})
        period_end = attrs.get("period_end", "")
        if not period_end:
            continue

        try:
            fy = int(period_end[:4])
        except (ValueError, TypeError):
            continue

        if fy not in target_years or fy in seen_years:
            continue

        # json_url is relative — prepend base
        json_path = attrs.get("json_url")
        if not json_path:
            log.info(f"    Tier 1 XBRL: {fy} — no json_url, skipping")
            continue

        json_url = f"{_BASE}{json_path}"
        log.info(f"    Tier 1 XBRL: fetching XBRL-JSON for {fy}...")
        facts_data = _api_get(json_url)
        if not facts_data:
            continue

        raw_facts = facts_data.get("facts", {})
        if not raw_facts:
            log.info(f"    Tier 1 XBRL: {fy} — no facts in JSON")
            continue

        index = _build_concept_index(raw_facts)
        currency = _detect_currency(index)

        # Build a viewer URL for this specific filing
        filing_dir = json_path.rsplit("/", 1)[0] if "/" in json_path else ""
        viewer_url = f"{_BASE}{filing_dir}/" if filing_dir else None

        revenue, rev_prov = _pick_value(index, _REVENUE_CONCEPTS)

        # Gross debt: try single concept first, then sum current + noncurrent pairs
        gross_debt, debt_prov = _pick_value(index, _DEBT_SINGLE_CONCEPTS)
        if gross_debt is None:
            gross_debt, debt_prov = _sum_pairs(index, _DEBT_SUM_PAIRS)

        # Lease liabilities
        lease_liab, lease_prov = _pick_value(index, _LEASE_SINGLE_CONCEPTS)
        if lease_liab is None:
            lease_liab, lease_prov = _sum_pairs(index, _LEASE_SUM_PAIRS)

        nci, nci_prov = _pick_value(index, _NCI_CONCEPTS)
        pref, pref_prov = _pick_value(index, _PREF_CONCEPTS)
        shares, shares_prov = _pick_value(index, _SHARES_CONCEPTS)

        if all(v is None for v in [revenue, gross_debt, shares]):
            log.info(f"    Tier 1 XBRL: {fy} — no PCAF fields found in "
                     f"{len(raw_facts)} facts ({len(index)} concepts)")
            continue

        def _ref(prov):
            return prov["concept"] if prov else None

        # Build per-field provenance for the popup
        provenance = {}
        for field, prov in [("revenue", rev_prov), ("gross_debt", debt_prov),
                            ("lease_liabilities", lease_prov),
                            ("non_controlling_interests", nci_prov),
                            ("preference_shares", pref_prov),
                            ("shares_outstanding", shares_prov)]:
            if prov:
                provenance[field] = prov

        entry = {
            "reporting_year": fy,
            "reporting_date": period_end[:10],
            "currency": currency,
            "unit_multiplier": 1,
            "viewer_url": viewer_url,
            "provenance": provenance,
            "gross_debt": {"value": gross_debt, "components": [], "ref": _ref(debt_prov), "confidence": "high"},
            "lease_liabilities": {"value": lease_liab, "label": _ref(lease_prov), "ref": _ref(lease_prov), "confidence": "high"},
            "non_controlling_interests": {"value": nci, "label": _ref(nci_prov), "ref": _ref(nci_prov), "confidence": "high"},
            "preference_shares": {"value": pref, "classification": "unknown", "listed": None, "ref": _ref(pref_prov)},
            "shares_outstanding": {"value": shares, "share_class": "", "ref": _ref(shares_prov), "confidence": "high"},
            "revenue": {"value": revenue, "label": _ref(rev_prov), "ref": _ref(rev_prov), "confidence": "high"},
            "is_financial_institution": False,
            "notes": [f"Tier 1: extracted from XBRL IFRS filing (LEI {lei})"],
        }

        parts = [f"Tier 1 XBRL: {fy} ({currency})"]
        if revenue is not None:
            parts.append(f"revenue={revenue:,.0f}")
        if gross_debt is not None:
            parts.append(f"debt={gross_debt:,.0f}")
        if nci is not None:
            parts.append(f"NCI={nci:,.0f}")
        if shares is not None:
            parts.append(f"shares={shares:,.0f}")
        log.info(f"    {' — '.join(parts)}")

        results.append(entry)
        seen_years.add(fy)

    if results:
        log.info(f"  Tier 1 XBRL: got {len(results)} year(s) for {company_name}")

    return results
