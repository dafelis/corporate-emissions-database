"""Look up LEI (Legal Entity Identifier) via the GLEIF public API.

Resolution order:
1. ISIN → LEI (deterministic). The ISIN comes from yfinance via the stored
   ticker; GLEIF's `filter[isin]` maps it to exactly one legal entity.
2. Name search, as a fallback — with entity-type guards, because a bare
   word-overlap score happily matches "X PLC SHARE INCENTIVE PLAN",
   "X PENSION SCHEME" or "X UK OPPORTUNITIES FUND" to company X.
"""

import re

import httpx


GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-records"

# Countries where FTSE 100 companies are typically registered
ALLOWED_COUNTRIES = {
    "GB",  # United Kingdom
    "IE",  # Ireland (e.g. Flutter, DCC)
    "JE",  # Jersey (e.g. some holding companies)
    "GG",  # Guernsey
    "CH",  # Switzerland (e.g. Glencore)
    "ZA",  # South Africa (e.g. Anglo American historically)
    "AU",  # Australia (e.g. Rio Tinto dual-listed)
    "NL",  # Netherlands (e.g. Shell, Unilever)
    "LU",  # Luxembourg
}

# Legal names containing these are vehicles attached to a company, not the
# company: rejected unless the search name itself contains the word.
_VEHICLE_TOKENS = re.compile(
    r"\b(plan|trust|trustee|scheme|pension|fund|nominee|nominees|employee|"
    r"benefit|foundation|charit\w*|section|esop|sip|unit trust|oeic|icvc)\b",
    re.I,
)


def _clean_name(name: str) -> str:
    """Remove parenthetical notes and common suffixes for better matching."""
    # "ABF (Associated British Foods)" -> "Associated British Foods"
    paren_match = re.search(r"\(([^)]+)\)", name)
    if paren_match:
        name = paren_match.group(1)
    return name.strip()


def _name_similarity(name_a: str, name_b: str) -> float:
    """Simple word-overlap similarity score between 0 and 1.

    Compares the set of significant words (ignoring common suffixes like
    plc, ltd, group, holdings) to see how much overlap there is.
    """
    stop_words = {
        "plc", "ltd", "limited", "group", "holdings", "inc", "corp",
        "corporation", "sa", "se", "nv", "ag", "the", "of", "and", "&",
    }

    def words(name):
        return {
            w.lower() for w in re.findall(r"[a-zA-Z0-9]+", name)
            if w.lower() not in stop_words and len(w) > 1
        }

    a = words(name_a)
    b = words(name_b)

    if not a or not b:
        return 0.0

    overlap = len(a & b)
    return overlap / max(len(a), len(b))


def _record_to_candidate(record: dict) -> dict:
    entity = record.get("attributes", {}).get("entity", {})
    country = (
        entity.get("legalAddress", {}).get("country", "")
        or entity.get("headquartersAddress", {}).get("country", "")
    )
    return {
        "lei": record.get("id") or record.get("attributes", {}).get("lei"),
        "legal_name": entity.get("legalName", {}).get("name", ""),
        "country": country,
        "category": entity.get("category"),
        "legal_form": (entity.get("legalForm") or {}).get("id"),
    }


def _search_gleif(query: str) -> list[dict]:
    """Search GLEIF for a company name, return top matches with metadata."""
    resp = httpx.get(
        GLEIF_SEARCH,
        params={
            "filter[fulltext]": query,
            "filter[entity.status]": "ACTIVE",
            "page[size]": 5,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [_record_to_candidate(r) for r in resp.json().get("data", [])]


def get_isin(ticker: str) -> str | None:
    """ISIN for a ticker via yfinance, or None."""
    if not ticker:
        return None
    try:
        import yfinance as yf
        isin = yf.Ticker(ticker).isin
    except Exception:
        return None
    if not isin or isin == "-" or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", isin):
        return None
    return isin


def lookup_lei_by_isin(isin: str) -> dict | None:
    """Resolve an ISIN to its issuing legal entity via GLEIF's ISIN mapping."""
    resp = httpx.get(
        GLEIF_SEARCH,
        params={"filter[isin]": isin, "page[size]": 3},
        timeout=30,
    )
    resp.raise_for_status()
    records = resp.json().get("data", [])
    if not records:
        return None
    cand = _record_to_candidate(records[0])
    if not cand["lei"]:
        return None
    return {
        **cand,
        "similarity": 1.0,
        "country_ok": True,
        "confidence": "high",
        "flag_reason": None,
        "method": f"isin:{isin}",
    }


def _best_match(candidates: list[dict], search_name: str) -> dict | None:
    """Pick the best match from GLEIF candidates, applying entity-type,
    country and name checks.

    Returns a dict with lei, legal_name, country, confidence, flag_reason (or None).
    """
    if not candidates:
        return None

    search_has_vehicle_word = bool(_VEHICLE_TOKENS.search(search_name))
    scored = []
    for c in candidates:
        # Funds, pension schemes, share plans, trusts: not the company.
        if (c.get("category") or "GENERAL") != "GENERAL":
            continue
        if _VEHICLE_TOKENS.search(c["legal_name"]) and not search_has_vehicle_word:
            continue

        similarity = _name_similarity(search_name, c["legal_name"])
        country_ok = c["country"] in ALLOWED_COUNTRIES

        # Confidence: high if name matches well AND country is right
        if similarity >= 0.5 and country_ok:
            confidence = "high"
        elif similarity >= 0.3 and country_ok:
            confidence = "medium"
        elif country_ok:
            confidence = "low"
        else:
            confidence = "rejected"

        reasons = []
        if similarity < 0.5:
            reasons.append(
                f"Low name similarity ({similarity:.0%}): "
                f"searched '{search_name}', found '{c['legal_name']}'"
            )
        if not country_ok:
            reasons.append(f"Country '{c['country']}' not in expected list for FTSE 100")

        scored.append({
            **c,
            "similarity": similarity,
            "country_ok": country_ok,
            "confidence": confidence,
            "flag_reason": "; ".join(reasons) if reasons else None,
            "method": "name",
        })

    if not scored:
        return None

    # Sort: high confidence first, then by similarity
    confidence_order = {"high": 0, "medium": 1, "low": 2, "rejected": 3}
    scored.sort(key=lambda x: (confidence_order[x["confidence"]], -x["similarity"]))

    best = scored[0]
    if best["confidence"] == "rejected":
        return None
    return best


def lookup_lei(company_name: str, ticker: str | None = None) -> dict | None:
    """Look up a company's LEI: by ISIN when a ticker is known, else by name.

    Returns dict with keys: lei, legal_name, country, confidence, flag_reason,
    method. Or None if no plausible match found.
    """
    isin = get_isin(ticker) if ticker else None
    if isin:
        try:
            result = lookup_lei_by_isin(isin)
            if result:
                return result
        except Exception:
            pass  # fall through to the name search

    all_candidates = []
    queries = [company_name, f"{company_name} plc"]
    cleaned = _clean_name(company_name)
    if cleaned != company_name:
        queries.extend([cleaned, f"{cleaned} plc"])

    for query in queries:
        all_candidates.extend(_search_gleif(query))

    seen = set()
    unique = []
    for c in all_candidates:
        if c["lei"] not in seen:
            seen.add(c["lei"])
            unique.append(c)

    return _best_match(unique, company_name)
