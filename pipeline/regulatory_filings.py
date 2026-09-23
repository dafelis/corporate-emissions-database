"""Tier 0 emissions sources: regulatory annual-report filings.

Two keyless public APIs, both verified 2026-09-23:

- filings.xbrl.org — ESEF inline-XBRL annual reports for every regulated-market
  issuer in the EU (FY2020+) and UK (FY2021+). One request by LEI returns the
  filings; `report_url` is the XHTML itself.
- FCA National Storage Mechanism — UK regulatory filings, including the
  pre-ESEF annual report PDFs needed for FY2020 and earlier.

Neither sits behind bot protection, so they also work for companies whose own
websites do (e.g. Imperva Incapsula on baesystems.com).
"""

import html
import json
import logging
import os
import re
import tempfile

import requests

log = logging.getLogger(__name__)

ESEF_API = "https://filings.xbrl.org"
NSM_SEARCH = "https://api.data.fca.org.uk/search?index=nsm-search"
NSM_ARTEFACTS = "https://data.fca.org.uk/artefacts/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
}

# Refuse absurdly large filings outright.
_MAX_REPORT_BYTES = 150 * 1024 * 1024

# An ESEF report runs to 30–70 MB of XHTML but its GHG table is a few KB, so
# only regions around these markers are converted to text. Converting a whole
# report cost several hundred MB of intermediate strings and OOM-killed the
# pipeline on a 2 GB instance at concurrency 4.
_GHG_MARKERS = (
    b"greenhouse gas", b"ghg emission", b"scope 1", b"scope 2",
    b"co2e", b"tco2", b"carbon dioxide equivalent",
)
_RAW_WINDOW = 150_000       # bytes of raw XHTML kept either side of a marker
_MAX_WINDOW_BYTES = 12 * 1024 * 1024   # ceiling on the total kept


# ── filings.xbrl.org ──────────────────────────────────────────────────────

def find_esef_filings(lei: str, timeout: int = 40) -> list[dict]:
    """Return ESEF filings for an LEI, newest period first.

    Each item: {year, period_end, country, report_url, package_url, fxo_id}.
    `year` is the calendar year of period_end.
    """
    params = {
        "page[size]": 25,
        "sort": "-period_end",
        "filter": json.dumps([{"name": "entity.identifier", "op": "eq", "val": lei}]),
    }
    resp = requests.get(
        f"{ESEF_API}/api/filings", params=params,
        headers={**_HEADERS, "Accept": "application/vnd.api+json"}, timeout=timeout,
    )
    resp.raise_for_status()

    out = []
    for f in resp.json().get("data", []):
        a = f.get("attributes", {})
        period_end, report_url = a.get("period_end"), a.get("report_url")
        if not period_end or not report_url:
            continue
        out.append({
            "year": int(period_end[:4]),
            "period_end": period_end,
            "country": a.get("country"),
            "report_url": ESEF_API + report_url,
            "package_url": (ESEF_API + a["package_url"]) if a.get("package_url") else None,
            "fxo_id": a.get("fxo_id"),
        })
    out.sort(key=lambda x: x["period_end"], reverse=True)
    return out


def _stream_to_tempfile(url: str, timeout: int) -> tuple[str, int]:
    """Stream a URL to a temporary file. Returns (path, size)."""
    resp = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
    resp.raise_for_status()
    declared = int(resp.headers.get("Content-Length") or 0)
    if declared > _MAX_REPORT_BYTES:
        raise ValueError(f"ESEF report too large ({declared:,} bytes)")

    fd, path = tempfile.mkstemp(suffix=".xhtml")
    total = 0
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                total += len(chunk)
                if total > _MAX_REPORT_BYTES:
                    raise ValueError(
                        f"ESEF report too large (>{_MAX_REPORT_BYTES:,} bytes)")
                f.write(chunk)
    except Exception:
        os.unlink(path)
        raise
    return path, total


def _marker_offsets(path: str, chunk_size: int = 1 << 20) -> list[int]:
    """Byte offsets of every GHG marker in a file, case-insensitively."""
    overlap = max(len(m) for m in _GHG_MARKERS) - 1
    offsets: list[int] = []
    tail = b""
    file_pos = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            buf = tail + chunk
            buf_start = file_pos - len(tail)
            low = buf.lower()
            for marker in _GHG_MARKERS:
                start = 0
                while True:
                    i = low.find(marker, start)
                    if i < 0:
                        break
                    offsets.append(buf_start + i)
                    start = i + 1
            file_pos += len(chunk)
            tail = buf[-overlap:] if overlap else b""
    return sorted(set(offsets))


