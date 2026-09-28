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
import collections
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


def _col_index(ref: str) -> int:
    """'C7' -> 2. Spreadsheet column letters to a 0-based index."""
    letters = "".join(ch for ch in ref if ch.isalpha())
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_rows(data: bytes, sheet: int = 1):
    """Yield rows of an .xlsx as lists of strings, using only the stdlib.

    An xlsx is a zip of XML, so openpyxl is not needed for a flat sheet.
    Cells are placed by their column reference rather than in document
    order, because blank cells are simply omitted from the XML.
    """
    z = zipfile.ZipFile(io.BytesIO(data))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter()
                                  if _localname(t.tag) == "t"))
    name = f"xl/worksheets/sheet{sheet}.xml"
    if name not in z.namelist():
        return
    root = ET.fromstring(z.read(name))
    for row in root.iter():
        if _localname(row.tag) != "row":
            continue
        cells: list[str] = []
        for c in row:
            if _localname(c.tag) != "c":
                continue
            value = ""
            for child in c:
                tag = _localname(child.tag)
                if tag == "v":
                    value = child.text or ""
                elif tag == "is":
                    value = "".join(t.text or "" for t in child.iter()
                                    if _localname(t.tag) == "t")
            if c.get("t") == "s" and value.isdigit():
                value = shared[int(value)]
            i = _col_index(c.get("r", "")) if c.get("r") else len(cells)
            while len(cells) <= i:
                cells.append("")
            cells[i] = value
        if any(cells):
            yield cells


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


US_EXCHANGE_MIC = {"Nasdaq": ("XNAS", "Nasdaq"), "NYSE": ("XNYS", "New York Stock Exchange"),
                   "CBOE": ("BATS", "Cboe BZX")}


def build_sec(out_dir: str, keep_otc: bool = False) -> str:
    """US listings from the SEC's exchange-tagged ticker file.

    company_tickers_exchange.json carries the exchange, so OTC can be
    dropped — company_tickers.json cannot distinguish it. No ISIN: US ISINs
    derive from CUSIP, which is licensed. CIK is EDGAR's key regardless.
    """
    print("SEC US (CIK; no ISIN — CUSIP is licensed)")
    data = json.loads(_get("https://www.sec.gov/files/company_tickers_exchange.json"))
    fields = data["fields"]
    idx = {name: fields.index(name) for name in ("cik", "name", "ticker", "exchange")}
    rows, skipped = [], collections.Counter()
    for entry in data["data"]:
        exch = entry[idx["exchange"]]
        if exch not in US_EXCHANGE_MIC and not keep_otc:
            skipped[exch or "(none)"] += 1
            continue
        mic, label = US_EXCHANGE_MIC.get(exch, ("", exch or ""))
        rows.append({
            "country": "US", "exchange": label, "mic": mic,
            "name": entry[idx["name"]], "isin": "", "isin_country": "",
            "lei": "", "local_code": str(entry[idx["cik"]]).zfill(10),
            "ticker": entry[idx["ticker"]], "currency": "USD", "cfi": "",
            "source": "SEC company_tickers_exchange.json",
        })
    path = os.path.join(out_dir, "universe_sec.csv")
    _write(path, rows)
    print(f"  {len(rows):,} exchange-listed; skipped {dict(skipped)}")
    print("  NB still includes ETFs and closed-end funds listed on those "
          "exchanges — filter on SIC 6726 via EDGAR if that matters")
    return path


def build_six(out_dir: str) -> str:
    """SIX Swiss equity issuers — publishes ISIN and a primary-listing flag."""
    print("SIX Swiss Exchange")
    text = _get("https://www.six-group.com/sheldon/equity_issuers/v1/equity_issuers.csv",
                headers=BROWSER).decode("utf-8-sig", "replace")
    rows = []
    for r in csv.DictReader(io.StringIO(text), delimiter=";"):
        isin = (r.get("ISIN") or "").strip()
        rows.append({
            "country": (r.get("Country") or "").strip() or "CH",
            "exchange": "SIX Swiss Exchange",
            "mic": (r.get("Trading platform") or "XSWX").strip(),
            "name": (r.get("Company") or "").strip(), "isin": isin,
            "isin_country": isin[:2], "lei": "",
            "local_code": (r.get("Symbol") or "").strip(),
            "ticker": f"{(r.get('Symbol') or '').strip()}.SW",
            "currency": (r.get("Traded Currency") or "").strip(), "cfi": "",
            "source": "SIX equity_issuers.csv"
                      + (" [primary]" if (r.get("Primary listing") or "").upper() == "TRUE" else ""),
        })
    path = os.path.join(out_dir, "universe_six.csv")
    _write(path, rows)
    primary = sum(1 for r in rows if "[primary]" in r["source"])
    print(f"  {len(rows):,} issuers ({primary:,} with SIX as primary listing; "
          "the rest are foreign lines)")
    return path


