"""Multi-strategy document parser with fallback chain.

When the primary HTTP request fails (timeout, 403, bot block), tries
alternative access methods in order:

  1. Direct HTTP request (fast timeout — current parser.py behaviour)
  2. Exa cached content (Exa has already crawled the page)
  3. Playwright headless Chrome (bypasses basic bot detection)
  4. Wayback Machine archived copy

Each strategy either returns table_dicts or raises to trigger the next.
"""

import hashlib
import logging
import os
import tempfile
import time

import requests
from bs4 import BeautifulSoup

from pipeline.parser import (
    _BROWSER_HEADERS,
    _html_table_to_markdown,
    _extract_tables_from_text,
    detect_source_type,
    download_to_tempfile,
    extract_tables_from_documents,
)

log = logging.getLogger(__name__)


# ── Local document cache ─────────────────────────────────────────────
# Successfully fetched documents are cached so future runs don't re-fetch.

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "document_cache")


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _check_cache(url: str, source_type: str) -> str | None:
    """Return local path if URL was previously cached, else None."""
    ext = {"pdf": ".pdf", "html": ".html", "excel": ".xlsx"}.get(source_type, ".html")
    path = os.path.join(CACHE_DIR, f"{_cache_key(url)}{ext}")
    return path if os.path.exists(path) else None


def _save_to_cache(url: str, content: bytes, source_type: str) -> str:
    """Save raw bytes to the local cache and return the path."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    ext = {"pdf": ".pdf", "html": ".html", "excel": ".xlsx"}.get(source_type, ".html")
    path = os.path.join(CACHE_DIR, f"{_cache_key(url)}{ext}")
    with open(path, "wb") as f:
        f.write(content)
    # Also write a .meta file so we know what URL this came from
    with open(path + ".meta", "w", encoding="utf-8") as f:
        f.write(url)
    return path


def _parse_local_file(path: str, source_type: str, llama_key: str) -> list[dict]:
    """Parse a locally cached file into table_dicts."""
    if source_type == "pdf":
        from llama_parse import LlamaParse

        parser = LlamaParse(api_key=llama_key, result_type="markdown", verbose=False)
        docs = parser.load_data(path)
        return extract_tables_from_documents(docs)
    elif source_type == "excel":
        from pipeline.parser import parse_excel

        return parse_excel(path)
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            html = f.read()
        return _tables_from_html(html)


def _tables_from_html(html: str) -> list[dict]:
    """Extract table_dicts from raw HTML string."""
    soup = BeautifulSoup(html, "html.parser")
    tables = []
    for tag in soup.find_all("table"):
        md = _html_table_to_markdown(tag)
        if md:
            tables.append({"markdown": md, "html_snippet": str(tag)})
    return tables


def _text_from_html(html: str) -> str:
    """Extract clean text from HTML (fallback when no tables found)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:50_000]


# ── Callback protocol for UI integration ─────────────────────────────

class ParseProgress:
    """Override methods to receive progress updates (e.g. Streamlit UI)."""

    def on_trying(self, method: str, url: str):
        pass

    def on_success(self, method: str, elapsed: float, table_count: int):
        pass

    def on_fail(self, method: str, error: str, elapsed: float):
        pass


# ── Strategy 1: Direct HTTP ──────────────────────────────────────────

def _strategy_direct(url: str, source_type: str, llama_key: str) -> list[dict]:
    """Direct HTTP request — uses parser.py functions with fast timeouts."""
    if source_type == "pdf":
        from pipeline.parser import parse_pdf

        docs = parse_pdf(url, llama_key)
        return extract_tables_from_documents(docs)
    elif source_type == "excel":
        from pipeline.parser import parse_excel

        return parse_excel(url)
    else:
        from pipeline.parser import parse_html

        tables = parse_html(url)
        if tables:
            return tables
        # No tables — try text extraction as last resort
        from pipeline.parser import extract_html_text

        text = extract_html_text(url)
        if text and len(text.strip()) > 100:
            return [{"markdown": text}]
        raise ValueError("Direct fetch: no tables or text found")


# ── Strategy 2: Exa cached content ──────────────────────────────────

def _strategy_exa_cache(url: str, exa_key: str) -> list[dict]:
    """Retrieve Exa's cached version of the page."""
    from exa_py import Exa

    exa = Exa(api_key=exa_key)

    # Exa get_contents accepts a list of document IDs (which are URLs)
    try:
        response = exa.get_contents([url], text=True)
    except Exception:
        # Fallback: search for the exact URL
        try:
            response = exa.find_similar(url, num_results=1, text=True)
        except Exception as e2:
            raise ValueError(f"Exa cache lookup failed: {e2}")

    if not response.results:
        raise ValueError("Exa has no cached content for this URL")

    text = getattr(response.results[0], "text", None)
    if not text or len(text.strip()) < 100:
        raise ValueError(f"Exa cache too short ({len(text or '')} chars)")

    # Try to extract markdown tables from the text
    tables = _extract_tables_from_text(text)
    if tables:
        return [{"markdown": t} for t in tables]

    # No structured tables — return full text as a single block
    # The LLM extraction step can still find data in unstructured text
    return [{"markdown": text[:50_000]}]


# ── Strategy 3: Playwright headless Chrome ───────────────────────────

