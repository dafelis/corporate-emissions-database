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
) -> dict:
    """Extract Scope 1/2/3 emissions from a table using Claude Opus.

    Returns structured emissions data matching EMISSIONS_SCHEMA.
    """
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=(
            "You are an expert at extracting greenhouse gas emissions data from tables "
            "in sustainability reports. Extract Scope 1, Scope 2 (both location-based and "
            "market-based if available), and Scope 3 emissions for ALL years present in the table. "
            "Normalise all values to the same unit (prefer tonnes CO2e). "
            "If the table uses kt or Mt, convert to tonnes. "
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
) -> dict:
    """Extract emissions data from free-form text (fallback when no tables found)."""
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=8192,
        system=(
            "You are an expert at extracting greenhouse gas emissions data from documents. "
            "Extract Scope 1, Scope 2 (both location-based and market-based if available), "
            "and Scope 3 emissions for ALL years mentioned. "
            "Normalise all values to tonnes CO2e. "
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
