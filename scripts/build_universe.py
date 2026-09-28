"""Build a listed-company universe with identifiers, from free public sources.

Each source yields rows with whatever identifiers it actually provides. Nothing
is inferred: a blank cell means that source does not publish that identifier.

    python scripts/build_universe.py esma            # EU/EEA — ISIN + LEI + venue
    python scripts/build_universe.py nse twse asx sec
    python scripts/build_universe.py all --out data/universe

Sources and what they give (all verified 2026-09-27):

  esma  ESMA FIRDS FULINS_E — every equity instrument admitted to trading on an
        EU/EEA venue. Carries ISIN, name, issuer LEI, venue MIC and CFI, so it
        answers "which companies are listed in country X" directly. ~500k
        instrument records per file, 364 MB of XML, streamed not loaded.
  nse   National Stock Exchange of India equity list — symbol + name + ISIN.
  twse  Taiwan Stock Exchange ISIN service — 4-digit code + name + ISIN.
        strMode=2 is the main board, strMode=4 is TPEx. Big5 encoded.
  asx   ASX listed companies — ticker + name + GICS. No ISIN is published.
  sec   SEC company_tickers.json — CIK + ticker + name. No ISIN: US ISINs
        derive from CUSIP, which is licensed. CIK is the EDGAR key anyway.

Deliberately absent: Canada (SEDAR+ bulk is licensed and its terms forbid
collection), Saudi Arabia and South Africa (exchange sites block non-browser
clients). Those need a different approach, not a different parser.
"""

import argparse
import csv
import http.cookiejar
import io
import json
import os
import re
import ssl
import sys
import urllib.request
import zipfile
from xml.etree import ElementTree as ET

UA = {"User-Agent": "CorporateEmissionsDB dafelis@hotmail.com"}

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

ESMA_SOLR = (
    "https://registers.esma.europa.eu/solr/esma_registers_firds_files/select"
    "?q=file_type:FULINS+AND+file_name:FULINS_E*&rows={rows}"
    "&sort=publication_date+desc&wt=json"
)

# Venue MIC -> (country ISO2, exchange label). Operating MICs and the main
# segment MICs for the exchanges in scope; anything unlisted is still emitted,
# with a blank country, so nothing is silently dropped.
MIC_MAP = {
    "XAMS": ("NL", "Euronext Amsterdam"), "XPAR": ("FR", "Euronext Paris"),
    "XMIL": ("IT", "Euronext Milan"),     "XOSL": ("NO", "Euronext Oslo"),
    "XBRU": ("BE", "Euronext Brussels"),  "XDUB": ("IE", "Euronext Dublin"),
    "XMSM": ("IE", "Euronext Dublin"),    "XLIS": ("PT", "Euronext Lisbon"),
    "XATH": ("GR", "Athens (Euronext)"),
    "XETR": ("DE", "Deutsche Börse XETRA"), "XFRA": ("DE", "Frankfurt"),
    "XCSE": ("DK", "Nasdaq Copenhagen"),  "XSTO": ("SE", "Nasdaq Stockholm"),
    "XHEL": ("FI", "Nasdaq Helsinki"),    "XICE": ("IS", "Nasdaq Iceland"),
    "XTAL": ("EE", "Nasdaq Tallinn"),     "XRIS": ("LV", "Nasdaq Riga"),
    "XLIT": ("LT", "Nasdaq Vilnius"),
    "XMAD": ("ES", "BME Madrid"),         "XWBO": ("AT", "Vienna"),
    "XWAR": ("PL", "Warsaw"),             "XLUX": ("LU", "Luxembourg"),
    "XPRA": ("CZ", "Prague"),             "XBUD": ("HU", "Budapest"),
    "XLON": ("GB", "London Stock Exchange"),
}


def _lenient_ssl() -> ssl.SSLContext:
    """Verify certificates, but without Python 3.13's strict X.509 checks.

    isin.twse.com.tw presents a certificate with no Subject Key Identifier,
    which VERIFY_X509_STRICT rejects and browsers and curl accept. Relaxing
    that one flag keeps chain and hostname verification intact.
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _get(url: str, timeout: int = 300, headers: dict | None = None,
         context: ssl.SSLContext | None = None) -> bytes:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
        return resp.read()


def _browser_session_get(page_url: str, data_url: str, timeout: int = 120) -> bytes:
    """Fetch data_url after visiting page_url, carrying cookies across.

    NSE drops requests that arrive without a prior session and a browser
    User-Agent; one warm-up GET is enough.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = list(BROWSER.items())
    try:
        opener.open(page_url, timeout=timeout).read(2048)
    except Exception:
        pass  # the warm-up is best-effort; the data request may still work
    headers = dict(BROWSER, Referer=page_url)
    req = urllib.request.Request(data_url, headers=headers)
    return opener.open(req, timeout=timeout).read()


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ── ESMA FIRDS ────────────────────────────────────────────────────────────

