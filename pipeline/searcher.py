"""Search the web for company disclosures using Exa."""

import json

import anthropic
from exa_py import Exa


RANKED_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["url", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def _search_and_rank(
    search_query: str,
    ranking_prompt: str,
    exa_key: str,
    anthropic_key: str,
    exclude_urls: list[str] = None,
) -> dict:
    """Generic search + rank helper.

    Returns {url, title, candidates: [{url, title}, ...]}
    """
    exa = Exa(api_key=exa_key)
    response = exa.search(search_query, num_results=10, type="auto")
    results = response.results

    # Filter out URLs we've already processed
    if exclude_urls:
        results = [r for r in results if r.url not in exclude_urls]

    if not results:
        raise ValueError(f"No search results found for query: {search_query[:80]}...")

    # PDFs first
    pdf_results = [r for r in results if r.url.lower().split("?")[0].endswith(".pdf")]
    other_results = [r for r in results if not r.url.lower().split("?")[0].endswith(".pdf")]
    sorted_results = pdf_results + other_results

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
                "content": f"{ranking_prompt}\n\nSearch results:\n{results_text}",
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": RANKED_SCHEMA,
            }
        },
    )

    text = next((b.text for b in response_msg.content if b.type == "text"), None)
    if text is None:
        raise ValueError("Claude returned no text response.")
    parsed = json.loads(text)
    ranked = parsed.get("ranked", [])

    if not ranked:
        raise ValueError("Could not identify a suitable source.")

    return {
        "url": ranked[0]["url"],
        "title": ranked[0]["title"],
        "candidates": ranked,
    }


def search_for_emissions_source(
    company_name: str,
    anthropic_key: str,
    exa_key: str,
    target_year: int = None,
    exclude_urls: list[str] = None,
) -> dict:
    """Search for a company's sustainability/emissions report.

    Args:
        company_name: Name of the company.
        anthropic_key: Anthropic API key.
        exa_key: Exa API key.
        target_year: If set, search specifically for this year's report.
        exclude_urls: URLs to skip (already processed).
    """
    if target_year:
        query = (
            f"{company_name} greenhouse gas emissions scope 1 2 3 "
            f"{target_year} sustainability report ESG annual report"
        )
        year_hint = f" for the year {target_year} (or covering {target_year})"
    else:
        query = (
            f"{company_name} greenhouse gas emissions scope 1 2 3 "
            "sustainability report annual report ESG"
        )
        year_hint = ""

    return _search_and_rank(
        search_query=query,
        ranking_prompt=(
            f"I'm looking for greenhouse gas emissions data (Scope 1, 2, 3) "
            f"from '{company_name}'{year_hint}.\n\n"
            "Rank these from most to least likely to contain emissions data.\n\n"
            "SOURCE PRIORITY (strongly prefer in this order):\n"
            "1. The company's own website (investor relations, sustainability pages)\n"
            "2. Official regulatory filings (SEC EDGAR, Companies House, annual report PDFs)\n"
            "3. CDP disclosures, GRI reports hosted on official platforms\n"
            "4. Reputable ESG data providers\n"
            "AVOID: news articles, blog posts, third-party aggregators (GuruFocus, Macrotrends, etc.)\n\n"
            "Prefer PDF sustainability reports, annual reports, and ESG reports. "
            "Prefer reports from the parent/group company rather than subsidiaries. "
            "Include only results with a reasonable chance of containing the data."
        ),
        exa_key=exa_key,
        anthropic_key=anthropic_key,
        exclude_urls=exclude_urls,
    )


def search_for_annual_report(
    company_name: str,
    anthropic_key: str,
    exa_key: str,
    target_year: int = None,
    exclude_urls: list[str] = None,
) -> dict:
    """Search for a company's annual report / financial statements.

    Args:
        company_name: Name of the company.
        anthropic_key: Anthropic API key.
        exa_key: Exa API key.
        target_year: If set, search specifically for this year's report.
        exclude_urls: URLs to skip (already processed).
    """
    if target_year:
        query = (
            f"{company_name} annual report financial statements "
            f"{target_year} revenue turnover balance sheet"
        )
        year_hint = f" for the year {target_year} (or covering {target_year})"
    else:
        query = (
            f"{company_name} annual report financial statements "
            "revenue turnover balance sheet"
        )
        year_hint = ""

    return _search_and_rank(
        search_query=query,
        ranking_prompt=(
            f"I'm looking for the annual report or financial statements "
            f"of '{company_name}'{year_hint} — specifically documents containing "
            "revenue/turnover, balance sheet data (debt, cash).\n\n"
            "SOURCE PRIORITY (strongly prefer in this order):\n"
            "1. The company's own website (investor relations, annual reports page)\n"
            "2. Official regulatory filings (SEC EDGAR 10-K/20-F, Companies House, "
            "   London Stock Exchange RNS, FCA National Storage Mechanism)\n"
            "3. Stock exchange disclosure platforms\n"
            "4. Reputable financial data providers\n"
            "AVOID: news articles, blog posts, third-party aggregators "
            "(GuruFocus, Macrotrends, SimplyWall.St, etc.)\n\n"
            "Strongly prefer PDF annual reports and regulatory filings. "
            "Prefer reports from the parent/group company rather than subsidiaries. "
            "Include only results with a reasonable chance of containing financial data."
        ),
        exa_key=exa_key,
        anthropic_key=anthropic_key,
        exclude_urls=exclude_urls,
    )


def search_for_financial_history(
    company_name: str,
    anthropic_key: str,
    exa_key: str,
    exclude_urls: list[str] = None,
) -> dict:
    """Search for a multi-year financial summary (five-year record, key financials page).

    Many companies publish a consolidated financial history that covers 5-10 years
    in a single table — much more efficient than finding individual annual reports.
    """
    return _search_and_rank(
        search_query=(
            f"{company_name} five year summary financial history "
            "key financials revenue debt historical performance"
        ),
        ranking_prompt=(
            f"I'm looking for a multi-year financial summary or five-year record "
            f"for '{company_name}' — a single page or document that shows revenue, "
            "debt, and/or cash across MULTIPLE years (ideally 5+ years).\n\n"
            "SOURCE PRIORITY (strongly prefer in this order):\n"
            "1. The company's own investor relations website (five-year record, "
            "   financial highlights, key performance indicators page)\n"
            "2. Official regulatory filings with historical summaries\n"
            "3. Stock exchange disclosure platforms\n"
            "AVOID: third-party aggregators (GuruFocus, Macrotrends, SimplyWall.St, "
            "Wisesheets, etc.) — these often block automated access and data may "
            "differ from official filings.\n\n"
            "Strongly prefer five-year summaries, key financial highlights pages, "
            "and investor fact sheets from official sources. "
            "Include only results likely to contain multi-year data."
        ),
        exa_key=exa_key,
        anthropic_key=anthropic_key,
        exclude_urls=exclude_urls,
    )
