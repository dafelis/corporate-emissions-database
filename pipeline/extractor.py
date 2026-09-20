"""Extract emissions data from tables using Claude."""

import json

import anthropic


EMISSIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "emissions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reporting_year": {"type": "integer"},
                    "period_start": {"type": "string", "description": "Start of reporting period in YYYY-MM-DD format, e.g. '2025-01-01' for calendar year or '2025-04-01' for April fiscal year. Null if not stated."},
                    "period_end": {"type": "string", "description": "End of reporting period in YYYY-MM-DD format, e.g. '2025-12-31' for calendar year or '2026-03-31' for March fiscal year. Null if not stated."},
                    "scope_1": {"type": "number", "description": "Scope 1 emissions value, or null if not found"},
                    "scope_2_location": {"type": "number", "description": "Scope 2 location-based value, or null"},
                    "scope_2_market": {"type": "number", "description": "Scope 2 market-based value, or null"},
                    "scope_3": {"type": "number", "description": "Scope 3 total value, or null"},
                    "scope_3_categories": {"type": "string", "description": "Which Scope 3 categories are included, if stated"},
                    "unit": {"type": "string", "description": "Unit of measurement, e.g. 'tonnes CO2e', 'kt CO2e', 'Mt CO2e'"},
                    "boundary": {"type": "string", "description": "Reporting boundary: 'operational control', 'equity share', or 'financial control', if stated"},
                },
                "required": ["reporting_year"],
                "additionalProperties": False,
            },
            "description": "One entry per reporting year found in the data.",
        },
        "methodology_notes": {
            "type": "string",
            "description": "Any methodological notes, restatements, or caveats mentioned alongside the emissions data. Empty string if none.",
        },
        "confidence_score": {
            "type": "integer",
            "description": "How confident you are that the extracted values are correct, 0-100.",
        },
    },
    "required": ["emissions", "methodology_notes", "confidence_score"],
    "additionalProperties": False,
}


def find_emissions_tables(
    tables: list[str],
    client: anthropic.Anthropic,
) -> list[dict]:
    """Rank tables by likelihood of containing emissions data.

    Returns a list of {index, score} dicts, best first.
    """
    if not tables:
        return []

    tables_text = "\n\n".join(
        f"TABLE {i}:\n{table[:600]}{'...' if len(table) > 600 else ''}"
        for i, table in enumerate(tables)
    )

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=(
            "You are an expert at identifying greenhouse gas emissions data in tables. "
            "Given table previews, rank ALL tables from most to least likely to contain "
            "Scope 1, 2, or 3 emissions data. Assign each a relevance score 0-100. "
            "Every table must appear in the ranking."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    "Find tables containing greenhouse gas emissions data "
                    "(Scope 1, Scope 2, Scope 3).\n\n"
                    f"Table previews:\n\n{tables_text}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ranked": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "score": {"type": "integer"},
                                },
                                "required": ["index", "score"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["ranked"],
                    "additionalProperties": False,
                },
            }
        },
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        return []
    data = json.loads(text)
    return data.get("ranked", [])


def extract_emissions(
    table: str,
    company_name: str,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    """Extract Scope 1/2/3 emissions from a table.

    Returns structured emissions data matching EMISSIONS_SCHEMA.
    Defaults to Haiku for cost efficiency; caller can pass a stronger model.
    """
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=(
            "You are an expert at extracting greenhouse gas emissions data from tables "
            "in sustainability reports. Extract Scope 1, Scope 2 (both location-based and "
            "market-based if available), and Scope 3 emissions for EVERY year present in the "
            "table — including prior-year comparison columns and historical trend data. "
            "Many reports show 2-5 years side by side; extract ALL of them, not just the "
            "most recent. "
            "Normalise all values to the same unit (prefer tonnes CO2e). "
            "If the table uses kt or Mt, convert to tonnes. "
            "CRITICAL: Only extract ABSOLUTE emissions values with units like tonnes CO2e, "
            "MtCO2e, ktCO2e, GtCO2e etc. Do NOT extract: percentage values (%), reduction "
            "targets, percentage changes, emissions intensity ratios (e.g. per revenue, "
            "per employee), or index values. If a cell contains '50%' or "
            "'50% reduction', that is NOT an emissions value of 50. "
            "IMPORTANT: Identify the reporting period for each year. Look for phrases like "
            "'year ended 31 December', 'for the 12 months to 31 March', 'calendar year', "
            "'FY2025' etc. Set period_start and period_end as YYYY-MM-DD dates. "
            "For example, 'year ended 31 March 2025' means period_start='2024-04-01', "
            "period_end='2025-03-31'. If not stated, set both to null. "
            "Note any methodology information, restatements, or caveats. "
            "If a scope is not present in the table, set its value to null. "
            "Be precise — extract the exact numbers from the table."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract all greenhouse gas emissions data for {company_name} "
                    f"from this table:\n\n{table}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": EMISSIONS_SCHEMA,
            }
        },
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text)