def esma_latest_files(limit: int = 4) -> list[str]:
    """Download URLs for the most recent FULINS_E parts.

    ESMA splits one day's equities across several numbered parts
    (…_01of02, …_02of02); all parts of the newest date are needed for a
    complete picture, so take the newest date and keep every part of it.
    """
    data = json.loads(_get(ESMA_SOLR.format(rows=40), timeout=90))
    docs = data["response"]["docs"]
    if not docs:
        raise SystemExit("ESMA returned no FULINS_E files")
    newest = docs[0]["publication_date"][:10]
    same_day = [d for d in docs if d["publication_date"][:10] == newest]
    print(f"  newest publication date: {newest} ({len(same_day)} part(s))")
    return [d["download_link"] for d in same_day[:limit]]


def esma_records(url: str, cfi_prefix: str = "ES"):
    """Stream one FULINS_E zip and yield equity instrument dicts.

    cfi_prefix filters by ISO 10962 classification. The default "ES" is
    ordinary/common shares, which excludes rights, warrants, fund units and
    depositary receipts — the last of these matters, because counting ADRs
    would double-count companies that are already in their home market.
    """
    raw = _get(url)
    print(f"    {url.rsplit('/', 1)[-1]}: {len(raw):,} bytes zipped")
    zf = zipfile.ZipFile(io.BytesIO(raw))
    name = zf.namelist()[0]
    with zf.open(name) as fh:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            if _localname(elem.tag) != "RefData":
                continue
            rec = {}
            for node in elem.iter():
                tag = _localname(node.tag)
                text = (node.text or "").strip()
                if not text:
                    continue
                if tag == "Id" and "isin" not in rec and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}\d", text):
                    rec["isin"] = text
                elif tag == "FullNm":
                    rec.setdefault("name", text)
                elif tag == "ClssfctnTp":
                    rec.setdefault("cfi", text)
                elif tag == "Issr":
                    rec.setdefault("lei", text)
                elif tag == "NtnlCcy":
                    rec.setdefault("currency", text)
                elif tag == "Id" and len(text) == 4 and text.isupper():
                    rec.setdefault("mic", text)
            elem.clear()
            if rec.get("isin") and rec.get("cfi", "").startswith(cfi_prefix):
                # ISIN prefix is the issuing CSD's country — a far better
                # signal of where a company belongs than the trading venue,
                # since a single share is admitted on dozens of MTFs and
                # German regional exchanges it has no listing on.
                rec["isin_country"] = rec["isin"][:2]
                yield rec


def build_esma(out_dir: str, parts: int, all_venues: bool = False,
               cfi_prefix: str = "ES") -> str:
    """One row per (issuer, listing venue) for EU/EEA ordinary shares.

    An instrument is admitted to trading on many venues, so venues are
    restricted to the primary exchange MICs in MIC_MAP unless all_venues is
    set. Rows are then deduplicated on (issuer, MIC): a company with two
    share classes on one exchange is one company.
    """
    print("ESMA FIRDS (EU/EEA equities)")
    rows: dict[tuple, dict] = {}
    seen_instruments = 0
    for url in esma_latest_files(parts):
        n = 0
        for rec in esma_records(url, cfi_prefix):
            seen_instruments += 1
            mic = rec.get("mic", "")
            if mic not in MIC_MAP and not all_venues:
                continue
            country, exchange = MIC_MAP.get(mic, ("", ""))
            issuer = rec.get("lei") or rec["isin"]
            key = (issuer, mic)
            if key not in rows:
                rows[key] = {
                    "country": country, "exchange": exchange, "mic": mic,
                    "name": rec.get("name", ""), "isin": rec["isin"],
                    "isin_country": rec.get("isin_country", ""),
                    "lei": rec.get("lei", ""), "local_code": "",
                    "ticker": "", "currency": rec.get("currency", ""),
                    "cfi": rec.get("cfi", ""), "source": "ESMA FIRDS",
                }
            n += 1
        print(f"      {n:,} matching instrument/venue records")
    print(f"  {seen_instruments:,} instrument/venue records seen, "
          f"{len(rows):,} kept after venue filter and issuer dedupe")
    path = os.path.join(out_dir, "universe_esma.csv")
    _write(path, rows.values())
    _summarise(rows.values())
    return path


# ── National lists ────────────────────────────────────────────────────────