def _strategy_playwright(url: str, source_type: str, llama_key: str) -> list[dict]:
    """Headless Chrome via Playwright — bypasses basic bot detection."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright not installed — run: "
            "pip install playwright && playwright install chromium"
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            if source_type == "pdf":
                # Navigate to the PDF — Chromium returns raw bytes in response
                response = page.goto(url, timeout=60_000)
                if response is None or not response.ok:
                    status = response.status if response else "no response"
                    raise ValueError(f"Playwright: HTTP {status}")

                body = response.body()
                if len(body) < 500:
                    raise ValueError("Playwright: PDF response too small")

                # Save to temp file and parse with LlamaParse
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.write(body)
                tmp.close()

                try:
                    from llama_parse import LlamaParse

                    parser = LlamaParse(
                        api_key=llama_key, result_type="markdown", verbose=False
                    )
                    docs = parser.load_data(tmp.name)
                    return extract_tables_from_documents(docs)
                finally:
                    os.unlink(tmp.name)
            else:
                # HTML page — wait for full render
                page.goto(url, timeout=30_000, wait_until="networkidle")
                html = page.content()

                tables = _tables_from_html(html)
                if tables:
                    return tables

                # No tables — try text extraction
                text = _text_from_html(html)
                if len(text) > 100:
                    return [{"markdown": text}]

                raise ValueError("Playwright: page loaded but no useful content")
        finally:
            browser.close()


# ── Strategy 4: Wayback Machine ──────────────────────────────────────

def _strategy_wayback(url: str, source_type: str, llama_key: str) -> list[dict]:
    """Fetch from the Wayback Machine's cached copy."""
    # Ask the Wayback Machine API for the latest snapshot
    api_url = f"https://archive.org/wayback/available?url={url}"
    resp = requests.get(api_url, timeout=(5, 10))
    resp.raise_for_status()
    data = resp.json()

    snapshot = data.get("archived_snapshots", {}).get("closest")
    if not snapshot or not snapshot.get("available"):
        raise ValueError("No Wayback Machine snapshot available for this URL")

    archived_url = snapshot["url"]
    # Ensure HTTPS
    archived_url = archived_url.replace(
        "http://web.archive.org", "https://web.archive.org"
    )
    log.info("Wayback snapshot: %s", archived_url)

    if source_type == "pdf":
        # Download the archived PDF, then parse with LlamaParse
        local_path = download_to_tempfile(archived_url)
        try:
            from llama_parse import LlamaParse

            parser = LlamaParse(
                api_key=llama_key, result_type="markdown", verbose=False
            )
            docs = parser.load_data(local_path)
            return extract_tables_from_documents(docs)
        finally:
            os.unlink(local_path)
    else:
        # Fetch the archived HTML page
        resp = requests.get(archived_url, headers=_BROWSER_HEADERS, timeout=(5, 30))
        resp.raise_for_status()

        # Remove Wayback Machine toolbar injection
        soup = BeautifulSoup(resp.text, "html.parser")
        for wm_tag in soup.select("#wm-ipp-base, #wm-ipp, #wm-ipp-print"):
            wm_tag.decompose()

        html = str(soup)
        tables = _tables_from_html(html)
        if tables:
            return tables

        # Text fallback
        text = _text_from_html(html)
        if len(text) > 100:
            return [{"markdown": text}]

        raise ValueError("Wayback page has no useful content")


# ── Main fallback chain ──────────────────────────────────────────────

def parse_with_fallbacks(
    url: str,
    llama_key: str,
    exa_key: str | None = None,
    source_type: str | None = None,
    progress: ParseProgress | None = None,
    skip_playwright: bool = False,
    use_cache: bool = True,
) -> tuple[list[dict], str, float]:
    """Parse a document URL trying multiple access strategies.

    Returns (table_dicts, method_name, elapsed_seconds).
    Raises RuntimeError if all strategies fail.
    """
    if source_type is None:
        source_type = detect_source_type(url)
    if progress is None:
        progress = ParseProgress()

    # Check local cache first
    if use_cache:
        cached_path = _check_cache(url, source_type)
        if cached_path:
            progress.on_trying("cache", url)
            t0 = time.time()
            try:
                table_dicts = _parse_local_file(cached_path, source_type, llama_key)
                elapsed = time.time() - t0
                progress.on_success("cache", elapsed, len(table_dicts))
                return table_dicts, "cache", elapsed
            except Exception as e:
                elapsed = time.time() - t0
                progress.on_fail("cache", str(e), elapsed)
                log.info("Cache parse failed for %s: %s", url, e)

    # Build strategy chain
    strategies: list[tuple[str, callable]] = [
        ("direct", lambda: _strategy_direct(url, source_type, llama_key)),
    ]

    if exa_key:
        strategies.append(
            ("exa_cache", lambda: _strategy_exa_cache(url, exa_key))
        )

    if not skip_playwright:
        strategies.append(
            ("playwright", lambda: _strategy_playwright(url, source_type, llama_key))
        )

    strategies.append(
        ("wayback", lambda: _strategy_wayback(url, source_type, llama_key))
    )

    errors: list[tuple[str, str, float]] = []

    for method_name, strategy_fn in strategies:
        progress.on_trying(method_name, url)
        t0 = time.time()
        try:
            table_dicts = strategy_fn()
            elapsed = time.time() - t0
            progress.on_success(method_name, elapsed, len(table_dicts))
            return table_dicts, method_name, elapsed
        except Exception as e:
            elapsed = time.time() - t0
            errors.append((method_name, str(e), elapsed))
            progress.on_fail(method_name, str(e), elapsed)
            log.info(
                "Strategy %s failed for %s: %s (%.1fs)",
                method_name, url, e, elapsed,
            )

    error_detail = "\n".join(
        f"  {name}: {err} ({t:.1f}s)" for name, err, t in errors
    )
    raise RuntimeError(
        f"All {len(errors)} parse strategies failed for {url}:\n{error_detail}"
    )
