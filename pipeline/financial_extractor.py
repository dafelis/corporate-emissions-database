"""Extract financial data (revenue, debt, cash) from company reports using Claude."""

import json

import anthropic


FINANCIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "financials": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reporting_year": {"type": "integer"},
                    "fiscal_year_end": {
                        "type": "string",
                        "description": "Fiscal year end date in YYYY-MM-DD format, if stated",
                    },
                    "revenue": {
                        "type": "number",
                        "description": "Total revenue / turnover / net sales, or null if not found",
                    },
                    "outstanding_debt": {
                        "type": "number",
                        "description": "Total borrowings / total debt / gross debt, or null if not found. Include both current and non-current portions.",
                    },
                    "cash_and_equivalents": {
                        "type": "number",
                        "description": "Cash and cash equivalents (and short-term investments if grouped), or null if not found",
                    },
                    "currency": {
                        "type": "string",
                        "description": "Currency code the figures are reported in, e.g. 'GBP', 'USD', 'EUR'",
                    },
                    "unit_multiplier": {
                        "type": "integer",
                        "description": "What the numbers are expressed in: 1 for units, 1000 for thousands, 1000000 for millions, 1000000000 for billions. Check the table header or notes.",
                    },
                },
                "required": ["reporting_year"],
                "additionalProperties": False,
            },
            "description": "One entry per reporting year found in the data.",
        },
        "methodology_notes": {
            "type": "string",
            "description": "Any notes about the financial data: accounting standard (IFRS/GAAP), restatements, exceptional items. Empty string if none.",
        },
        "confidence_score": {
            "type": "integer",
            "description": "How confident you are that the extracted values are correct, 0-100.",
        },
    },
    "required": ["financials", "methodology_notes", "confidence_score"],
    "additionalProperties": False,
}


def find_financial_tables(
    tables: list[str],
    client: anthropic.Anthropic,
) -> list[dict]:
    """Rank tables by likelihood of containing financial summary data.

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
            "You are an expert at identifying financial data in company reports. "
            "Given table previews, rank ALL tables from most to least likely to contain "
            "revenue, total debt, and cash & equivalents. Look for income statements, "
            "balance sheets, financial highlights, and key financial summaries. "
            "Assign each a relevance score 0-100. Every table must appear in the ranking."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    "Find tables containing financial summary data "
                    "(revenue/turnover, total debt/borrowings, cash & equivalents).\n\n"
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


def extract_financials(
    table: str,
    company_name: str,
    client: anthropic.Anthropic,
) -> dict:
    """Extract revenue, debt, and cash from a table using Claude Opus.

    Returns structured financial data matching FINANCIAL_SCHEMA.
    """
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=(
            "You are an expert at extracting financial data from company annual reports. "
            "Extract revenue (turnover/net sales), total outstanding debt (borrowings, "
            "both current and non-current), and cash & cash equivalents for ALL years "
            "present in the table. "
            "IMPORTANT: Check what unit the table uses (thousands, millions, billions) — "
            "this is usually stated in the table header or a note. Set unit_multiplier "
            "accordingly and report the raw numbers as shown in the table. "
            "Identify the currency from the table (GBP, USD, EUR, etc). "
            "Note the fiscal year end date if visible (e.g. 'Year ended 31 December 2024'). "
            "If a value is not present in the table, set it to null."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract financial data for {company_name} from this table:\n\n{table}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": FINANCIAL_SCHEMA,
            }
        },
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text)


def normalise_to_units(value, multiplier):
    """Convert a value from thousands/millions/billions to units."""
    if value is None or multiplier is None:
        return value
    return value * multiplier
