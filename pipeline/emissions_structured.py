"""Tier S: emissions already published as structured data.

Some regulators publish GHG figures as data rather than as a document, so
there is nothing to extract — no PDF, no LLM call, no evidence page. That
is a different tier from the regulatory-filing route in
`regulatory_filings.py`, which still has to read a report.

Taiwan (verified 2026-10-18)
    TWSE and TPEx each expose a keyless JSON dataset carrying Scope 1, 2
    and 3 in tonnes CO2e, with the consolidation boundary and a
    third-party-verification flag per scope. 1,074 + 885 = 1,959
    companies. One reporting year only (ROC 114 = 2025), so it is a
    current snapshot; earlier years still need the sustainability reports
    on MOPS.

Keyed on the 4-digit company code, which is also the root of the Yahoo
ticker (1101.TW), so no new identifier column is needed.
"""

import json
import logging
import re
import ssl
import urllib.request

log = logging.getLogger(__name__)

TWSE_GHG = "https://openapi.twse.com.tw/v1/opendata/t187ap46_L_1"
TPEX_GHG = "https://www.tpex.org.tw/openapi/v1/t187ap46_O_1"

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "application/json",
}

# Field names are Traditional Chinese and stable across both exchanges.
_F_YEAR = "報告年度"            # ROC year: 114 = 2025
_F_CODE = "公司代號"
_F_NAME = "公司名稱"
_F_SCOPE = {
    "scope_1": ("範疇一排放量(噸CO2e)", "範疇一資料邊界", "範疇一取得驗證"),
    "scope_2_location": ("範疇二排放量(噸CO2e)", "範疇二資料邊界", "範疇二取得驗證"),
    "scope_3": ("範疇三排放量(噸CO2e)", "範疇三資料邊界", "範疇三取得驗證"),
}
_F_INTENSITY = "溫室氣體排放密集度(噸CO2e/單位)"
_VERIFIED_YES = "是"

_cache: dict[str, dict] | None = None


def _ssl_context() -> ssl.SSLContext:
    """Verify certificates, minus Python 3.13's strict X.509 extras.

    The Taiwanese exchange hosts present certificates with no Subject Key
    Identifier, which VERIFY_X509_STRICT rejects and browsers and curl
    accept. Clearing that one flag leaves chain and hostname verification
    in force.
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _get_json(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _number(raw) -> float | None:
    """Parse a TWSE numeric string, treating placeholders as missing."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in {"-", "—", "NA", "N/A", "不適用", "無"}:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    # 0 here means "not reported" rather than "no emissions".
    return value if value != 0 else None


def fetch_taiwan_emissions(force: bool = False) -> dict[str, dict]:
    """All Taiwanese GHG disclosures, keyed by 4-digit company code.

    Both exchanges return their whole dataset in one request, so this is
    fetched once per process and reused for every company.
    """
    global _cache
    if _cache is not None and not force:
        return _cache

    out: dict[str, dict] = {}
    for url, market in ((TWSE_GHG, "TWSE"), (TPEX_GHG, "TPEx")):
        try:
            rows = _get_json(url)
        except Exception as e:
            log.warning(f"  Tier S Taiwan: {market} fetch failed: {e}")
            continue
        for row in rows:
            code = str(row.get(_F_CODE, "")).strip()
            roc = str(row.get(_F_YEAR, "")).strip()
            if not code or not roc.isdigit():
                continue
            entry = {
                "reporting_year": int(roc) + 1911,
                "company_name": str(row.get(_F_NAME, "")).strip(),
                "market": market,
                "intensity": _number(row.get(_F_INTENSITY)),
            }
            for field, (val_key, boundary_key, verified_key) in _F_SCOPE.items():
                entry[field] = _number(row.get(val_key))
                entry[f"{field}_boundary"] = str(row.get(boundary_key, "")).strip()
                entry[f"{field}_verified"] = (
                    str(row.get(verified_key, "")).strip() == _VERIFIED_YES)
            out[code] = entry
        log.info(f"  Tier S Taiwan: {market} returned {len(rows):,} records")

    _cache = out
    return out


def taiwan_code_for(company) -> str | None:
    """The 4-digit code for a company, from its Yahoo ticker (1101.TW)."""
    ticker = (getattr(company, "ticker", "") or "").strip().upper()
    m = re.fullmatch(r"([0-9A-Z]{4,6})\.(TW|TWO)", ticker)
    return m.group(1) if m else None


def taiwan_record_for(company) -> dict | None:
    """The structured disclosure for one company, or None if absent."""
    code = taiwan_code_for(company)
    if not code:
        return None
    return fetch_taiwan_emissions().get(code)


def describe_taiwan_entry(entry: dict) -> tuple[str, str]:
    """Return (boundary, methodology_notes) for an entry.

    The three scopes can each carry their own consolidation boundary and
    assurance status, which matters for comparability, so both are recorded
    rather than collapsed into one figure.
    """
    boundaries = {
        field: entry.get(f"{field}_boundary", "")
        for field in _F_SCOPE
        if entry.get(field) is not None and entry.get(f"{field}_boundary")
    }
    # The most common boundary stands as the record's boundary.
    boundary = ""
    if boundaries:
        boundary = max(set(boundaries.values()), key=list(boundaries.values()).count)[:200]

    verified = [name.replace("scope_", "Scope ").replace("_location", " (location)")
                for name in _F_SCOPE if entry.get(f"{name}_verified")]
    notes = [
        f"Source: {entry['market']} statutory GHG dataset "
        f"(structured disclosure, no extraction)."
    ]
    notes.append("Third-party verified: "
                 + (", ".join(verified) if verified else "none of the reported scopes"))
    for field, text in boundaries.items():
        if text and text != boundary:
            label = field.replace("scope_", "Scope ").replace("_location", "")
            notes.append(f"{label} boundary: {text[:300]}")
    if entry.get("intensity") is not None:
        notes.append(f"Reported intensity: {entry['intensity']} tCO2e/unit")
    return boundary, " ".join(notes)
