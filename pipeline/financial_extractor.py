"""Extract financial data (PCAF EVIC inputs) from company reports using Claude."""

import json

import anthropic


# ── Provenance sub-schema (reused per field) ─────────────────────────────

_FIELD_WITH_REF = {
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "description": "Extracted numeric value, or null if not found"},
        "label": {"type": "string", "description": "Label as printed in the source document"},
        "ref": {"type": "string", "description": "Page number where the value was found, e.g. 'p.42'"},
        "confidence": {"type": "string", "description": "high, medium, or low"},
    },
    "required": ["value", "label", "ref", "confidence"],
    "additionalProperties": False,
}

_DEBT_FIELD = {
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "description": "Gross debt total, or null if not found"},
        "components": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Component line items summed to get gross debt",
        },
        "ref": {"type": "string", "description": "Page number(s)"},
        "confidence": {"type": "string", "description": "high, medium, or low"},
    },
    "required": ["value", "components", "ref", "confidence"],
    "additionalProperties": False,
}

_PREF_SHARES_FIELD = {
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "description": "Preference share value, or null if not found"},
        "classification": {"type": "string", "description": "equity or liability"},
        "listed": {"type": ["boolean", "null"], "description": "Are the preference shares listed? null if unknown"},
        "ref": {"type": "string"},
    },
    "required": ["value", "classification", "listed", "ref"],
    "additionalProperties": False,
}

_SHARES_FIELD = {
    "type": "object",
    "properties": {
        "value": {"type": ["number", "null"], "description": "Shares outstanding net of treasury, or null"},
        "share_class": {"type": "string", "description": "Share class if multiple, or empty string"},
        "ref": {"type": "string"},
        "confidence": {"type": "string"},
    },
    "required": ["value", "share_class", "ref", "confidence"],
    "additionalProperties": False,
}


# ── Main extraction schema ───────────────────────────────────────────────

FINANCIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "financials": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reporting_year": {"type": "integer"},
                    "reporting_date": {
                        "type": "string",
                        "description": "Balance sheet date in YYYY-MM-DD format",
                    },
                    "period_start": {"type": "string", "description": "Start of reporting period YYYY-MM-DD"},
                    "period_end": {"type": "string", "description": "End of reporting period YYYY-MM-DD"},
                    "currency": {"type": "string", "description": "ISO currency code"},
                    "unit_multiplier": {
                        "type": "integer",
                        "description": "1=units, 1000=thousands, 1000000=millions, 1000000000=billions",
                    },
                    "gross_debt": _DEBT_FIELD,
                    "lease_liabilities": _FIELD_WITH_REF,
                    "non_controlling_interests": _FIELD_WITH_REF,
                    "preference_shares": _PREF_SHARES_FIELD,
                    "shares_outstanding": _SHARES_FIELD,
                    "revenue": _FIELD_WITH_REF,
                    "is_financial_institution": {
                        "type": "boolean",
                        "description": "True if bank, insurer, or asset manager",
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Judgement calls, restatements, policy changes",
                    },
                },
                "required": [
                    "reporting_year", "reporting_date", "currency",
                    "unit_multiplier", "gross_debt", "revenue",
                    "is_financial_institution", "notes",
                ],
                "additionalProperties": False,
            },
            "description": "One entry per reporting year found in the document.",
        },
        "methodology_notes": {
            "type": "string",
            "description": "Accounting standard (IFRS/GAAP), restatements, exceptional items. Empty string if none.",
        },
        "confidence_score": {
            "type": "integer",
            "description": "Overall confidence 0-100.",
        },
    },
    "required": ["financials", "methodology_notes", "confidence_score"],
    "additionalProperties": False,
}


# ── Extraction prompt (Tier 3) ───────────────────────────────────────────

