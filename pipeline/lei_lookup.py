"""Look up LEI (Legal Entity Identifier) via the GLEIF public API."""

import re

import httpx


GLEIF_SEARCH = "https://api.gleif.org/api/v1/lei-records"


def _clean_name(name: str) -> str:
    """Remove parenthetical notes and common suffixes for better matching."""
    # "ABF (Associated British Foods)" -> "Associated British Foods"
    paren_match = re.search(r"\(([^)]+)\)", name)
    if paren_match:
        name = paren_match.group(1)
    return name.strip()


def _search_gleif(query: str) -> dict | None:
    """Search GLEIF for a company name, return first match or None."""
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

    records = data.get("data", [])
    if not records:
        return None

    # Return the first active record
    record = records[0]
    lei = record.get("id")
    legal_name = (
        record.get("attributes", {})
        .get("entity", {})
        .get("legalName", {})
        .get("name", query)
    )

    return {"lei": lei, "legal_name": legal_name}


def lookup_lei(company_name: str) -> dict | None:
    """Look up a company's LEI by name using the GLEIF API.

    Tries multiple name variations to improve match rates:
    1. Original name
    2. Name with "plc" appended (common for UK companies)
    3. Cleaned name (parenthetical text extracted, e.g. "ABF (Associated British Foods)")

    Returns {"lei": str, "legal_name": str} or None if no match found.
    """
    # Try original name first
    result = _search_gleif(company_name)
    if result:
        return result

    # Try with "plc" appended (FTSE 100 companies are all UK plcs)
    result = _search_gleif(f"{company_name} plc")
    if result:
        return result

    # Try cleaned name (extract from parentheses if present)
    cleaned = _clean_name(company_name)
    if cleaned != company_name:
        result = _search_gleif(cleaned)
        if result:
            return result

        result = _search_gleif(f"{cleaned} plc")
        if result:
            return result

    return None
