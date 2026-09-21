"""Search the web for company disclosures using Exa."""

import json
import logging

import anthropic
from exa_py import Exa

log = logging.getLogger(__name__)


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
            f"{target_year} {target_year + 1} sustainability report ESG annual report"
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
            "AVOID: news articles, blog posts, third-party aggregators (GuruFocus, Macrotrends, etc.)\n"
            "AVOID: methodology reports, 'basis of reporting', reporting criteria, "
            "verification/assurance statements, Scope 3 methodology documents — "
            "these describe HOW emissions are calculated, not the actual values. "
            "Do NOT include them even if they are the only PDF result.\n\n"
            "LINK TYPE PRIORITY:\n"
            "1. Direct links to PDF documents (URLs ending in .pdf) — STRONGLY PREFER\n"
            "2. Specific report pages with downloadable content\n"
            "3. DEPRIORITISE: index/landing pages like 'Results, reports and presentations', "
            "'Investor relations', 'Document library' — these list reports but don't contain data\n\n"
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
            "DOCUMENT TYPE PRIORITY (when multiple results from the same company):\n"
            "1. 'Annual Report and Accounts' / 'Annual Report' PDF — full audited accounts (BEST)\n"
            "2. Financial Statements, 10-K, 20-F regulatory filings\n"
            "3. Five-year summaries, financial highlights pages\n"
            "4. DEPRIORITISE: 'Results for the year', 'Preliminary Results', "
            "'Annual Results' — often unaudited with rounded figures\n"
            "5. AVOID: 'Investor Presentation', 'Capital Markets Day' slides — "
            "numbers are often rounded or taken out of context\n\n"
            "LINK TYPE PRIORITY:\n"
            "1. Direct links to PDF documents (URLs ending in .pdf) — STRONGLY PREFER\n"
            "2. Specific report pages with downloadable content\n"
            "3. DEPRIORITISE: index/landing pages like 'Results, reports and presentations', "
            "'Investor relations', 'Document library' — these list reports but don't contain data\n\n"
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
            "DOCUMENT TYPE PRIORITY:\n"
            "1. Five-year summaries from Annual Report PDFs (BEST)\n"
            "2. Financial highlights / KPI pages on company investor relations website\n"
            "3. DEPRIORITISE: Results announcements, investor presentations — "
            "may use rounded or preliminary figures\n\n"
            "LINK TYPE PRIORITY:\n"
            "1. Direct links to PDF documents (URLs ending in .pdf) — STRONGLY PREFER\n"
            "2. DEPRIORITISE: index/landing pages like 'Results, reports and presentations', "
            "'Document library' — these list reports but don't contain data\n\n"
            "Include only results likely to contain multi-year data."
        ),
        exa_key=exa_key,
        anthropic_key=anthropic_key,
        exclude_urls=exclude_urls,
    )


# ── Archive page scraping ────────────────────────────────────────────────

def _extract_pdf_links(html, base_url, target_year=None):
    """Parse HTML and extract sustainability-related PDF links."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")

    pdf_links = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().split("?")[0].endswith(".pdf"):
            continue

        full_url = urljoin(base_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        link_text = a.get_text(strip=True) or ""
        combined = (link_text + " " + full_url).lower()

        is_methodology = any(kw in combined for kw in [
            "methodology", "basis of reporting", "reporting criteria",
            "verification", "assurance statement",
        ])
        if is_methodology:
            continue

        is_sustainability = any(kw in combined for kw in [
            "sustainability", "esg", "climate", "environment",
        ])

        year_match = False
        if target_year:
            year_match = str(target_year) in combined

        pdf_links.append({
            "url": full_url,
            "title": link_text or "(untitled PDF)",
            "is_sustainability": is_sustainability,
            "year_match": year_match,
        })

    pdf_links.sort(key=lambda x: (
        not (x["year_match"] and x["is_sustainability"]),
        not x["year_match"],
        not x["is_sustainability"],
    ))

    return [{"url": p["url"], "title": p["title"]} for p in pdf_links]


def scrape_report_pdfs(page_url, target_year=None):
    """Fetch a web page and extract PDF links for sustainability reports.

    Tries simple HTTP first, then Playwright for JS-heavy pages.
    Returns list of {url, title} candidates, best matches first.
    """
    import requests

    html = None

    try:
        resp = requests.get(page_url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        })
        resp.raise_for_status()
        html = resp.text
    except Exception:
        pass

    if html:
        links = _extract_pdf_links(html, page_url, target_year)
        if links:
            return links

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(page_url, timeout=30000)
            page.wait_for_load_state("networkidle", timeout=15000)
            html = page.content()
            browser.close()

        if html:
            return _extract_pdf_links(html, page_url, target_year)
    except Exception:
        pass

    return []


def find_archive_pdfs(company_name, exa_key, target_year, exclude_urls=None):
    """Search for the company's report archive page and scrape PDF links.

    Returns list of {url, title} candidates for the target year,
    or empty list if no archive page found.
    """
    exa = Exa(api_key=exa_key)
    response = exa.search(
        f"{company_name} sustainability reports previous years downloads",
        num_results=5, type="auto",
    )

    for result in response.results:
        url = result.url
        if url.lower().split("?")[0].endswith(".pdf"):
            continue
        if exclude_urls and url in exclude_urls:
            continue

        log.info(f"    Scraping archive page: {result.title}")
        log.info(f"      {url[:100]}")

        pdf_links = scrape_report_pdfs(url, target_year=target_year)
        if pdf_links:
            new_links = [
                p for p in pdf_links
                if not exclude_urls or p["url"] not in exclude_urls
            ]
            if new_links:
                log.info(f"      Found {len(new_links)} PDF(s) "
                         f"matching year {target_year}")
                return new_links
            log.info(f"      All PDFs already searched")
        else:
            log.info(f"      No PDF links found on page")

    return []
