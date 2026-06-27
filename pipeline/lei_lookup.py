"""Look up LEI (Legal Entity Identifier) via the GLEIF public API."""

import httpx


GLEIF_API = "https://api.gleif.org/api/v1/fuzzycompletions"
GLEIF_RECORDS = "https://api.gleif.org/api/v1/lei-records"


def lookup_lei(company_name: str) -> dict | None:
    """Look up a company's LEI by name using the GLEIF API.

    Returns {"lei": str, "legal_name": str} or None if no match found.
    """
    resp = httpx.get(
        GLEIF_API,
        params={"field": "fulltext", "q": company_name},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    completions = data.get("data", [])
    if not completions:
        return None

    # The fuzzy completions endpoint returns LEI values directly
    lei = completions[0].get("relationships", {}).get("lei-records", {}).get("data", {}).get("id")
    if not lei:
        # Try alternative structure
        lei = completions[0].get("id")

    if not lei:
        return None

    # Fetch the full record to get the legal name
    record_resp = httpx.get(f"{GLEIF_RECORDS}/{lei}", timeout=30)
    record_resp.raise_for_status()
    record = record_resp.json()

    legal_name = (
        record.get("data", {})
        .get("attributes", {})
        .get("entity", {})
        .get("legalName", {})
        .get("name", company_name)
    )

    return {"lei": lei, "legal_name": legal_name}
