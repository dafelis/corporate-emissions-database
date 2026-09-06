"""Classify companies into NAICS, NACE, and SIC codes using Claude."""

import json
import logging

import anthropic

log = logging.getLogger(__name__)


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "sic_code": {
            "type": "string",
            "description": "US SIC code (4 digits), e.g. '6020'",
        },
        "sic_description": {
            "type": "string",
            "description": "SIC code description, e.g. 'State commercial banks-Federal Reserve members and state'",
        },
        "naics_code": {
            "type": "string",
            "description": "NAICS code (5-6 digits), e.g. '522110'",
        },
        "naics_description": {
            "type": "string",
            "description": "NAICS code description, e.g. 'Commercial Banking'",
        },
        "nace_code": {
            "type": "string",
            "description": "NACE Rev. 2 code (e.g. '64.19')",
        },
        "nace_description": {
            "type": "string",
            "description": "NACE code description, e.g. 'Other monetary intermediation'",
        },
        "confidence": {
            "type": "string",
            "description": "How confident you are in the mapping: 'high', 'medium', or 'low'",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of the classification choice",
        },
    },
    "required": [
        "sic_code", "sic_description",
        "naics_code", "naics_description",
        "nace_code", "nace_description",
        "confidence", "reasoning",
    ],
    "additionalProperties": False,
}


def classify_company(
    company_name: str,
    yfinance_sector: str,
    yfinance_industry: str,
    client: anthropic.Anthropic,
) -> dict:
    """Map a company to NAICS, NACE, and SIC codes using Claude.

    Uses the company name and yfinance sector/industry as inputs.

    Returns dict matching CLASSIFICATION_SCHEMA.
    """
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=(
            "You are an expert in industry classification systems. "
            "Given a company name and its sector/industry from Yahoo Finance, "
            "map it to the most appropriate codes in three classification systems:\n"
            "1. US SIC (Standard Industrial Classification) — 4-digit code\n"
            "2. NAICS (North American Industry Classification System) — 5-6 digit code\n"
            "3. NACE Rev. 2 (EU statistical classification) — code like '64.19'\n\n"
            "Use the primary business activity of the company. "
            "If the company is a conglomerate, use the dominant revenue segment. "
            "Be precise with the codes — use real, valid codes from each system."
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Classify this company:\n"
                    f"Name: {company_name}\n"
                    f"Yahoo Finance sector: {yfinance_sector or 'unknown'}\n"
                    f"Yahoo Finance industry: {yfinance_industry or 'unknown'}"
                ),
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": CLASSIFICATION_SCHEMA,
            }
        },
    )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text)