def build_tsx(out_dir: str) -> str:
    """Toronto Stock Exchange company directory. No ISIN published."""
    print("Toronto Stock Exchange (no ISIN published)")
    data = json.loads(_get("https://www.tsx.com/json/company-directory/search/tsx/%5E*",
                           headers=BROWSER))
    rows = []
    for r in data.get("results", []):
        symbol = (r.get("symbol") or "").strip()
        rows.append({
            "country": "CA", "exchange": "Toronto Stock Exchange", "mic": "XTSE",
            "name": (r.get("name") or "").strip(), "isin": "", "isin_country": "",
            # Yahoo writes Canadian class/unit suffixes with a hyphen:
            # TSX "IGBT.UN" is "IGBT-UN.TO", not "IGBT.UN.TO".
            "lei": "", "local_code": symbol,
            "ticker": f"{symbol.replace('.', '-')}.TO" if symbol else "",
            "currency": "CAD", "cfi": "", "source": "tsx.com company-directory",
        })
    path = os.path.join(out_dir, "universe_tsx.csv")
    _write(path, rows); print(f"  {len(rows):,} listings (includes trusts and ETFs)")
    return path


def build_hkex(out_dir: str) -> str:
    """HKEX List of Securities. Equities only; the file covers every product."""
    print("Hong Kong HKEX")
    data = _get("https://www.hkex.com.hk/eng/services/trading/securities/"
                "securitieslists/ListOfSecurities.xlsx", headers=BROWSER)
    rows, header, hdr_idx = [], None, {}
    for cells in _xlsx_rows(data):
        joined = [c.strip() for c in cells]
        if header is None:
            if any(c.lower().startswith("stock code") for c in joined):
                header = joined
                hdr_idx = {c.lower(): i for i, c in enumerate(header)}
            continue
        def cell(*names):
            for n in names:
                for key, i in hdr_idx.items():
                    if key.startswith(n) and i < len(joined):
                        return joined[i]
            return ""
        code = cell("stock code")
        name = cell("name of securities", "stock short name")
        category = cell("category", "classification")
        if not code or not code.isdigit():
            continue
        if category and "equity" not in category.lower():
            continue
        rows.append({
            "country": "HK", "exchange": "Hong Kong Stock Exchange", "mic": "XHKG",
            "name": name, "isin": "", "isin_country": "", "lei": "",
            # HKEX pads to 5 digits; Yahoo uses 4 (00700 -> 0700.HK).
            "local_code": code.zfill(5), "ticker": f"{int(code):04d}.HK",
            "currency": "HKD", "cfi": "", "source": "HKEX ListOfSecurities.xlsx",
        })
    path = os.path.join(out_dir, "universe_hkex.csv")
    _write(path, rows); print(f"  {len(rows):,} equity listings")
    return path


def build_b3(out_dir: str, page_size: int = 20) -> str:
    """B3 Brazil listed companies. Paginated; keyed on CNPJ and CVM code."""
    print("B3 Brazil")
    import base64
    import time
    base = ("https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/"
            "CompanyCall/GetInitialCompanies/")
    rows, page, total_pages = [], 1, None
    while True:
        token = base64.b64encode(json.dumps(
            {"language": "pt-br", "pageNumber": page, "pageSize": page_size}
        ).encode()).decode()
        payload = json.loads(_get(base + token, headers=BROWSER, timeout=60))
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if total_pages is None:
            total_pages = (payload.get("page") or {}).get("totalPages") or 1
            print(f"    {(payload.get('page') or {}).get('totalRecords')} companies "
                  f"over {total_pages} pages")
        for r in payload.get("results", []):
            ticker_root = (r.get("issuingCompany") or "").strip()
            rows.append({
                "country": "BR", "exchange": "B3", "mic": "BVMF",
                "name": (r.get("companyName") or "").strip(), "isin": "",
                "isin_country": "", "lei": "",
                "local_code": (r.get("cnpj") or "").strip(),
                "ticker": f"{ticker_root}3.SA" if ticker_root else "",
                "currency": "BRL", "cfi": "",
                "source": f"B3 GetInitialCompanies (CVM {r.get('codeCVM','')})",
            })
        page += 1
        if page > (total_pages or 1):
            break
        time.sleep(0.2)
    path = os.path.join(out_dir, "universe_b3.csv")
    _write(path, rows)
    print(f"  {len(rows):,} companies (ticker root only — B3 appends 3/4/11 "
          "for ON/PN/UNIT classes)")
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


def build_jpx(out_dir: str) -> str:
    """Tokyo listed issues. The workbook covers ETFs and REITs too."""
    print("Japan JPX (Tokyo)")
    data = _get("https://www.jpx.co.jp/markets/statistics-equities/misc/"
                "tvdivq0000001vg2-att/data_j.xlsx", headers=BROWSER)
    rows, header = [], None
    for cells in _xlsx_rows(data):
        if header is None:
            header = cells
            continue
        if len(cells) < 4:
            continue
        code, name, segment = cells[1].strip(), cells[2].strip(), cells[3].strip()
        if not code.isdigit():
            continue
        # 市場・商品区分: keep the equity markets, drop ETF・ETN and REITs.
        if "内国株式" not in segment and "外国株式" not in segment:
            continue
        rows.append({
            "country": "JP", "exchange": f"Tokyo ({segment})", "mic": "XTKS",
            "name": name, "isin": "", "isin_country": "", "lei": "",
            "local_code": code, "ticker": f"{code}.T", "currency": "JPY",
            "cfi": "", "source": "JPX data_j.xlsx",
        })
    path = os.path.join(out_dir, "universe_jpx.csv")
    _write(path, rows); print(f"  {len(rows):,} companies (ETFs/REITs excluded)")
    return path