def extract_emissions_from_text(
    text: str,
    company_name: str,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    """Extract emissions data from free-form text (fallback when no tables found)."""
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=(
            "You are an expert at extracting greenhouse gas emissions data from documents. "
            "Extract Scope 1, Scope 2 (both location-based and market-based if available), "
            "and Scope 3 emissions for ALL years mentioned. "
            "Normalise all values to tonnes CO2e. "
            "CRITICAL: Only extract ABSOLUTE emissions values with units like tonnes CO2e, "
            "MtCO2e, ktCO2e, GtCO2e etc. Do NOT extract: percentage values (%), reduction "
            "targets, percentage changes, emissions intensity ratios (e.g. per revenue, "
            "per employee), or index values. If the text says '50%' or "
            "'50% reduction', that is NOT an emissions value of 50. "
            "IMPORTANT: Identify the reporting period for each year. Look for phrases like "
            "'year ended 31 December', 'for the 12 months to 31 March', 'calendar year', "
            "'FY2025' etc. Set period_start and period_end as YYYY-MM-DD dates. "
            "If not stated, set both to null. "
            "If a scope is not found, set its value to null. "
            "Be precise — extract exact numbers only, do not estimate."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract all greenhouse gas emissions data for {company_name} "
                    f"from this document text:\n\n{text}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": EMISSIONS_SCHEMA,
            }
        },
    )

    text_out = next((b.text for b in response.content if b.type == "text"), None)
    if text_out is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text_out)


_PDF_EMISSIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "emissions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reporting_year": {"type": "integer"},
                    "period_start": {"type": "string", "description": "Start of reporting period in YYYY-MM-DD format. Null if not stated."},
                    "period_end": {"type": "string", "description": "End of reporting period in YYYY-MM-DD format. Null if not stated."},
                    "scope_1": {"type": "number", "description": "Scope 1 emissions value, or null if not found"},
                    "scope_1_page": {"type": "integer", "description": "Document page position (1-indexed) where the Scope 1 value was found, or null"},
                    "scope_2_location": {"type": "number", "description": "Scope 2 location-based value, or null"},
                    "scope_2_market": {"type": "number", "description": "Scope 2 market-based value, or null"},
                    "scope_2_page": {"type": "integer", "description": "Document page position (1-indexed) where the Scope 2 value was found, or null"},
                    "scope_3": {"type": "number", "description": "Scope 3 total value, or null"},
                    "scope_3_page": {"type": "integer", "description": "Document page position (1-indexed) where the Scope 3 value was found, or null"},
                    "scope_3_categories": {"type": "string", "description": "Which Scope 3 categories are included, if stated"},
                    "unit": {"type": "string", "description": "Unit of measurement, e.g. 'tonnes CO2e', 'kt CO2e', 'Mt CO2e'"},
                    "boundary": {"type": "string", "description": "Reporting boundary: 'operational control', 'equity share', or 'financial control', if stated"},
                },
                "required": ["reporting_year"],
                "additionalProperties": False,
            },
            "description": "One entry per reporting year found in the document.",
        },
        "methodology_notes": {
            "type": "string",
            "description": "Any methodological notes, restatements, or caveats mentioned alongside the emissions data. Empty string if none.",
        },
        "confidence_score": {
            "type": "integer",
            "description": "How confident you are that the extracted values are correct, 0-100.",
        },
    },
    "required": ["emissions", "methodology_notes", "confidence_score"],
    "additionalProperties": False,
}


