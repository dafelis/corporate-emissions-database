#!/usr/bin/env python3
"""
Bulk PDF Downloader for FTSE 100 sustainability & annual reports.

Downloads reports for all (or selected) FTSE 100 companies and caches
them locally so the pipeline doesn't need to re-fetch from corporate
websites that often block automated access.

Usage:
    # Download all FTSE 100 companies (sustainability reports, 2024)
    python scripts/bulk_download.py

    # Specific companies
    python scripts/bulk_download.py --companies "Aviva" "Shell" "BP"

    # Specific year
    python scripts/bulk_download.py --year 2023

    # Annual reports instead of sustainability
    python scripts/bulk_download.py --type annual

    # Use Playwright for blocked sites (slower, more reliable)
    python scripts/bulk_download.py --use-playwright

    # Retry only previously failed companies
    python scripts/bulk_download.py --retry-failed

    # Dry run — show what would be downloaded
    python scripts/bulk_download.py --dry-run
"""

import argparse
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

import requests
from exa_py import Exa

from data.ftse100 import FTSE_100
from pipeline.parser import _BROWSER_HEADERS, detect_source_type

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "document_cache")
MANIFEST_PATH = os.path.join(CACHE_DIR, "manifest.json")


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _load_manifest() -> dict:
    """Load the download manifest (tracks what we've cached)."""
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r") as f:
            return json.load(f)
    return {}


def _save_manifest(manifest: dict):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f, indent=2)


# ── Search ────────────────────────────────────────────────────────────

def _search_for_report(company_name: str, year: int, report_type: str, exa: Exa) -> list[dict]:
    """Search Exa for the company's report and return ranked candidates."""
    if report_type == "sustainability":
        query = (
            f"{company_name} greenhouse gas emissions scope 1 2 3 "
            f"{year} sustainability report ESG annual report PDF"
        )
    else:
        query = (
            f"{company_name} annual report financial statements "
            f"{year} revenue balance sheet PDF"
        )

    try:
        response = exa.search(query, num_results=10, type="auto")
        results = response.results
    except Exception as e:
        log.error(f"  Search failed for {company_name}: {e}")
        return []

    # Prefer PDFs
    pdf_results = [r for r in results if r.url.lower().split("?")[0].endswith(".pdf")]
    other_results = [r for r in results if not r.url.lower().split("?")[0].endswith(".pdf")]

    candidates = []
    for r in pdf_results + other_results:
        candidates.append({
            "url": r.url,
            "title": getattr(r, "title", "") or "(no title)",
            "is_pdf": r.url.lower().split("?")[0].endswith(".pdf"),
        })

    return candidates


# ── Download strategies ───────────────────────────────────────────────

def _download_direct(url: str) -> bytes:
    """Download via requests with fast timeout."""
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=(5, 60))
    resp.raise_for_status()
    if len(resp.content) < 500:
        raise ValueError(f"Response too small ({len(resp.content)} bytes)")
    return resp.content


def _download_playwright(url: str) -> bytes:
    """Download via headless Chrome (bypasses bot detection)."""
    from playwright.sync_api import sync_playwright

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
            response = page.goto(url, timeout=60_000)
            if response is None or not response.ok:
                status = response.status if response else "no response"
                raise ValueError(f"HTTP {status}")
            body = response.body()
            if len(body) < 500:
                raise ValueError(f"Response too small ({len(body)} bytes)")
            return body
        finally:
            browser.close()


def _download_wayback(url: str) -> bytes:
    """Download from Wayback Machine."""
    api_url = f"https://archive.org/wayback/available?url={url}"
    resp = requests.get(api_url, timeout=(5, 10))
    resp.raise_for_status()
    data = resp.json()

    snapshot = data.get("archived_snapshots", {}).get("closest")
    if not snapshot or not snapshot.get("available"):
        raise ValueError("No Wayback snapshot")

    archived_url = snapshot["url"].replace(
        "http://web.archive.org", "https://web.archive.org"
    )
    resp = requests.get(archived_url, headers=_BROWSER_HEADERS, timeout=(5, 60))
    resp.raise_for_status()
    if len(resp.content) < 500:
        raise ValueError(f"Archived response too small ({len(resp.content)} bytes)")
    return resp.content


def _download_with_fallback(url: str, use_playwright: bool = False) -> tuple[bytes, str]:
    """Try downloading with fallback chain. Returns (content, method)."""
    # Strategy 1: Direct
    try:
        return _download_direct(url), "direct"
    except Exception as e:
        log.debug(f"    Direct failed: {e}")

    # Strategy 2: Playwright (if enabled)
    if use_playwright:
        try:
            return _download_playwright(url), "playwright"
        except Exception as e:
            log.debug(f"    Playwright failed: {e}")

    # Strategy 3: Wayback Machine
    try:
        return _download_wayback(url), "wayback"
    except Exception as e:
        log.debug(f"    Wayback failed: {e}")

    raise RuntimeError(f"All download methods failed for {url}")


# ── Main bulk download logic ─────────────────────────────────────────

