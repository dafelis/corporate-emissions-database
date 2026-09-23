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


def _tables_or_text_from_docs(docs: list) -> list[dict]:
    """Extract tables from LlamaParse docs, falling back to full text.

    If LlamaParse produced structured markdown tables (pipe-delimited with
    separator rows), return those.  Otherwise return the full text so the
    LLM extraction step can still find emissions data in unstructured text.
    """
    if not docs:
        log.warning("LlamaParse returned 0 documents")
        return []

    tables = extract_tables_from_documents(docs)
    if tables:
        log.info("Found %d structured table(s) across %d page(s)", len(tables), len(docs))
        return tables

    page_lens = [len(doc.text or "") for doc in docs]
    total_chars = sum(page_lens)
    log.info(
        "LlamaParse: %d page(s), %d total chars, 0 markdown tables. "
        "Page lengths: %s",
        len(docs), total_chars,
        page_lens[:10] if len(page_lens) <= 10 else page_lens[:5] + ["..."],
    )

    all_text = "\n\n".join(doc.text for doc in docs if doc.text)
    if all_text and len(all_text.strip()) > 100:
        pipe_lines = sum(1 for line in all_text.split("\n") if line.strip().startswith("|"))
        if pipe_lines:
            log.info("  %d lines start with '|' but no valid separator row found", pipe_lines)
        return [{"markdown": all_text[:50_000]}]

    log.warning("LlamaParse returned %d page(s) with no useful text", len(docs))
    return []


_EMISSIONS_KEYWORDS = [
    "scope 1", "scope 2", "scope 3", "emissions",
    "ghg", "greenhouse", "co2", "carbon dioxide",
    "tco2", "tonnes co2", "mt co2", "ktco2",
]


def _extract_text_with_pymupdf(pdf_path: str) -> list[dict]:
    """Extract text from a local PDF using pymupdf (no external API)."""
    import pymupdf

    pdf_doc = pymupdf.open(pdf_path)
    page_count = len(pdf_doc)
    all_pages = []
    for page_idx in range(page_count):
        text = pdf_doc[page_idx].get_text()
        if text and len(text.strip()) > 100:
            all_pages.append({"markdown": text, "page_index": page_idx})
    pdf_doc.close()

    if not all_pages:
        log.warning("pymupdf: no text extracted from %d pages", page_count)
        return []

    # Check for structured tables first
    all_tables = []
    for r in all_pages:
        for t in _extract_tables_from_text(r["markdown"]):
            all_tables.append({"markdown": t, "page_index": r["page_index"]})
    if all_tables:
        log.info("pymupdf: found %d structured table(s) across %d pages", len(all_tables), page_count)
        return all_tables

    # Filter to pages mentioning emissions-related keywords
    relevant = [
        p for p in all_pages
        if any(kw in p["markdown"].lower() for kw in _EMISSIONS_KEYWORDS)
    ]
    if relevant:
        log.info("pymupdf: %d/%d pages contain emissions keywords", len(relevant), page_count)
        return relevant

    # No keyword matches — return all pages (capped)
    log.info("pymupdf: no keyword matches, returning all %d pages", len(all_pages))
    return all_pages


def _parse_pdf_with_fallback(local_path: str, llama_key: str) -> list[dict]:
    """Parse a local PDF: LlamaParse first, pymupdf if that fails."""
    file_size = os.path.getsize(local_path)
    log.info("PDF file: %.1f MB (%d bytes)", file_size / 1_048_576, file_size)

    from llama_parse import LlamaParse

    parser = LlamaParse(api_key=llama_key, result_type="markdown", verbose=False)
    try:
        docs = parser.load_data(local_path)
    except Exception as e:
        log.warning("LlamaParse error: %s — trying pymupdf", e)
        return _extract_text_with_pymupdf(local_path)

    tables = _tables_or_text_from_docs(docs)
    if tables:
        return tables

    log.info("LlamaParse produced no usable output, trying pymupdf")
    return _extract_text_with_pymupdf(local_path)


def _parse_local_file(path: str, source_type: str, llama_key: str) -> list[dict]:
    """Parse a locally cached file into table_dicts."""
    if source_type == "pdf":
        return _parse_pdf_with_fallback(path, llama_key)
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


# Text that only ever appears on a WAF / bot-protection challenge page.
# Such pages return HTTP 200 and clear the minimum-length check, so without
# this they get passed to the LLM as if they were the document.
_BLOCK_SIGNATURES = (
    "incapsula incident id",
    "_incapsula_resource",
    "request unsuccessful.",
    "just a moment...",
    "checking your browser before accessing",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "enable javascript and cookies to continue",
    "pardon our interruption",
    "verify you are human",
    "you don't have permission to access",
)


