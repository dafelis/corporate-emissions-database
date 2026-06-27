"""Search the web for a company's emissions disclosure using Exa."""

import json

import anthropic
from exa_py import Exa


def search_for_emissions_source(
    company_name: str,
    anthropic_key: str,
    exa_key: str,
) -> dict:
    """Search for a company's sustainability/emissions report.

    Returns:
        {url, title, candidates: [{url, title}, ...]}
    """
    exa = Exa(api_key=exa_key)
    search_query = (
        f"{company_name} greenhouse gas emissions scope 1 2 3 "
        "sustainability report annual report ESG"
    )

    response = exa.search(search_query, num_results=10, type="auto")
    results = response.results

    if not results:
        raise ValueError(f"No search results found for '{company_name}'.")

    # PDFs first — better structured for table extraction
    pdf_results = [r for r in results if r.url.lower().split("?")[0].endswith(".pdf")]
    other_results = [r for r in results if not r.url.lower().split("?")[0].endswith(".pdf")]
    sorted_results = pdf_results + other_results

    # Use Haiku to rank results
    client = anthropic.Anthropic(api_key=anthropic_key)

    results_text = "\n".join(
        f"{i + 1}. Title: {r.title or '(no title)'}\n   URL: {r.url}"
        for i, r in enumerate(sorted_results)
    )

    response_msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        messages=[
            {
                "role": "user",
                "content": (
                    f"I'm looking for greenhouse gas emissions data (Scope 1, 2, 3) "
                    f"from '{company_name}'.\n\n"
                    f"Search results:\n{results_text}\n\n"
                    "Rank these from most to least likely to contain emissions data. "
                    "Strongly prefer PDF sustainability reports, annual reports, ESG reports, "
                    "and CDP disclosures over general web pages or news articles. "
                    "Prefer reports from the parent/group company rather than subsidiaries. "
                    "Include only results with a reasonable chance of containing the data.\n"
                    'Reply with JSON only: {"ranked": [{"url": "<url>", "title": "<title>"}, ...]}'
                ),
            }
        ],
    )

    raw = response_msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    parsed = json.loads(raw.strip())
    ranked = parsed.get("ranked", [])

    if not ranked:
        raise ValueError(f"Could not identify a suitable source for '{company_name}'.")

    return {
        "url": ranked[0]["url"],
        "title": ranked[0]["title"],
        "candidates": ranked,
    }