def _marker_windows(offsets: list[int], size: int) -> list[tuple[int, int]]:
    """Merge marker offsets into byte ranges, densest first, under the cap.

    Windows are chosen by how many markers they contain rather than by
    position: the GHG table is usually late in the document, so taking the
    earliest windows would be exactly the wrong heuristic.
    """
    merged: list[list[int]] = []
    counts: list[int] = []
    for off in offsets:
        s, e = max(0, off - _RAW_WINDOW), min(size, off + _RAW_WINDOW)
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
            counts[-1] += 1
        else:
            merged.append([s, e])
            counts.append(1)

    ranked = sorted(zip(counts, merged), key=lambda t: t[0], reverse=True)
    kept, total = [], 0
    for _, (s, e) in ranked:
        if total + (e - s) > _MAX_WINDOW_BYTES:
            continue
        kept.append((s, e))
        total += e - s
    return sorted(kept)


def fetch_esef_report_text(report_url: str, timeout: int = 180) -> str:
    """Return normalised text of an ESEF report's GHG-relevant regions.

    The report is streamed to disk, scanned for emissions markers, and only
    generous windows around them are converted — peak memory is a few MB
    rather than the several hundred that converting the whole document
    costs. Returns "" when the report mentions no emissions terms at all.
    """
    path, size = _stream_to_tempfile(report_url, timeout)
    try:
        offsets = _marker_offsets(path)
        if not offsets:
            log.info("    No GHG markers in report (%.1f MB)", size / 1e6)
            return ""
        windows = _marker_windows(offsets, size)
        kept = sum(e - s for s, e in windows)
        log.info("    %.1f MB report, %d marker hit(s), %d window(s), %.1f MB converted",
                 size / 1e6, len(offsets), len(windows), kept / 1e6)

        parts = []
        with open(path, "rb") as f:
            for start, end in windows:
                f.seek(start)
                raw = f.read(end - start).decode("utf-8", "replace")
                parts.append(_normalise_text(_xhtml_to_text(raw)))
                del raw
        return "\n\n".join(parts)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _xhtml_to_text(xhtml: str) -> str:
    """Tag-strip without building a DOM (reports run to 70 MB).

    Block-level tags become newlines and table cells become tabs, so numbers
    in adjacent columns stay separated; inline tags (span, ix:*) vanish, which
    is what re-joins text that PDF-to-XHTML converters split into positioned
    runs.
    """
    s = re.sub(r"(?is)<(script|style|ix:header)\b.*?</\1\s*>", " ", xhtml)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    # Footnote markers are superscripts; keep them from gluing onto years ("20251").
    s = re.sub(r"(?is)<sup\b[^>]*>(.*?)</sup\s*>", r" \1 ", s)
    s = re.sub(r"(?i)</?(p|div|br|tr|li|h[1-6]|table|thead|tbody|tfoot|section|article|blockquote|ul|ol)\b[^>]*>", "\n", s)
    s = re.sub(r"(?i)</?(td|th)\b[^>]*>", "\t", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return s


_COMMA_NUMBER = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")


def _joins_number(prev_tok: str, cur_tok: str, next_line: str) -> bool:
    """Should two adjacent lines be glued because they are one split number?

    Handles the fragment patterns seen in PDF-to-XHTML conversions —
    "1"/"04,948", "372"/","/"1"/"50", "26"/"7"/",202", "107,3"/"60" — while
    refusing the look-alikes: a footnote marker after a complete number
    ("104,948"/"1"/"52,662") and a year followed by a marker ("2025"/"1").
    """
    if not (re.fullmatch(r"[\d,]+", prev_tok) and re.fullmatch(r"[\d,]+", cur_tok)):
        return False
    if prev_tok.endswith(",") or cur_tok.startswith(","):
        return True
    if _COMMA_NUMBER.match(prev_tok):
        return False  # complete number; what follows is a marker or the next value
    if re.match(r"0\d*,", cur_tok):
        return True  # no real number starts "0," — must be a split
    m = re.search(r",(\d{0,2})$", prev_tok)
    if m and cur_tok.isdigit() and len(m.group(1)) + len(cur_tok) <= 3:
        return True  # completing a short trailing group: "107,3" + "60"
    if (prev_tok.isdigit() and cur_tok.isdigit()
            and len(prev_tok) + len(cur_tok) <= 3 and next_line.startswith(",")):
        return True  # two bare fragments that a comma piece will continue
    return False


def _normalise_text(text: str) -> str:
    """Collapse whitespace and repair numbers split across lines.

    Inline-tag stripping in _xhtml_to_text already re-joins runs split into
    spans; this pass covers converters that emit each run as a block.
    """
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n")]
    lines = [l for l in lines if l]
    out: list[str] = []
    for i, line in enumerate(lines):
        if out:
            prev = out[-1]
            prev_tok = prev.rsplit(" ", 1)[-1]
            cur_tok = line.split(" ", 1)[0]
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            if _joins_number(prev_tok, cur_tok, next_line):
                out[-1] = prev + line
                continue
            # Single lowercase letter after a word ("Scop" / "e") — join.
            if (len(line) == 1 and line.isalpha() and line.islower()
                    and re.search(r"[A-Za-z]$", prev)):
                out[-1] = prev + line
                continue
        out.append(line)
    return "\n".join(out)


def find_ghg_sections(
    text: str,
    before: int = 1800,
    after: int = 2600,
    max_sections: int = 6,
    max_len: int = 6000,
) -> list[str]:
    """Return the text windows most likely to hold the GHG emissions table.

    Windows are centred on each "Scope 1" mention, merged while they overlap
    (up to max_len), then ranked by density of scope/CO2e terms and
    thousands-separated numbers. Best first.
    """
    hits = [m.start() for m in re.finditer(r"scope\s*[-–]?\s*1\b", text, re.I)]
    if not hits:
        return []

    windows: list[list[int]] = []
    for i in hits:
        s, e = max(0, i - before), min(len(text), i + after)
        if windows and s <= windows[-1][1] and (e - windows[-1][0]) <= max_len:
            windows[-1][1] = max(windows[-1][1], e)
        else:
            windows.append([s, e])

    scored = []
    for s, e in windows:
        w = text[s:e]
        low = w.lower()
        score = (
            3 * low.count("scope 2") + 3 * low.count("scope 3")
            + 4 * (low.count("tco2e") + low.count("co2e") + low.count("co2 e"))
            + 2 * low.count("tonnes")
            + len(re.findall(r"\b\d{1,3}(?:,\d{3})+\b", w))
            + (5 if ("scope 1" in low and "scope 2" in low) else 0)
        )
        scored.append((score, s, e))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [text[s:e] for _, s, e in scored[:max_sections]]


# ── FCA National Storage Mechanism ────────────────────────────────────────

def find_nsm_annual_reports(lei: str, timeout: int = 40) -> list[dict]:
    """Return annual-report filings on the NSM for an LEI, newest first.

    Only PDF and ESEF-package filings are returned (the RNS "Annual Financial
    Report" announcements are cover notes). Each item:
    {year, url, title, format ('pdf'|'esef'), published}.
    """
    hits: dict[str, dict] = {}
    for headline in ("Annual Report", "Annual Financial Report", "Annual Report and Accounts"):
        body = {
            "from": 0, "size": 40, "sort": "publication_date", "sortorder": "desc",
            "criteriaObj": {
                "criteria": [
                    {"name": "lei", "value": lei},
                    {"name": "headline", "value": headline},
                ],
                "dateCriteria": None,
            },
        }
        resp = requests.post(NSM_SEARCH, json=body, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
        for h in resp.json().get("hits", {}).get("hits", []):
            src = h.get("_source", {})
            if src.get("download_link"):
                hits[src["download_link"]] = src

    out = []
    for src in hits.values():
        head = src.get("headline") or ""
        fmt = (src.get("document_format") or "").lower()
        link = src["download_link"]
        if not re.search(r"annual (financial )?report", head, re.I):
            continue
        if re.search(r"notification|notice of|agm|proxy", head, re.I):
            continue
        is_pdf = fmt == "pdf" or link.lower().endswith(".pdf")
        is_esef = fmt == "tagged" or link.lower().endswith((".zip", ".xbri"))
        if not (is_pdf or is_esef):
            continue
        published = (src.get("publication_date") or "")[:10]
        m = re.search(r"\b(20\d\d)\b", head)
        if m:
            year = int(m.group(1))
        elif published:
            y, mo = int(published[:4]), int(published[5:7])
            year = y - 1 if mo <= 6 else y
        else:
            continue
        out.append({
            "year": year,
            "url": NSM_ARTEFACTS + link,
            "title": head,
            "format": "pdf" if is_pdf else "esef",
            "published": published,
        })
    out.sort(key=lambda x: (x["year"], x["published"]), reverse=True)
    return out