def _looks_blocked(text: str) -> str | None:
    """Return the matching signature if `text` is a bot-protection page, else None.

    Only short bodies are considered: challenge pages are a few hundred bytes,
    and a real report could legitimately contain one of these phrases.
    """
    low = (text or "").lower()
    if len(low) > 5_000:
        return None
    for sig in _BLOCK_SIGNATURES:
        if sig in low:
            return sig
    return None


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
        local_path = download_to_tempfile(url)
        try:
            return _parse_pdf_with_fallback(local_path, llama_key)
        finally:
            os.unlink(local_path)
    elif source_type == "excel":
        from pipeline.parser import parse_excel

        return parse_excel(url)
    else:
        # For HTML, do a quick HEAD check first to fail fast on blocked sites
        try:
            head_resp = requests.head(url, headers=_BROWSER_HEADERS, timeout=5, allow_redirects=True)
            head_resp.raise_for_status()
        except Exception as e:
            raise ValueError(f"Site unreachable: {e}")

        from pipeline.parser import parse_html

        tables = parse_html(url)
        if tables:
            return tables
        # No tables — try text extraction as last resort
        from pipeline.parser import extract_html_text

        text = extract_html_text(url)
        blocked = _looks_blocked(text)
        if blocked:
            raise ValueError(f"Blocked by bot protection ({blocked!r})")
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
    blocked = _looks_blocked(text)
    if blocked:
        # Exa's crawler was blocked too — its "cached content" is the challenge page
        raise ValueError(f"Exa cache is a bot-protection page ({blocked!r})")
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
                # Fetch through the browser context rather than navigating:
                # Chromium treats a PDF URL as a download and page.goto()
                # raises "Download is starting". The context request shares
                # the browser's cookies, headers and TLS fingerprint, so a
                # challenge cookie obtained by visiting the site still applies.
                from urllib.parse import urlparse
                parsed = urlparse(url)
                origin = f"{parsed.scheme}://{parsed.netloc}/"
                try:
                    page.goto(origin, timeout=20_000, wait_until="domcontentloaded")
                except Exception:
                    pass  # origin unreachable is not fatal — try the PDF anyway

                response = context.request.get(url, timeout=60_000)
                if not response.ok:
                    raise ValueError(f"Playwright: HTTP {response.status}")

                body = response.body()
                if len(body) < 500:
                    raise ValueError("Playwright: PDF response too small")
                if not body[:5].startswith(b"%PDF"):
                    raise ValueError(
                        "Playwright: response is not a PDF "
                        "(site may have returned an HTML page)"
                    )

                # Save to temp file and parse
                tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
                tmp.write(body)
                tmp.close()

                try:
                    return _parse_pdf_with_fallback(tmp.name, llama_key)
                finally:
                    os.unlink(tmp.name)
            else:
                # HTML page — wait for full render
                page.goto(url, timeout=30_000, wait_until="networkidle")
                html = page.content()

                blocked = _looks_blocked(_text_from_html(html))
                if blocked:
                    raise ValueError(f"Playwright: blocked by bot protection ({blocked!r})")

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
        local_path = download_to_tempfile(archived_url)
        try:
            return _parse_pdf_with_fallback(local_path, llama_key)
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
        blocked = _looks_blocked(_text_from_html(html))
        if blocked:
            raise ValueError(f"Wayback: archived copy is a bot-protection page ({blocked!r})")
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

    first_empty: tuple[list[dict], str, float] | None = None

    # Check local cache first
    if use_cache:
        cached_path = _check_cache(url, source_type)
        if cached_path:
            progress.on_trying("cache", url)
            t0 = time.time()
            try:
                table_dicts = _parse_local_file(cached_path, source_type, llama_key)
                elapsed = time.time() - t0
                if table_dicts:
                    progress.on_success("cache", elapsed, len(table_dicts))
                    return table_dicts, "cache", elapsed
                progress.on_fail("cache", "No tables found in cached copy", elapsed)
                first_empty = (table_dicts, "cache", elapsed)
            except Exception as e:
                elapsed = time.time() - t0
                progress.on_fail("cache", str(e), elapsed)
                log.info("Cache parse failed for %s: %s", url, e)

    # Build strategy chain — each entry is (name, callable)
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
            if table_dicts:
                progress.on_success(method_name, elapsed, len(table_dicts))
                return table_dicts, method_name, elapsed
            # Strategy accessed the document but found 0 tables — try next
            progress.on_fail(method_name, "No tables found in document", elapsed)
            if first_empty is None:
                first_empty = (table_dicts, method_name, elapsed)
            log.info(
                "Strategy %s returned 0 tables for %s (%.1fs)",
                method_name, url, elapsed,
            )
        except Exception as e:
            elapsed = time.time() - t0
            errors.append((method_name, str(e), elapsed))
            progress.on_fail(method_name, str(e), elapsed)
            log.info(
                "Strategy %s failed for %s: %s (%.1fs)",
                method_name, url, e, elapsed,
            )

    # If at least one strategy accessed the document but found no tables,
    # return that result so the caller knows the document was reachable
    if first_empty is not None:
        return first_empty

    error_detail = "\n".join(
        f"  {name}: {err} ({t:.1f}s)" for name, err, t in errors
    )
    raise RuntimeError(
        f"All {len(errors)} parse strategies failed for {url}:\n{error_detail}"
    )
