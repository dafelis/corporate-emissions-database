"""Tier S: India. BRSR emissions published as tagged XBRL, not prose.

SEBI requires the top listed companies to file a Business Responsibility
and Sustainability Report, and NSE publishes each filing as an XBRL
instance alongside the PDF. Principle 6 carries Scope 1 and Scope 2 as
tagged numeric facts in tCO2e, so — as with Taiwan — there is nothing to
extract: no PDF, no LLM call, no evidence page.

Verified 2026-11-25 against the live NSE feed:
    1,265 filings, 100% of them carrying an xbrlFile. Entities are
    identified by ISIN. Each instance reports two periods (current and
    prior year), so one fetch yields two company-years.

What this source does NOT give you
    Scope 3 has no value element in this taxonomy — only two booleans
    recording whether it applies and whether it was assured. Scope 3 for
    Indian companies still needs the search walk.

Keyed on the NSE trading symbol, which is the root of the Yahoo ticker
(SCHAEFFLER.NS), so no new identifier column is needed.
"""

import gzip
import json
import logging
import re
import urllib.request
import http.cookiejar
import xml.etree.ElementTree as ET
import zlib
from datetime import date as date_type

log = logging.getLogger(__name__)

NSE_LANDING = ("https://www.nseindia.com/companies-listing/"
               "corporate-filings-bussiness-sustainabilitiy-reports")
NSE_BRSR_API = ("https://www.nseindia.com/api/"
                "corporate-bussiness-sustainabilitiy?index=equities")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Local names, matched without their namespace URI: the SEBI taxonomy URI
# carries a version date (…/xbrl/2026-02-28/in-capmkt) that changes when
# the taxonomy is reissued, and the element names outlive it.
_F_SCOPE_1 = "TotalScope1Emissions"
_F_SCOPE_2 = "TotalScope2Emissions"
_F_INTENSITY = "TotalScope1AndScope2EmissionsIntensityPerRupeeOfTurnover"
_F_ASSURANCE = "AssuranceSubTypeForDetailsOfGreenHouseGasEmissionsAndItsIntensity"
_F_ASSURER = ("NameOfTheExternalAgencyThatUndertookIndependentAssessment"
              "OrEvaluationOrAssuranceForGreenHouseGasEmissions"
              "ExplanatoryTextBlock")
_F_SCOPE_3_APPLIES = "WhetherTotalScope3EmissionsAndItsIntensityIsApplicableToTheCompany"

# An 855 KB instance is typical; this is a sanity bound, not a tuning knob.
_MAX_XBRL_BYTES = 25 * 1024 * 1024

_index_cache: dict[str, list[dict]] | None = None
_opener = None


def _session():
    """A cookie-bearing opener. NSE rejects bare programmatic requests.

    The API 403s without the cookies that the filings landing page sets,
    so the landing page is visited first. This mirrors the warm-up in
    scripts/build_universe.py.
    """
    global _opener
    if _opener is not None:
        return _opener
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.addheaders = [
        ("User-Agent", _UA),
        ("Accept", "application/json,text/html,*/*"),
        ("Accept-Language", "en-US,en;q=0.9"),
        ("Accept-Encoding", "gzip, deflate"),
        ("Referer", "https://www.nseindia.com/"),
    ]
    try:
        with op.open(NSE_LANDING, timeout=40) as r:
            _body(r)
    except Exception as e:
        log.warning(f"  Tier S India: cookie warm-up failed: {e}")
    _opener = op
    return op


def _body(resp, max_bytes: int | None = None) -> bytes:
    """Read a response, undoing the content encoding NSE negotiates."""
    raw = resp.read(max_bytes) if max_bytes else resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        except Exception:
            pass
    return raw


def _number(raw) -> float | None:
    """Parse a reported figure, treating placeholders as missing.

    A reported 0 means "not disclosed" rather than "no emissions" — the
    same convention as the Taiwanese dataset, and visible here in filings
    that tag Scope 2 as 0 while reporting a real Scope 1.
    """
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "—", "NA", "N/A", "NIL", "Nil"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value != 0 else None