def download_company(
    company_name: str,
    year: int,
    report_type: str,
    exa: Exa,
    manifest: dict,
    use_playwright: bool = False,
    dry_run: bool = False,
) -> dict:
    """Download a report for one company. Returns status dict."""
    key = f"{company_name}|{year}|{report_type}"

    # Check if already cached
    if key in manifest and manifest[key].get("status") == "success":
        path = manifest[key]["path"]
        if os.path.exists(path):
            log.info(f"  ✅ Already cached: {company_name}")
            return {"status": "cached", "path": path}

    # Search for the report
    candidates = _search_for_report(company_name, year, report_type, exa)
    if not candidates:
        log.warning(f"  ❌ No search results: {company_name}")
        manifest[key] = {"status": "no_results", "company": company_name, "year": year}
        return {"status": "no_results"}

    if dry_run:
        top = candidates[0]
        log.info(f"  🔍 Would download: {top['title']}")
        log.info(f"     {top['url']}")
        return {"status": "dry_run", "url": top["url"], "title": top["title"]}

    # Try downloading each candidate (prefer PDFs)
    for i, candidate in enumerate(candidates[:5]):
        url = candidate["url"]
        title = candidate["title"]
        source_type = detect_source_type(url)
        ext = {"pdf": ".pdf", "html": ".html", "excel": ".xlsx"}.get(source_type, ".html")

        try:
            content, method = _download_with_fallback(url, use_playwright)

            # Save to cache
            os.makedirs(CACHE_DIR, exist_ok=True)
            safe_name = company_name.lower().replace(" ", "_").replace("&", "and")
            filename = f"{safe_name}_{year}_{report_type}{ext}"
            filepath = os.path.join(CACHE_DIR, filename)

            with open(filepath, "wb") as f:
                f.write(content)

            # Also write a .meta file
            with open(filepath + ".meta", "w", encoding="utf-8") as f:
                f.write(url)

            # Update manifest
            manifest[key] = {
                "status": "success",
                "company": company_name,
                "year": year,
                "type": report_type,
                "url": url,
                "title": title,
                "path": filepath,
                "method": method,
                "size_kb": len(content) // 1024,
                "source_type": source_type,
                "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            log.info(
                f"  ✅ {company_name} — {len(content) // 1024}KB "
                f"via {method} ({source_type})"
            )
            return {"status": "success", "path": filepath, "method": method}

        except Exception as e:
            log.debug(f"    Candidate {i + 1} failed: {e}")
            continue

    log.warning(f"  ❌ All candidates failed: {company_name}")
    manifest[key] = {
        "status": "failed",
        "company": company_name,
        "year": year,
        "candidates_tried": len(candidates[:5]),
    }
    return {"status": "failed"}


def main():
    parser = argparse.ArgumentParser(description="Bulk download FTSE 100 reports")
    parser.add_argument(
        "--companies", nargs="*",
        help="Specific company names (default: all FTSE 100)",
    )
    parser.add_argument(
        "--year", type=int, default=2024,
        help="Target reporting year (default: 2024)",
    )
    parser.add_argument(
        "--type", choices=["sustainability", "annual"], default="sustainability",
        help="Report type (default: sustainability)",
    )
    parser.add_argument(
        "--use-playwright", action="store_true",
        help="Enable Playwright fallback for blocked sites",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help="Only retry previously failed companies",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be downloaded without downloading",
    )
    parser.add_argument(
        "--delay", type=float, default=2.0,
        help="Delay between companies in seconds (default: 2.0)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
    )

    exa_key = os.environ.get("EXA_API_KEY")
    if not exa_key:
        log.error("EXA_API_KEY not set in environment")
        sys.exit(1)

    exa = Exa(api_key=exa_key)
    manifest = _load_manifest()

    # Build company list
    if args.companies:
        companies = [
            c for c in FTSE_100
            if c["name"] in args.companies
        ]
        # Also allow partial matches
        if not companies:
            companies = [
                c for c in FTSE_100
                if any(term.lower() in c["name"].lower() for term in args.companies)
            ]
        if not companies:
            log.error(f"No matching companies found for: {args.companies}")
            sys.exit(1)
    else:
        companies = FTSE_100

    # Filter to retry-failed only
    if args.retry_failed:
        failed_names = set()
        for key, info in manifest.items():
            if info.get("status") in ("failed", "no_results"):
                failed_names.add(info.get("company", ""))
        companies = [c for c in companies if c["name"] in failed_names]
        log.info(f"Retrying {len(companies)} previously failed companies")

    log.info(f"{'='*60}")
    log.info(f"Bulk Download: {len(companies)} companies, year={args.year}, type={args.type}")
    log.info(f"Playwright: {'enabled' if args.use_playwright else 'disabled'}")
    log.info(f"Cache dir: {os.path.abspath(CACHE_DIR)}")
    log.info(f"{'='*60}\n")

    stats = {"success": 0, "cached": 0, "failed": 0, "no_results": 0, "dry_run": 0}
    t_start = time.time()

    for i, company in enumerate(companies):
        name = company["name"]
        log.info(f"[{i + 1}/{len(companies)}] {name}")

        result = download_company(
            company_name=name,
            year=args.year,
            report_type=args.type,
            exa=exa,
            manifest=manifest,
            use_playwright=args.use_playwright,
            dry_run=args.dry_run,
        )
        stats[result["status"]] = stats.get(result["status"], 0) + 1

        # Save manifest after each company
        if not args.dry_run:
            _save_manifest(manifest)

        # Rate limit
        if i < len(companies) - 1:
            time.sleep(args.delay)

    elapsed = time.time() - t_start

    log.info(f"\n{'='*60}")
    log.info(f"Done in {elapsed:.0f}s")
    log.info(f"  ✅ Downloaded: {stats.get('success', 0)}")
    log.info(f"  📦 Already cached: {stats.get('cached', 0)}")
    log.info(f"  ❌ Failed: {stats.get('failed', 0)}")
    log.info(f"  🔍 No results: {stats.get('no_results', 0)}")

    if stats.get("failed", 0) > 0:
        log.info(f"\nTo retry failed companies: python scripts/bulk_download.py --retry-failed")
        log.info(f"To try with Playwright: python scripts/bulk_download.py --retry-failed --use-playwright")


if __name__ == "__main__":
    main()