def build_krx(out_dir: str) -> str:
    """Korean listed companies from KIND, the KRX disclosure portal.

    corpList.do is labelled application/vnd.ms-excel but is an EUC-KR HTML
    table. It covers both KOSPI (유가증권) and KOSDAQ.
    """
    print("Korea KRX (via KIND)")
    raw = _get("https://kind.krx.co.kr/corpgeneral/corpList.do"
               "?method=download&searchType=13", headers=BROWSER)
    html = raw.decode("euc-kr", "replace")
    market_mic = {"유가증권": ("XKRX", "KOSPI", "KS"),
                  "코스닥": ("XKOS", "KOSDAQ", "KQ"),
                  "코넥스": ("XKON", "KONEX", "KN")}
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 3 or not re.fullmatch(r"[0-9A-Z]{6}", cells[2] or ""):
            continue
        name, market, code = cells[0], cells[1], cells[2]
        mic, label, suffix = market_mic.get(market, ("XKRX", market, "KS"))
        rows.append({
            "country": "KR", "exchange": f"Korea Exchange ({label})", "mic": mic,
            "name": name, "isin": "", "isin_country": "", "lei": "",
            "local_code": code, "ticker": f"{code}.{suffix}", "currency": "KRW",
            "cfi": "", "source": "KIND corpList.do",
        })
    path = os.path.join(out_dir, "universe_krx.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


def build_sse(out_dir: str) -> str:
    """Shanghai listed companies. Needs a Referer; returns the full list at once."""
    print("Shanghai Stock Exchange")
    url = ("https://query.sse.com.cn/sseQuery/commonQuery.do"
           "?sqlId=COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L"
           "&pageHelp.pageSize=10000&pageHelp.pageNo=1"
           "&pageHelp.beginPage=1&pageHelp.endPage=1")
    payload = json.loads(_get(url, headers=dict(BROWSER, Referer="https://www.sse.com.cn/")))
    rows = []
    for r in payload.get("result", []):
        code = (r.get("A_STOCK_CODE") or "").strip()
        if not code:
            continue
        board = (r.get("LIST_BOARD") or "").strip()
        rows.append({
            "country": "CN",
            "exchange": "Shanghai (STAR)" if board == "2" else "Shanghai Stock Exchange",
            "mic": "XSHG",
            "name": (r.get("FULL_NAME_IN_ENGLISH") or r.get("COMPANY_ABBR_EN")
                     or r.get("FULL_NAME") or "").strip(),
            "isin": "", "isin_country": "", "lei": "",
            "local_code": code, "ticker": f"{code}.SS", "currency": "CNY",
            "cfi": "", "source": "SSE commonQuery",
        })
    path = os.path.join(out_dir, "universe_sse.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


def build_szse(out_dir: str) -> str:
    """Shenzhen listed companies.

    szse.cn refused connections outright from a UK/EU egress (empty reply on
    http, TLS handshake failure on https) — most likely geo-filtering rather
    than anything about the request. Retry from a different network before
    concluding it is unavailable.
    """
    print("Shenzhen Stock Exchange")
    url = ("https://www.szse.cn/api/report/ShowReport/data"
           "?SHOWTYPE=JSON&CATALOGID=1110&TABKEY=tab1&PAGENO=1&PAGESIZE=5000")
    payload = json.loads(_get(url, headers=dict(
        BROWSER, Referer="https://www.szse.cn/market/product/stock/list/index.html")))
    blocks = payload if isinstance(payload, list) else [payload]
    rows = []
    for block in blocks:
        for r in (block.get("data") or []):
            code = re.sub(r"<[^>]+>", "", str(r.get("zqdm", ""))).strip()
            name = re.sub(r"<[^>]+>", "", str(r.get("zqjc", ""))).strip()
            if not code:
                continue
            rows.append({
                "country": "CN", "exchange": "Shenzhen Stock Exchange", "mic": "XSHE",
                "name": name, "isin": "", "isin_country": "", "lei": "",
                "local_code": code, "ticker": f"{code}.SZ", "currency": "CNY",
                "cfi": "", "source": "SZSE ShowReport",
            })
    path = os.path.join(out_dir, "universe_szse.csv")
    _write(path, rows); print(f"  {len(rows):,} companies")
    return path


BUILDERS = {"nse": build_nse, "twse": build_twse, "asx": build_asx,
            "sec": build_sec, "six": build_six, "tsx": build_tsx,
            "hkex": build_hkex, "b3": build_b3, "jpx": build_jpx,
            "krx": build_krx, "sse": build_sse, "szse": build_szse}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+",
                    choices=["esma", "nse", "twse", "asx", "sec", "six",
                             "tsx", "hkex", "b3", "jpx", "krx", "sse",
                             "szse", "all"])
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