def fetch_brsr_index(force: bool = False) -> dict[str, list[dict]]:
    """Every BRSR filing NSE lists, grouped by trading symbol.

    The endpoint returns its whole result set in one request, so this is
    fetched once per process and reused for every company. Newest filing
    first within each symbol.
    """
    global _index_cache
    if _index_cache is not None and not force:
        return _index_cache

    out: dict[str, list[dict]] = {}
    try:
        op = _session()
        with op.open(NSE_BRSR_API, timeout=90) as r:
            rows = json.loads(_body(r).decode("utf-8", "replace")).get("data", [])
    except Exception as e:
        log.warning(f"  Tier S India: BRSR index fetch failed: {e}")
        _index_cache = {}
        return _index_cache

    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        xbrl = (row.get("xbrlFile") or "").strip()
        if not symbol or not xbrl:
            continue
        out.setdefault(symbol, []).append({
            "symbol": symbol,
            "company_name": str(row.get("companyName", "")).strip(),
            "xbrl_url": xbrl,
            "pdf_url": (row.get("attachmentFile") or "").strip(),
            "fy_from": row.get("fyFrom"),
            "fy_to": row.get("fyTo"),
            "submission_date": str(row.get("submissionDate", "")).strip(),
        })

    # Newest submission first, so the freshest filing is tried first.
    for filings in out.values():
        filings.sort(key=lambda f: _submission_sort_key(f), reverse=True)

    _index_cache = out
    log.info(f"  Tier S India: BRSR index has {len(rows):,} filings "
             f"across {len(out):,} symbols")
    return _index_cache


_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1)}


def _submission_sort_key(filing: dict) -> tuple:
    """Sortable key from NSE's "18-Nov-2026" submission dates."""
    m = re.fullmatch(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})",
                     filing.get("submission_date", ""))
    if m:
        return (int(m.group(3)), _MONTHS.get(m.group(2).title(), 0), int(m.group(1)))
    return (filing.get("fy_to") or 0, 0, 0)


def india_symbol_for(company) -> str | None:
    """The NSE symbol for a company, from its Yahoo ticker (TITAN.NS)."""
    ticker = (getattr(company, "ticker", "") or "").strip().upper()
    m = re.fullmatch(r"([A-Z0-9&\-]{1,20})\.(NS|BO)", ticker)
    return m.group(1) if m else None


def _local(tag: str) -> str:
    """Element local name, with any namespace URI stripped."""
    return tag.rsplit("}", 1)[-1]


def _parse_contexts(root) -> dict[str, dict]:
    """Map each context id to its period and whether it is dimensional.

    Scope 1 and 2 are also tagged against dimensional contexts for
    breakdowns; the entity-wide figure is the one with no scenario, so
    that distinction has to be kept.
    """
    contexts: dict[str, dict] = {}
    for el in root.iter():
        if _local(el.tag) != "context":
            continue
        cid = el.get("id")
        if not cid:
            continue
        start = end = instant = None
        dimensional = False
        isin = None
        for child in el.iter():
            name = _local(child.tag)
            if name == "startDate":
                start = (child.text or "").strip()
            elif name == "endDate":
                end = (child.text or "").strip()
            elif name == "instant":
                instant = (child.text or "").strip()
            elif name in ("scenario", "segment"):
                # Only dimensional if it actually carries members.
                if any(_local(g.tag) in ("explicitMember", "typedMember")
                       for g in child.iter()):
                    dimensional = True
            elif name == "identifier":
                isin = (child.text or "").strip()
        contexts[cid] = {
            "start": start, "end": end or instant,
            "dimensional": dimensional, "isin": isin,
        }
    return contexts


def _parse_units(root) -> dict[str, str]:
    """Map each unit id to a lowercase description of its measures."""
    units: dict[str, str] = {}
    for el in root.iter():
        if _local(el.tag) != "unit":
            continue
        uid = el.get("id")
        if not uid:
            continue
        measures = [(m.text or "").strip() for m in el.iter()
                    if _local(m.tag) == "measure"]
        units[uid] = " ".join(measures).lower()
    return units