def extract_emissions_from_pdf(
    pdf_path: str,
    company_name: str,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
    num_pages: int = 0,
) -> dict:
    """Extract emissions data by sending a PDF directly to Claude.

    The PDF should be pre-filtered to contain only emissions-relevant pages.
    Claude reads the document with full spatial layout preserved and reports
    which page each data point came from (position in this document, not
    any page number printed on the pages).
    """
    import base64

    with open(pdf_path, "rb") as f:
        pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

    page_instruction = (
        f"PAGE TRACKING: This document has exactly {num_pages} pages. "
        f"Pages are numbered by their POSITION in this document: 1 is the first page, "
        f"2 is the second, up to {num_pages}. Ignore any page numbers printed on the "
        "pages themselves — those are from the original report and do not match. "
        "For each scope value you extract, report which page (by position) you read "
        "it from in scope_1_page, scope_2_page, and scope_3_page. Scope 1 and Scope 3 "
        "may be on different pages — report each one individually. "
        "If a scope value is null, set its page to null too. "
        if num_pages > 0 else ""
    )

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=(
            "You are an expert at extracting greenhouse gas emissions data from "
            "sustainability reports. You are given a PDF document (or a subset of pages "
            "from a larger report). "
            "Extract Scope 1, Scope 2 (both location-based and market-based if available), "
            "and Scope 3 emissions for EVERY year present in the document — including "
            "prior-year comparison columns and historical trend data. "
            "Normalise all values to the same unit (prefer tonnes CO2e). "
            "If the document uses kt or Mt, convert to tonnes. "
            "CRITICAL: Only extract ABSOLUTE emissions values with units like tonnes CO2e, "
            "MtCO2e, ktCO2e, GtCO2e etc. Do NOT extract: percentage values (%), reduction "
            "targets, percentage changes, emissions intensity ratios (e.g. per revenue, "
            "per employee), or index values. If a cell contains '50%' or "
            "'50% reduction', that is NOT an emissions value of 50. "
            "IMPORTANT: Identify the reporting period for each year. Look for phrases like "
            "'year ended 31 December', 'for the 12 months to 31 March', 'calendar year', "
            "'FY2025' etc. Set period_start and period_end as YYYY-MM-DD dates. "
            "If not stated, set both to null. "
            + page_instruction +
            "If a scope is not present, set its value to null. "
            "If the document does not contain any emissions data, return an empty "
            "emissions array. "
            "Be precise — read the exact numbers from the document."
        ),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Extract all greenhouse gas emissions data for {company_name} "
                            f"from this document."
                        ),
                    },
                ],
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": _PDF_EMISSIONS_SCHEMA,
            }
        },
    )

    text_out = next((b.text for b in response.content if b.type == "text"), None)
    if text_out is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text_out)


_VERIFY_PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "contains_values": {
            "type": "boolean",
            "description": "True if this page contains the emissions values listed in the query.",
        },
        "scope_1_value": {
            "type": "number",
            "description": "Scope 1 value visible on this page, or null if not present.",
        },
        "scope_2_value": {
            "type": "number",
            "description": "Scope 2 value visible on this page, or null if not present.",
        },
        "scope_3_value": {
            "type": "number",
            "description": "Scope 3 value visible on this page, or null if not present.",
        },
    },
    "required": ["contains_values"],
    "additionalProperties": False,
}


def verify_page_contains_values(
    pdf_path: str,
    page_index: int,
    year: int,
    scope_1=None,
    scope_2=None,
    scope_3=None,
    unit: str = "tonnes CO2e",
    client: anthropic.Anthropic = None,
) -> bool:
    """Send a single PDF page to Claude and ask if it contains the given values.

    Renders the page as a PNG image so Claude sees it in isolation with no
    page-tracking ambiguity. Returns True if Claude confirms the values.
    """
    import base64
    import pymupdf

    doc = pymupdf.open(pdf_path)
    pix = doc[page_index].get_pixmap(dpi=100)
    img_bytes = pix.tobytes("png")
    doc.close()

    values_desc = []
    if scope_1 is not None:
        values_desc.append(f"Scope 1 = {scope_1}")
    if scope_2 is not None:
        values_desc.append(f"Scope 2 = {scope_2}")
    if scope_3 is not None:
        values_desc.append(f"Scope 3 = {scope_3}")

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=(
            "You are checking whether a single page from a sustainability report "
            "contains specific greenhouse gas emissions values. Look at the page "
            "and answer whether the stated values appear on it. The values may be "
            "in a table or in running text. They may appear in different units "
            f"(e.g. Mt CO2e vs tonnes CO2e) — check for equivalent values."
        ),
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            f"Does this page contain the following GHG emissions "
                            f"values for reporting year {year}?\n"
                            + "\n".join(f"  - {v}" for v in values_desc)
                            + f"\nUnit context: {unit}"
                        ),
                    },
                ],
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": _VERIFY_PAGE_SCHEMA,
            }
        },
    )

    text_out = next((b.text for b in response.content if b.type == "text"), None)
    if text_out is None:
        return False
    result = json.loads(text_out)
    return result.get("contains_values", False)