def build_nse(out_dir: str) -> str:
    print("NSE India")
    text = _browser_session_get(
        "https://www.nseindia.com/market-data/securities-available-for-trading",
        "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv",
    ).decode("utf-8", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        r = { (k or "").strip(): (v or "").strip() for k, v in r.items() }
        rows.append({
            "country": "IN", "exchange": "NSE India", "mic": "XNSE",
            "name": r.get("NAME OF COMPANY", ""), "isin": r.get("ISIN NUMBER", ""),
            "lei": "", "local_code": r.get("SYMBOL", ""),
            "ticker": f"{r.get('SYMBOL','')}.NS", "currency": "INR",
            "cfi": "", "source": "NSE EQUITY_L.csv",
        })
    path = os.path.join(out_dir, "universe_nse.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


# The TWSE ISIN page lists every security under category header rows. Only
# these two are operating companies: 股票 is ordinary shares and 創新板 the
# Innovation Board. Everything else is warrants (35k of them), ETFs, ETNs,
# REITs, preferred lines of companies already counted, or TDRs of foreign
# issuers that belong to their home market.
TW_COMPANY_CATEGORIES = {"股票", "創新板"}


def build_twse(out_dir: str) -> str:
    print("Taiwan TWSE + TPEx")
    rows = []
    for mode, exch, mic, suffix in (("2", "Taiwan Stock Exchange", "XTAI", "TW"),
                                    ("4", "Taipei Exchange (TPEx)", "ROCO", "TWO")):
        html = _get(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                    headers=BROWSER, context=_lenient_ssl()).decode("big5", "replace")
        category = None
        kept = 0
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).replace("　", " ").strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
            if len(cells) == 1 and cells[0]:
                category = cells[0]
                continue
            if len(cells) < 5 or not re.fullmatch(r"TW[A-Z0-9]{9}\d", cells[1] or ""):
                continue
            if category not in TW_COMPANY_CATEGORIES:
                continue
            code, _, name = (cells[0] or "").partition(" ")
            rows.append({
                "country": "TW", "exchange": exch, "mic": mic,
                "name": name.strip(), "isin": cells[1],
                "isin_country": "TW", "lei": "",
                "local_code": code.strip(), "ticker": f"{code.strip()}.{suffix}",
                "currency": "TWD", "cfi": "",
                "source": f"TWSE ISIN strMode={mode} ({category})",
            })
            kept += 1
        print(f"    {exch}: {kept:,} companies")
    path = os.path.join(out_dir, "universe_twse.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


def build_asx(out_dir: str) -> str:
    print("ASX Australia (no ISIN published)")
    text = _get("https://www.asx.com.au/asx/research/ASXListedCompanies.csv").decode("utf-8", "replace")
    start = text.find("Company name")
    rows = []
    for r in csv.DictReader(io.StringIO(text[start:])):
        code = (r.get("ASX code") or "").strip()
        if not code:
            continue
        rows.append({
            "country": "AU", "exchange": "ASX", "mic": "XASX",
            "name": (r.get("Company name") or "").strip(), "isin": "",
            "lei": "", "local_code": code, "ticker": f"{code}.AX",
            "currency": "AUD", "cfi": "", "source": "ASXListedCompanies.csv",
        })
    path = os.path.join(out_dir, "universe_asx.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


def build_sec(out_dir: str) -> str:
    print("SEC US (CIK, no ISIN — CUSIP is licensed)")
    data = json.loads(_get("https://www.sec.gov/files/company_tickers.json"))
    rows = []
    for entry in data.values():
        rows.append({
            "country": "US", "exchange": "US (Nasdaq/NYSE/other)", "mic": "",
            "name": entry.get("title", ""), "isin": "", "lei": "",
            "local_code": str(entry.get("cik_str", "")).zfill(10),
            "ticker": entry.get("ticker", ""), "currency": "USD",
            "cfi": "", "source": "SEC company_tickers.json",
        })
    path = os.path.join(out_dir, "universe_sec.csv")
    _write(path, rows)
    print(f"  {len(rows):,} filers with tickers "
          "(includes funds/ETFs/OTC — filter before use)")
    return path


# ── output ────────────────────────────────────────────────────────────────

FIELDS = ["country", "exchange", "mic", "name", "isin", "isin_country", "lei",
          "local_code", "ticker", "currency", "cfi", "source"]


def _write(path: str, rows) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"  -> {path}")


def _summarise(rows) -> None:
    rows = list(rows)
    by_exchange: dict[str, set] = {}
    for r in rows:
        label = r.get("exchange") or r.get("mic") or "(unmapped)"
        by_exchange.setdefault(label, set()).add(r.get("lei") or r.get("isin"))
    print("  distinct issuers by listing venue:")
    for label, issuers in sorted(by_exchange.items(), key=lambda kv: -len(kv[1])):
        print(f"      {label:28s} {len(issuers):>6,}")


BUILDERS = {"nse": build_nse, "twse": build_twse, "asx": build_asx, "sec": build_sec}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+",
                    choices=["esma", "nse", "twse", "asx", "sec", "all"])
    ap.add_argument("--out", default="data/universe", help="output directory")
    ap.add_argument("--esma-parts", type=int, default=4,
                    help="how many FULINS_E parts of the newest date to fetch")
    ap.add_argument("--all-venues", action="store_true",
                    help="keep every trading venue, not just primary exchanges "
                         "(a share is admitted on dozens of MTFs)")
    ap.add_argument("--cfi", default="ES",
                    help="CFI prefix filter; ES = ordinary shares (default), "
                         "E = all equity types incl. depositary receipts")
    args = ap.parse_args()

    names = list(BUILDERS) + ["esma"] if "all" in args.sources else args.sources
    os.makedirs(args.out, exist_ok=True)
    for name in names:
        try:
            if name == "esma":
                build_esma(args.out, args.esma_parts, args.all_venues, args.cfi)
            else:
                BUILDERS[name](args.out)
        except Exception as e:
            print(f"  {name}: FAILED — {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
