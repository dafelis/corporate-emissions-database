"""Look up LEI (Legal Entity Identifier) via the GLEIF public API."""

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
    data = resp.json()

    results = []
    for record in data.get("data", []):
        entity = record.get("attributes", {}).get("entity", {})
        legal_name = entity.get("legalName", {}).get("name", "")
        country = (
            entity.get("legalAddress", {}).get("country", "")
            or entity.get("headquartersAddress", {}).get("country", "")
        )

        results.append({
            "lei": record.get("id"),
            "legal_name": legal_name,
            "country": country,
        })

    return results


def _best_match(candidates: list[dict], search_name: str) -> dict | None:
    """Pick the best match from GLEIF candidates, applying country and name checks.

    Returns a dict with lei, legal_name, country, confidence, flag_reason (or None).
    """
    if not candidates:
        return None

    scored = []
    for c in candidates:
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

        flag_reason = None
        reasons = []
        if similarity < 0.5:
            reasons.append(
                f"Low name similarity ({similarity:.0%}): "
                f"searched '{search_name}', found '{c['legal_name']}'"
            )
        if not country_ok:
            reasons.append(f"Country '{c['country']}' not in expected list for FTSE 100")
        if reasons:
            flag_reason = "; ".join(reasons)

        scored.append({
            **c,
            "similarity": similarity,
            "country_ok": country_ok,
            "confidence": confidence,
            "flag_reason": flag_reason,
        })

    # Sort: high confidence first, then by similarity
    confidence_order = {"high": 0, "medium": 1, "low": 2, "rejected": 3}
    scored.sort(key=lambda x: (confidence_order[x["confidence"]], -x["similarity"]))

    best = scored[0]

    # Reject obvious mismatches outright
    if best["confidence"] == "rejected":
        return None

    return best


def lookup_lei(company_name: str) -> dict | None:
    """Look up a company's LEI by name using the GLEIF API.

    Tries multiple name variations and picks the best match based on
    country filter and name similarity scoring.

    Returns dict with keys: lei, legal_name, country, confidence, flag_reason
    Or None if no plausible match found.
    """
    all_candidates = []

    # Try multiple name variations
    queries = [company_name, f"{company_name} plc"]

    cleaned = _clean_name(company_name)
    if cleaned != company_name:
        queries.extend([cleaned, f"{cleaned} plc"])

    for query in queries:
        candidates = _search_gleif(query)
        all_candidates.extend(candidates)

    # Deduplicate by LEI
    seen = set()
    unique = []
    for c in all_candidates:
        if c["lei"] not in seen:
            seen.add(c["lei"])
            unique.append(c)

    return _best_match(unique, company_name)