def _as_date(text: str | None) -> date_type | None:
    if not text:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        return None
    try:
        return date_type(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_brsr_xbrl(xml: str) -> list[dict]:
    """Entity-wide Scope 1/2 facts from a BRSR instance, one per period.

    Returns an entry per reporting period present (typically the current
    and prior year). Facts in units other than tCO2e are discarded rather
    than guessed at.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        log.warning(f"  Tier S India: XBRL parse failed: {e}")
        return []

    contexts = _parse_contexts(root)
    units = _parse_units(root)

    # Facts that describe the whole filing rather than one period.
    assurance = assurer = ""
    scope_3_applies = ""
    by_context: dict[str, dict] = {}

    for el in root.iter():
        name = _local(el.tag)
        text = (el.text or "").strip()
        if name == _F_ASSURANCE and text:
            assurance = text
        elif name == _F_ASSURER and text:
            assurer = re.sub(r"<[^>]+>|&lt;[^&]*&gt;", " ", text)
            assurer = re.sub(r"\s+", " ", assurer).strip()
        elif name == _F_SCOPE_3_APPLIES and text:
            scope_3_applies = text
        elif name in (_F_SCOPE_1, _F_SCOPE_2, _F_INTENSITY):
            cid = el.get("contextRef") or ""
            ctx = contexts.get(cid)
            if not ctx or ctx["dimensional"]:
                continue
            if name in (_F_SCOPE_1, _F_SCOPE_2):
                unit = units.get(el.get("unitRef") or "", "")
                if "tco2e" not in unit:
                    log.info(f"  Tier S India: skipping {name} in unit "
                             f"{unit or 'unknown'!r}")
                    continue
            slot = by_context.setdefault(cid, {})
            slot[name] = _number(text)

    entries = []
    for cid, facts in by_context.items():
        ctx = contexts[cid]
        end = _as_date(ctx["end"])
        if not end:
            continue
        if facts.get(_F_SCOPE_1) is None and facts.get(_F_SCOPE_2) is None:
            continue
        entries.append({
            "reporting_year": end.year,
            "period_start": _as_date(ctx["start"]),
            "period_end": end,
            "isin": ctx.get("isin"),
            "scope_1": facts.get(_F_SCOPE_1),
            "scope_2_location": facts.get(_F_SCOPE_2),
            # No value element exists for Scope 3 in this taxonomy.
            "scope_3": None,
            "intensity": facts.get(_F_INTENSITY),
            "assurance": assurance,
            "assurer": assurer,
            "scope_3_applies": scope_3_applies,
            "context_id": cid,
        })

    entries.sort(key=lambda e: e["reporting_year"], reverse=True)
    return entries


def india_entries_for(company) -> tuple[list[dict], dict | None]:
    """Scope 1/2 entries for one company, newest period first.

    Returns (entries, filing) so the caller can cite the filing it came
    from. ([], None) when the company is not an Indian BRSR filer.
    """
    symbol = india_symbol_for(company)
    if not symbol:
        return [], None
    filings = fetch_brsr_index().get(symbol)
    if not filings:
        return [], None

    filing = filings[0]
    try:
        op = _session()
        with op.open(filing["xbrl_url"], timeout=120) as r:
            xml = _body(r, _MAX_XBRL_BYTES).decode("utf-8", "replace")
    except Exception as e:
        log.warning(f"  Tier S India: XBRL fetch failed for {symbol}: {e}")
        return [], None

    entries = parse_brsr_xbrl(xml)
    for entry in entries:
        entry["company_name"] = filing["company_name"]
        entry["symbol"] = symbol
        entry["source_url"] = filing["xbrl_url"]
        entry["pdf_url"] = filing["pdf_url"]
    return entries, filing


def describe_india_entry(entry: dict) -> tuple[str, str]:
    """Return (boundary, methodology_notes) for an entry.

    BRSR records the assurance level and the assuring agency for GHG as a
    whole rather than per scope, which is worth keeping: "Reasonable" is
    a materially stronger claim than "Limited".
    """
    # BRSR Principle 6 is reported for the entity as filed; the taxonomy
    # carries no consolidation-boundary element of its own.
    boundary = "As filed in BRSR Principle 6 (entity-wide)"

    notes = ["Source: SEBI BRSR XBRL filed with NSE "
             "(structured disclosure, no extraction)."]
    level = (entry.get("assurance") or "").strip()
    assurer = (entry.get("assurer") or "").strip()
    if level:
        notes.append(f"GHG assurance: {level[:60]}"
                     + (f" by {assurer[:120]}" if assurer else ""))
    else:
        notes.append("GHG assurance: not stated in the filing")
    if entry.get("intensity") is not None:
        notes.append(f"Reported Scope 1+2 intensity: "
                     f"{entry['intensity']} tCO2e per rupee of turnover")
    applies = (entry.get("scope_3_applies") or "").strip()
    notes.append("Scope 3: no value element exists in the BRSR taxonomy"
                 + (f"; company states applicability = {applies[:20]}"
                    if applies else ""))
    if entry.get("isin"):
        notes.append(f"Filer ISIN: {entry['isin']}")
    return boundary, " ".join(notes)


def is_india_verified(entry: dict) -> bool:
    """Whether the GHG figures carry external assurance."""
    return bool(re.search(r"reasonable|limited",
                          entry.get("assurance") or "", re.I))
