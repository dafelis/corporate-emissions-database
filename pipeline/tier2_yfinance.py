"""Tier 2: Extract PCAF EVIC financial inputs from yfinance.

yfinance provides up to 4 years of annual balance sheet and income
statement data as structured DataFrames. Values are in raw units
(not thousands/millions) in the company's reporting currency.
"""

import logging
import math
from datetime import date

import yfinance as yf

log = logging.getLogger(__name__)

# Mapping from yfinance row labels to our PCAF fields
_BALANCE_SHEET_FIELDS = {
    "gross_debt": "TotalDebt",
    "lease_liabilities": "CapitalLeaseObligations",
    "non_controlling_interests": "MinorityInterest",
    "preference_shares": "PreferredStock",
    "shares_outstanding": "OrdinarySharesNumber",
    "cash_and_equivalents": "CashAndCashEquivalents",
}

_INCOME_FIELDS = {
    "revenue": "TotalRevenue",
}

# Fallback labels if primary is missing
_DEBT_FALLBACKS = ["LongTermDebtAndCapitalLeaseObligation", "LongTermDebt"]
_SHARES_FALLBACKS = ["ShareIssued"]


def _safe_float(val):
    """Convert a pandas/numpy value to float, returning None for NaN/None."""
    if val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None


def _get_val(df, label, col):
    """Safely get a value from a DataFrame by row label and column."""
    if df is None or df.empty:
        return None
    if label in df.index:
        return _safe_float(df.at[label, col])
    # yfinance sometimes uses "Title Case With Spaces" instead of CamelCase
    spaced = label[0]
    for c in label[1:]:
        if c.isupper():
            spaced += " " + c
        else:
            spaced += c
    if spaced in df.index:
        return _safe_float(df.at[spaced, col])
    return None


def extract_financials_from_yfinance(ticker: str, company_name: str) -> list[dict]:
    """Fetch structured financial data from yfinance.

    Returns a list of dicts, one per reporting year, matching the PCAF
    extraction schema (flat values, not nested objects). Values are in
    raw units in the company's financial reporting currency.

    Returns an empty list if no data is available.
    """
    try:
        stock = yf.Ticker(ticker)
        bs = stock.balance_sheet
        inc = stock.financials
        info = stock.info
    except Exception as e:
        log.warning(f"  Tier 2: yfinance fetch failed for {ticker}: {e}")
        return []

    if bs is None or bs.empty:
        log.info(f"  Tier 2: no balance sheet data for {ticker}")
        return []

    log.info(f"  Tier 2: yfinance balance sheet has {len(bs.index)} rows, "
             f"{len(bs.columns)} periods for {ticker}")

    currency = info.get("financialCurrency", "")
    results = []

    for col in bs.columns:
        # col is a datetime — extract the fiscal year-end date
        try:
            fy_date = col.date() if hasattr(col, "date") else col
            year = fy_date.year
        except Exception:
            continue

        # Gross debt
        gross_debt = _get_val(bs, _BALANCE_SHEET_FIELDS["gross_debt"], col)
        if gross_debt is None:
            for fb in _DEBT_FALLBACKS:
                gross_debt = _get_val(bs, fb, col)
                if gross_debt is not None:
                    break

        # Lease liabilities
        lease_liab = _get_val(bs, _BALANCE_SHEET_FIELDS["lease_liabilities"], col)

        # NCI
        nci = _get_val(bs, _BALANCE_SHEET_FIELDS["non_controlling_interests"], col)

        # Preference shares
        pref = _get_val(bs, _BALANCE_SHEET_FIELDS["preference_shares"], col)

        # Shares outstanding
        shares = _get_val(bs, _BALANCE_SHEET_FIELDS["shares_outstanding"], col)
        if shares is None:
            for fb in _SHARES_FALLBACKS:
                shares = _get_val(bs, fb, col)
                if shares is not None:
                    break

        # Cash
        cash = _get_val(bs, _BALANCE_SHEET_FIELDS["cash_and_equivalents"], col)

        # Revenue (from income statement, matched by column date)
        revenue = None
        if inc is not None and not inc.empty and col in inc.columns:
            revenue = _get_val(inc, _INCOME_FIELDS["revenue"], col)

        # Skip if we got nothing useful
        if all(v is None for v in [gross_debt, revenue, shares]):
            continue

        def _yfprov(label, val):
            if val is None:
                return None
            return {
                "concept": f"yfinance:{label}",
                "value": val,
                "unit": f"iso4217:{currency}" if currency else "",
                "period": fy_date.isoformat(),
                "calculated": False,
            }

        provenance = {}
        for field, label, val in [
            ("revenue", "TotalRevenue", revenue),
            ("gross_debt", "TotalDebt", gross_debt),
            ("lease_liabilities", "CapitalLeaseObligations", lease_liab),
            ("non_controlling_interests", "MinorityInterest", nci),
            ("preference_shares", "PreferredStock", pref),
            ("shares_outstanding", "OrdinarySharesNumber", shares),
        ]:
            prov = _yfprov(label, val)
            if prov:
                provenance[field] = prov

        entry = {
            "reporting_year": year,
            "reporting_date": fy_date.isoformat(),
            "currency": currency,
            "unit_multiplier": 1,
            "provenance": provenance,
            "gross_debt": {"value": gross_debt, "components": [], "ref": "yfinance:TotalDebt", "confidence": "medium"},
            "lease_liabilities": {"value": lease_liab, "label": "CapitalLeaseObligations", "ref": "yfinance:CapitalLeaseObligations", "confidence": "medium"},
            "non_controlling_interests": {"value": nci, "label": "MinorityInterest", "ref": "yfinance:MinorityInterest", "confidence": "medium"},
            "preference_shares": {"value": pref, "classification": "unknown", "listed": None, "ref": "yfinance:PreferredStock"},
            "shares_outstanding": {"value": shares, "share_class": "", "ref": "yfinance:OrdinarySharesNumber", "confidence": "medium"},
            "revenue": {"value": revenue, "label": "TotalRevenue", "ref": "yfinance:TotalRevenue", "confidence": "medium"},
            "is_financial_institution": False,
            "notes": ["Tier 2: extracted from yfinance structured data"],
        }

        # Also include legacy fields for backwards compat
        entry["cash_and_equivalents"] = cash

        results.append(entry)
        parts = [f"Tier 2: {year}"]
        if revenue is not None:
            parts.append(f"revenue={revenue:,.0f}")
        if gross_debt is not None:
            parts.append(f"debt={gross_debt:,.0f}")
        if shares is not None:
            parts.append(f"shares={shares:,.0f}")
        log.info(f"    {' — '.join(parts)}")

    if results:
        log.info(f"  Tier 2: yfinance returned {len(results)} year(s) "
                 f"for {company_name} ({currency})")
    else:
        log.info(f"  Tier 2: no usable financial data from yfinance for {ticker}")

    return results