PCAF_EXTRACTION_PROMPT = (
    "You are extracting financial data from a listed company's annual report for "
    "use in a PCAF financed-emissions calculation. Extract only from the document "
    "provided. Do not estimate, infer from prior years, or use outside knowledge. "
    "If a value cannot be found, return null and say why in notes.\n\n"
    "Report on the CONSOLIDATED (group) financial statements, not parent-only. "
    "Extract ALL years present in the document, including prior-year comparatives.\n\n"
    "Companies use varying terminology; match on substance, not the exact label.\n\n"
    "For EVERY year found, extract:\n\n"
    "1. reporting_date: balance sheet date (YYYY-MM-DD).\n"
    "2. currency and unit_multiplier: ISO currency code; 1=units, 1000=thousands, "
    "1000000=millions. Check the table header or page note.\n"
    "3. gross_debt: sum of all interest-bearing borrowings, current and non-current. "
    "Include bank loans, overdrafts, bonds, notes, debentures, commercial paper, "
    "related-party loans, liability component of convertibles, and lease liabilities "
    "(IFRS 16 / ASC 842). EXCLUDE trade payables, provisions, deferred tax, contract "
    "liabilities, derivative liabilities, pension deficits, customer deposits. "
    "Carrying/book value. If only net debt is reported, reverse out cash and note this. "
    "List the component lines summed.\n"
    "4. lease_liabilities: current + non-current, separately identified.\n"
    "5. non_controlling_interests: book value from equity section (may be labelled "
    "'minority interests'). Report as shown, even if negative.\n"
    "6. preference_shares: value, equity or liability classification, listed?\n"
    "7. shares_outstanding: ordinary shares in issue at the reporting date, net of "
    "treasury shares. State share class if multiple.\n"
    "8. revenue: total consolidated revenue/turnover/sales for the year, before "
    "deductions, excluding other income and finance income. Record the label as printed.\n"
    "9. is_financial_institution: true if bank, insurer, or asset manager (customer "
    "deposits, insurance contract liabilities, or net interest income as main revenue).\n\n"
    "For every numeric field give value, label as printed, page number (ref), and "
    "confidence (high/medium/low). Record judgement calls in notes.\n\n"
    "IMPORTANT: Check what unit the document uses (thousands, millions, billions) — "
    "this is usually stated in the header. Set unit_multiplier accordingly and report "
    "the raw numbers as shown.\n\n"
    "IMPORTANT: Identify the reporting period. Look for phrases like 'year ended "
    "31 December', 'for the 12 months to 31 March', 'FY2025' etc. Set period_start "
    "and period_end as YYYY-MM-DD dates."
)


def find_financial_tables(
    tables: list[str],
    client: anthropic.Anthropic,
) -> list[dict]:
    """Rank tables by likelihood of containing PCAF-relevant financial data.

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
        system=[{
            "type": "text",
            "text": (
                "You are an expert at identifying financial data in company reports. "
                "Given table previews, rank ALL tables from most to least likely to contain "
                "data needed for PCAF EVIC calculation: revenue, total borrowings/gross debt, "
                "lease liabilities, non-controlling interests, preference shares, shares "
                "outstanding. Look for consolidated income statements, balance sheets, "
                "borrowings notes, equity notes, and financial summaries. "
                "Assign each a relevance score 0-100. Every table must appear in the ranking."
            ),
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[
            {
                "role": "user",
                "content": (
                    "Find tables containing financial data needed for PCAF EVIC "
                    "(revenue, gross debt/borrowings, lease liabilities, NCI, "
                    "preference shares, shares outstanding).\n\n"
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
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    """Extract PCAF EVIC inputs from a table.

    Returns structured financial data matching FINANCIAL_SCHEMA.
    """
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": PCAF_EXTRACTION_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
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


def extract_financials_from_pdf(
    pdf_path: str,
    company_name: str,
    client: anthropic.Anthropic,
    model: str = "claude-haiku-4-5-20251001",
    num_pages: int = 0,
) -> dict:
    """Extract PCAF EVIC inputs by sending a filtered PDF to Claude."""
    import base64

    with open(pdf_path, "rb") as f:
        pdf_data = base64.standard_b64encode(f.read()).decode("utf-8")

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        system=[{
            "type": "text",
            "text": PCAF_EXTRACTION_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
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
                            f"Extract all PCAF EVIC financial data for {company_name} "
                            f"from this document ({num_pages} pages)."
                        ),
                    },
                ],
            }
        ],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": FINANCIAL_SCHEMA,
            }
        },
    )

    text_out = next((b.text for b in response.content if b.type == "text"), None)
    if text_out is None:
        raise ValueError("Claude returned no text response.")
    return json.loads(text_out)


def normalise_to_units(value, multiplier):
    """Convert a value from thousands/millions/billions to units."""
    if value is None or multiplier is None:
        return value
    return value * multiplier
