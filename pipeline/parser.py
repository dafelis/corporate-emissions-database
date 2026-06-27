"""Parse PDFs, Excel files, and HTML pages into markdown tables."""

import io
import os
import re
import tempfile

import pandas as pd
import requests
from bs4 import BeautifulSoup
from llama_parse import LlamaParse


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


def download_to_tempfile(url: str) -> str:
    """Download a URL to a temporary file and return the local path."""
    response = requests.get(url, headers=_BROWSER_HEADERS, timeout=120)
    if response.status_code == 403:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        headers = {**_BROWSER_HEADERS, "Referer": f"{parsed.scheme}://{parsed.netloc}/"}
        response = requests.get(url, headers=headers, timeout=120)
    response.raise_for_status()

    suffix = ".pdf" if url.lower().split("?")[0].endswith(".pdf") else ".html"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(response.content)
        return tmp.name


def parse_pdf(source: str, llama_api_key: str) -> list:
    """Parse a PDF and return LlamaParse document objects (one per page)."""
    parser = LlamaParse(
        api_key=llama_api_key,
        result_type="markdown",
        verbose=False,
    )

    if source.startswith("http://") or source.startswith("https://"):
        local_path = download_to_tempfile(source)
        documents = parser.load_data(local_path)
        os.unlink(local_path)
    else:
        documents = parser.load_data(source)

    return documents


def _extract_tables_from_text(text: str) -> list[str]:
    """Extract markdown table blocks from text."""
    tables = []
    current_lines: list[str] = []
    in_table = False

    for line in text.split("\n"):
        if line.strip().startswith("|"):
            current_lines.append(line)
            in_table = True
        else:
            if in_table and current_lines:
                table_text = "\n".join(current_lines)
                if re.search(r"\|[\s\-:]+\|", table_text):
                    tables.append(table_text)
                current_lines = []
                in_table = False

    if in_table and current_lines:
        table_text = "\n".join(current_lines)
        if re.search(r"\|[\s\-:]+\|", table_text):
            tables.append(table_text)

    return tables


def extract_tables_from_documents(documents: list) -> list[str]:
    """Extract all tables from parsed documents as markdown strings."""
    tables = []
    for doc in documents:
        tables.extend(_extract_tables_from_text(doc.text))
    return tables


def _df_to_markdown(df: pd.DataFrame) -> str:
    """Convert a DataFrame to a markdown table string."""
    df = df.fillna("").astype(str)
    headers = list(df.columns)
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def parse_excel(source: str) -> list[str]:
    """Parse an Excel file and return markdown tables, one per non-empty sheet."""
    if source.startswith("http://") or source.startswith("https://"):
        resp = requests.get(source, headers=_BROWSER_HEADERS, timeout=60)
        resp.raise_for_status()
        file_obj = io.BytesIO(resp.content)
    else:
        file_obj = source

    xl = pd.ExcelFile(file_obj)
    tables = []
    for sheet_name in xl.sheet_names:
        df = xl.parse(sheet_name)
        if not df.empty:
            tables.append(_df_to_markdown(df))
    return tables


def _html_table_to_markdown(tag) -> str:
    """Convert a BeautifulSoup <table> element to a markdown table string."""
    rows = tag.find_all("tr")
    if not rows:
        return ""
    table_data = [
        [cell.get_text(strip=True) for cell in row.find_all(["th", "td"])]
        for row in rows
    ]
    table_data = [r for r in table_data if any(r)]
    if not table_data:
        return ""
    header = table_data[0]
    n = len(header)
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * n) + " |",
    ]
    for row in table_data[1:]:
        padded = (row + [""] * n)[:n]
        lines.append("| " + " | ".join(padded) + " |")
    return "\n".join(lines)


def parse_html(url: str) -> list[str]:
    """Fetch an HTML page and extract all tables as markdown strings."""
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    tables = []
    for tag in soup.find_all("table"):
        md = _html_table_to_markdown(tag)
        if md:
            tables.append(md)
    return tables


def extract_html_text(url: str) -> str:
    """Fetch an HTML page and return clean text (fallback when no tables found)."""
    resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=60)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    return text[:50_000]


def detect_source_type(url: str) -> str:
    """Detect whether a URL points to a PDF, Excel file, or HTML page."""
    lower = url.lower().split("?")[0]
    if lower.endswith(".pdf"):
        return "pdf"
    if lower.endswith((".xlsx", ".xls")):
        return "excel"
    return "html"
