"""
Streamlit interface for the corporate emissions database.

Views:
  - Data Table: all companies × years pivot with colour-coded approval status
  - Review: single-record review with approve/reject/flag/edit actions

Run with: streamlit run review/app.py
"""

import os
import sys

# Ensure the project root is on the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import func

load_dotenv()

from db.models import (
    Company, EmissionsRecord, FinancialRecord, Source,
    PipelineRun, ReviewHistory, get_session,
)

st.set_page_config(page_title="Corporate Emissions Database", layout="wide")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    st.error("DATABASE_URL not set in .env")
    st.stop()

session = get_session(DATABASE_URL)

# ════════════════════════════════════════════════════════════════════════
# Sidebar — shared across views
# ════════════════════════════════════════════════════════════════════════

st.sidebar.title("Corporate Emissions DB")

view = st.sidebar.radio("View", ["Data Table", "Review"], index=0)

st.sidebar.markdown("---")
st.sidebar.header("Stats")
for label, status in [("Pending", "pending"), ("Flagged", "flagged"),
                       ("Approved", "approved"), ("Rejected", "rejected")]:
    count = session.query(EmissionsRecord).filter_by(review_status=status).count()
    st.sidebar.metric(label, count)
st.sidebar.metric("Total records", session.query(EmissionsRecord).count())


# ════════════════════════════════════════════════════════════════════════
# Helper: log review action (used by Review view)
# ════════════════════════════════════════════════════════════════════════

def log_review(record_obj, record_type, old_status, new_status, notes="", field_changes=None):
    """Write a row to review_history and update the record."""
    record_obj.review_status = new_status
    record_obj.reviewed_by = "reviewer"
    record_obj.reviewed_at = datetime.utcnow()

    history = ReviewHistory(
        record_type=record_type,
        record_id=record_obj.id,
        old_status=old_status,
        new_status=new_status,
        changed_by="reviewer",
        changed_at=datetime.utcnow(),
        notes=notes or None,
        field_changes=json.dumps(field_changes) if field_changes else None,
    )
    session.add(history)
    session.commit()


# ════════════════════════════════════════════════════════════════════════
# DATA TABLE VIEW
# ════════════════════════════════════════════════════════════════════════

def render_data_table():
    st.title("Data Table")

    # ── Load all data ──────────────────────────────────────────────────
    companies = session.query(Company).order_by(Company.name).all()
    emissions = session.query(EmissionsRecord).all()
    financials = session.query(FinancialRecord).all()

    if not companies:
        st.info("No companies in the database yet.")
        return

    # Index emissions and financials by (company_id, year)
    em_by_key = {}
    for e in emissions:
        em_by_key[(e.company_id, e.reporting_year)] = e

    fin_by_key = {}
    for f in financials:
        fin_by_key[(f.company_id, f.reporting_year)] = f

    # Collect all years across both datasets
    all_years = sorted({e.reporting_year for e in emissions} | {f.reporting_year for f in financials})

    if not all_years:
        st.info("No data extracted yet.")
        return

    # ── Filter controls ────────────────────────────────────────────────
    st.sidebar.markdown("---")
    st.sidebar.header("Data Table Filters")
    show_emissions = st.sidebar.checkbox("Emissions", value=True)
    show_financials = st.sidebar.checkbox("Financials", value=True)

    # ── Build column definitions ───────────────────────────────────────
    em_fields = [
        ("S1", "scope_1", "emissions"),
        ("S2 loc", "scope_2_location", "emissions"),
        ("S2 mkt", "scope_2_market", "emissions"),
        ("S3", "scope_3", "emissions"),
    ]
    fin_fields = [
        ("Revenue", "revenue", "financial"),
        ("Debt", "outstanding_debt", "financial"),
        ("Cash", "cash_and_equivalents", "financial"),
        ("Equity", "equity_value", "financial"),
        ("EV", "enterprise_value", "financial"),
    ]

    fields = []
    if show_emissions:
        fields += em_fields
    if show_financials:
        fields += fin_fields

    if not fields:
        st.info("Select at least one data category in the sidebar.")
        return

    # ── Year basis helper ─────────────────────────────────────────────
    def record_basis(rec):
        """Return e.g. 'CY 2025', 'FY 2026', 'Other' from a record's period_end."""
        if not rec or not rec.period_end:
            return "—"
        m, d, y = rec.period_end.month, rec.period_end.day, rec.period_end.year
        if m == 12 and d == 31:
            return f"CY {y}"
        if m == 3 and d == 31:
            return f"FY {y}"
        return "Other"

    # ── Format helpers ─────────────────────────────────────────────────
    def fmt_num(value):
        if value is None:
            return "—"
        if abs(value) >= 1_000_000_000:
            return f"{value / 1_000_000_000:,.1f}bn"
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:,.1f}m"
        if abs(value) >= 1_000:
            return f"{value / 1_000:,.1f}k"
        if value != int(value):
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{int(value):,}"

    # ── Build HTML table ───────────────────────────────────────────────
    html_parts = []
    html_parts.append("""
    <style>
    .data-table-wrap { overflow-x: auto; }
    .data-table {
        border-collapse: collapse;
        font-size: 13px;
        white-space: nowrap;
    }
    .data-table th {
        background: #f0f2f6;
        border: 1px solid #ddd;
        padding: 4px 8px;
        text-align: center;
        position: sticky;
        top: 0;
        z-index: 1;
    }
    .data-table td {
        border: 1px solid #eee;
        padding: 3px 8px;
        text-align: right;
    }
    .data-table td.company-name {
        text-align: left;
        font-weight: 600;
        position: sticky;
        left: 0;
        background: white;
        z-index: 1;
    }
    .data-table tr:hover { background: #f8f9fb; }
    .data-table .approved { color: #111; }
    .data-table .not-approved { color: #aaa; }
    .data-table .no-data { color: #ddd; }
    .data-table .year-header { border-bottom: 2px solid #999; }
    @media (prefers-color-scheme: dark) {
        .data-table th { background: #262730; border-color: #444; color: #fafafa; }
        .data-table td { border-color: #333; }
        .data-table td.company-name { background: #0e1117; color: #fafafa; }
        .data-table tr:hover { background: #1a1c24; }
        .data-table .approved { color: #fafafa; }
        .data-table .not-approved { color: #666; }
        .data-table .no-data { color: #333; }
    }
    </style>
    <div class="data-table-wrap">
    <table class="data-table">
    """)

    # Count basis columns: 1 per visible category
    n_basis = (1 if show_emissions else 0) + (1 if show_financials else 0)

    # Header row 1: year spans
    html_parts.append("<thead><tr><th rowspan='2'>Company</th>")
    for year in all_years:
        html_parts.append(f"<th class='year-header' colspan='{len(fields) + n_basis}'>{year}</th>")
    html_parts.append("</tr>")

    # Header row 2: field names with basis columns at end
    html_parts.append("<tr>")
    for year in all_years:
        for label, _, _ in fields:
            html_parts.append(f"<th>{label}</th>")
        if show_emissions:
            html_parts.append("<th>Em. Basis</th>")
        if show_financials:
            html_parts.append("<th>Fin. Basis</th>")
    html_parts.append("</tr></thead>")

    # Data rows
    html_parts.append("<tbody>")
    for company in companies:
        # Skip companies with no data at all
        has_any = any(
            (company.id, y) in em_by_key or (company.id, y) in fin_by_key
            for y in all_years
        )
        if not has_any:
            continue

        html_parts.append(f"<tr><td class='company-name'>{company.name}</td>")

        for year in all_years:
            em = em_by_key.get((company.id, year))
            fin = fin_by_key.get((company.id, year))

            for label, field_name, source_type in fields:
                if source_type == "emissions":
                    value = getattr(em, field_name, None) if em else None
                    status = em.review_status if em else None
                else:
                    value = getattr(fin, field_name, None) if fin else None
                    status = fin.review_status if fin else None

                if value is not None:
                    css_class = "approved" if status == "approved" else "not-approved"
                    html_parts.append(f"<td class='{css_class}'>{fmt_num(value)}</td>")
                else:
                    html_parts.append("<td class='no-data'>—</td>")

            # Basis columns at end
            if show_emissions:
                em_basis = record_basis(em)
                em_basis_class = "not-approved" if em_basis == "—" else "approved"
                html_parts.append(f"<td class='{em_basis_class}' style='text-align:center'>{em_basis}</td>")
            if show_financials:
                fin_basis = record_basis(fin)
                fin_basis_class = "not-approved" if fin_basis == "—" else "approved"
                html_parts.append(f"<td class='{fin_basis_class}' style='text-align:center'>{fin_basis}</td>")

        html_parts.append("</tr>")

    html_parts.append("</tbody></table></div>")

    st.markdown("".join(html_parts), unsafe_allow_html=True)

    # ── Export as CSV ──────────────────────────────────────────────────
    st.markdown("---")

    # Build a flat DataFrame for export
    export_rows = []
    for company in companies:
        has_any = any(
            (company.id, y) in em_by_key or (company.id, y) in fin_by_key
            for y in all_years
        )
        if not has_any:
            continue

        for year in all_years:
            em = em_by_key.get((company.id, year))
            fin = fin_by_key.get((company.id, year))
            if not em and not fin:
                continue

            row = {
                "Company": company.name,
                "Year": year,
            }
            if em:
                row["Emissions Status"] = em.review_status
                row["Scope 1"] = em.scope_1
                row["Scope 2 (location)"] = em.scope_2_location
                row["Scope 2 (market)"] = em.scope_2_market
                row["Scope 3"] = em.scope_3
                if em.period_start and em.period_end:
                    row["Emissions Period"] = f"{em.period_start} to {em.period_end}"
            if fin:
                row["Financial Status"] = fin.review_status
                row["Revenue"] = fin.revenue
                row["Outstanding Debt"] = fin.outstanding_debt
                row["Cash & Equivalents"] = fin.cash_and_equivalents
                row["Equity Value"] = fin.equity_value
                row["Enterprise Value"] = fin.enterprise_value
                row["Currency"] = fin.currency
                if fin.period_start and fin.period_end:
                    row["Financial Period"] = f"{fin.period_start} to {fin.period_end}"

            export_rows.append(row)

    if export_rows:
        export_df = pd.DataFrame(export_rows)
        csv = export_df.to_csv(index=False)
        st.download_button("📥 Export full dataset (CSV)", csv, "emissions_database.csv", "text/csv")


# ════════════════════════════════════════════════════════════════════════
# REVIEW VIEW
# ════════════════════════════════════════════════════════════════════════

def render_review():
    st.title("Review Records")

    # Status filter
    st.sidebar.markdown("---")
    st.sidebar.header("Review Filters")
    status_filter = st.sidebar.radio(
        "Show records",
        ["Pending", "Flagged", "Approved", "Rejected", "All"],
        index=0,
    )
    status_map = {
        "Flagged": "flagged",
        "Pending": "pending",
        "Approved": "approved",
        "Rejected": "rejected",
    }

    # Build query
    query = session.query(EmissionsRecord).join(Company).join(Source, isouter=True)
    if status_filter != "All":
        query = query.filter(EmissionsRecord.review_status == status_map[status_filter])
    query = query.order_by(Company.name, EmissionsRecord.reporting_year)
    records = query.all()

    if not records:
        st.info(f"No {status_filter.lower()} records to review.")
        return

    st.write(f"Showing {len(records)} records")

    # Navigation
    if "review_index" not in st.session_state:
        st.session_state.review_index = 0

    idx = st.session_state.review_index
    idx = min(idx, len(records) - 1)
    record = records[idx]
    company = session.query(Company).get(record.company_id)
    source = session.query(Source).get(record.source_id) if record.source_id else None

    # Progress bar
    st.progress((idx + 1) / len(records))
    st.caption(f"Record {idx + 1} of {len(records)}")

    # ── Record display ─────────────────────────────────────────────────
    col1, col2 = st.columns([2, 1])

    with col1:
        st.subheader(f"{company.name} — {record.reporting_year}")

        # Show reporting period if available
        if record.period_start and record.period_end:
            st.caption(f"📅 Reporting period: {record.period_start.strftime('%d %b %Y')} – {record.period_end.strftime('%d %b %Y')}")
        elif record.period_end:
            st.caption(f"📅 Period ending: {record.period_end.strftime('%d %b %Y')}")

        if record.flag_reason:
            st.warning(f"Flag reason: {record.flag_reason}")

        # Emissions data
        data = {"Scope": [], "Value": [], "Unit": []}
        for label, value in [
            ("Scope 1", record.scope_1),
            ("Scope 2 (location)", record.scope_2_location),
            ("Scope 2 (market)", record.scope_2_market),
            ("Scope 3", record.scope_3),
        ]:
            data["Scope"].append(label)
            if value is not None:
                data["Value"].append(
                    f"{value:,.2f}".rstrip("0").rstrip(".")
                    if value != int(value) else f"{int(value):,}"
                )
            else:
                data["Value"].append("—")
            data["Unit"].append(record.unit or "—")
        st.table(pd.DataFrame(data))

        if record.scope_3_categories:
            st.caption(f"Scope 3 categories: {record.scope_3_categories}")
        if record.methodology_notes:
            st.caption(f"Methodology: {record.methodology_notes}")
        if record.boundary:
            st.caption(f"Boundary: {record.boundary}")

        # Prior years for context
        prior = (
            session.query(EmissionsRecord)
            .filter_by(company_id=company.id)
            .filter(EmissionsRecord.reporting_year < record.reporting_year)
            .order_by(EmissionsRecord.reporting_year.desc())
            .limit(3)
            .all()
        )
        if prior:
            st.markdown("**Prior years:**")
            prior_data = []
            for p in reversed(prior):
                def _fmt(v):
                    if v is None:
                        return "—"
                    return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
                prior_data.append({
                    "Year": p.reporting_year,
                    "Scope 1": _fmt(p.scope_1),
                    "Scope 2": _fmt(p.scope_2_location),
                    "Scope 3": _fmt(p.scope_3),
                })
            st.table(pd.DataFrame(prior_data))

    with col2:
        st.markdown("**Source**")
        if source:
            st.write(f"📄 {source.title or 'Untitled'}")
            st.write(f"Type: {source.document_type}")
            if source.url:
                st.markdown(f"[Open URL]({source.url})")
            if source.page_number is not None:
                st.caption(f"Page: {source.page_number + 1}")
        else:
            st.write("No source linked")

        # Source preview
        if source and source.screenshot_path and os.path.exists(source.screenshot_path):
            st.markdown("**Source table (PDF screenshot):**")
            st.image(source.screenshot_path, use_container_width=True)
        elif source and source.html_snippet:
            st.markdown("**Source table:**")
            st.markdown(source.html_snippet, unsafe_allow_html=True)

        # Confidence
        score = record.confidence_score
        if score is not None:
            if score >= 70:
                color = "🟢"
            elif score >= 40:
                color = "🟡"
            else:
                color = "🔴"
            st.metric("Confidence", f"{color} {score}/100")

        # LEI
        st.markdown("**LEI**")
        if company.lei:
            lei_conf = company.lei_confidence or "unknown"
            lei_icon = "🟢" if lei_conf == "high" else ("🟡" if lei_conf == "medium" else "🔴")
            st.write(f"{lei_icon} {company.lei}")
            st.caption(f"GLEIF name: {company.lei_legal_name or '—'}")
            st.caption(f"Country: {company.lei_country or '—'}")
            st.caption(f"Status: {company.lei_review_status or 'pending'}")
            if company.lei_flag_reason:
                st.warning(company.lei_flag_reason)
        else:
            st.write("Not found")

        # Industry classification
        if company.yfinance_sector:
            st.markdown("**Industry**")
            st.write(f"{company.yfinance_sector} / {company.yfinance_industry or '—'}")
            if company.sic_code:
                st.caption(f"SIC: {company.sic_code} — {company.sic_description or ''}")
            if company.naics_code:
                st.caption(f"NAICS: {company.naics_code} — {company.naics_description or ''}")
            if company.nace_code:
                st.caption(f"NACE: {company.nace_code} — {company.nace_description or ''}")

    # ── Financial data ─────────────────────────────────────────────────
    fin_record = (
        session.query(FinancialRecord)
        .filter_by(company_id=company.id, reporting_year=record.reporting_year)
        .first()
    )

    if fin_record:
        st.markdown("---")
        st.subheader("Financial Data")
        if fin_record.period_start and fin_record.period_end:
            st.caption(f"📅 Financial period: {fin_record.period_start.strftime('%d %b %Y')} – {fin_record.period_end.strftime('%d %b %Y')}")
        elif fin_record.fiscal_year_end:
            st.caption(f"📅 Fiscal year ending: {fin_record.fiscal_year_end.strftime('%d %b %Y')}")

        def _fmt_currency(value, currency=""):
            if value is None:
                return "—"
            prefix = f"{currency} " if currency else ""
            if abs(value) >= 1_000_000_000:
                return f"{prefix}{value / 1_000_000_000:,.2f}bn"
            if abs(value) >= 1_000_000:
                return f"{prefix}{value / 1_000_000:,.1f}m"
            return f"{prefix}{value:,.0f}"

        fin_col1, fin_col2 = st.columns(2)
        with fin_col1:
            st.markdown("**From company report:**")
            st.table(pd.DataFrame({
                "Metric": ["Revenue", "Outstanding debt", "Cash & equivalents"],
                "Value": [
                    _fmt_currency(fin_record.revenue, fin_record.currency),
                    _fmt_currency(fin_record.outstanding_debt, fin_record.currency),
                    _fmt_currency(fin_record.cash_and_equivalents, fin_record.currency),
                ],
            }))
        with fin_col2:
            st.markdown("**Market data:**")
            st.table(pd.DataFrame({
                "Metric": ["Equity value (market cap)", "Enterprise value"],
                "Value": [
                    _fmt_currency(fin_record.equity_value, fin_record.equity_currency),
                    _fmt_currency(fin_record.enterprise_value, fin_record.equity_currency),
                ],
            }))
            if fin_record.fiscal_year_end:
                st.caption(f"As at fiscal year-end: {fin_record.fiscal_year_end}")

    # ── Review actions ─────────────────────────────────────────────────
    st.markdown("---")
    action_cols = st.columns(5)

    with action_cols[0]:
        if st.button("✓ Approve", type="primary", use_container_width=True):
            log_review(record, "emissions", record.review_status, "approved")
            st.session_state.review_index = min(idx + 1, len(records) - 1)
            st.rerun()

    with action_cols[1]:
        if st.button("✗ Reject", use_container_width=True):
            log_review(record, "emissions", record.review_status, "rejected")
            st.session_state.review_index = min(idx + 1, len(records) - 1)
            st.rerun()

    with action_cols[2]:
        if st.button("⚑ Flag", use_container_width=True):
            log_review(record, "emissions", record.review_status, "flagged")
            st.session_state.review_index = min(idx + 1, len(records) - 1)
            st.rerun()

    with action_cols[3]:
        if st.button("← Prev", use_container_width=True):
            st.session_state.review_index = max(0, idx - 1)
            st.rerun()

    with action_cols[4]:
        if st.button("Next →", use_container_width=True):
            st.session_state.review_index = min(idx + 1, len(records) - 1)
            st.rerun()

    # ── Edit values ────────────────────────────────────────────────────
    with st.expander("Edit values"):
        edit_cols = st.columns(4)
        with edit_cols[0]:
            new_s1 = st.number_input("Scope 1", value=record.scope_1 or 0.0, format="%.0f")
        with edit_cols[1]:
            new_s2l = st.number_input("Scope 2 (loc)", value=record.scope_2_location or 0.0, format="%.0f")
        with edit_cols[2]:
            new_s2m = st.number_input("Scope 2 (mkt)", value=record.scope_2_market or 0.0, format="%.0f")
        with edit_cols[3]:
            new_s3 = st.number_input("Scope 3", value=record.scope_3 or 0.0, format="%.0f")

        notes = st.text_input("Review notes", value="")

        if st.button("Save edits"):
            changes = {}
            for field, new_val in [("scope_1", new_s1), ("scope_2_location", new_s2l),
                                    ("scope_2_market", new_s2m), ("scope_3", new_s3)]:
                old_val = getattr(record, field)
                new_clean = new_val if new_val > 0 else None
                if old_val != new_clean:
                    changes[field] = {"old": old_val, "new": new_clean}

            record.scope_1 = new_s1 if new_s1 > 0 else None
            record.scope_2_location = new_s2l if new_s2l > 0 else None
            record.scope_2_market = new_s2m if new_s2m > 0 else None
            record.scope_3 = new_s3 if new_s3 > 0 else None

            log_review(record, "emissions", record.review_status, record.review_status,
                       notes=notes, field_changes=changes if changes else None)
            st.success("Values updated")
            st.rerun()

    # ── Review history ─────────────────────────────────────────────────
    hist = (
        session.query(ReviewHistory)
        .filter_by(record_type="emissions", record_id=record.id)
        .order_by(ReviewHistory.changed_at.desc())
        .all()
    )
    if hist:
        with st.expander(f"Review history ({len(hist)} entries)"):
            for h in hist:
                ts = h.changed_at.strftime("%d %b %Y %H:%M") if h.changed_at else "—"
                line = f"**{ts}** — {h.changed_by}: {h.old_status} → {h.new_status}"
                if h.notes:
                    line += f"  \n_{h.notes}_"
                if h.field_changes:
                    try:
                        fc = json.loads(h.field_changes)
                        edits = ", ".join(f"{k}: {v['old']}→{v['new']}" for k, v in fc.items())
                        line += f"  \nEdits: {edits}"
                    except (json.JSONDecodeError, KeyError):
                        pass
                st.markdown(line)

    # ── Overview table ─────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("All records overview")

    overview_query = (
        session.query(
            Company.name,
            EmissionsRecord.reporting_year,
            EmissionsRecord.scope_1,
            EmissionsRecord.scope_2_location,
            EmissionsRecord.scope_3,
            EmissionsRecord.confidence_score,
            EmissionsRecord.review_status,
        )
        .join(Company)
        .order_by(Company.name, EmissionsRecord.reporting_year)
    )

    if status_filter != "All":
        overview_query = overview_query.filter(
            EmissionsRecord.review_status == status_map[status_filter]
        )

    rows = overview_query.all()
    if rows:
        df = pd.DataFrame(rows, columns=[
            "Company", "Year", "Scope 1", "Scope 2 (loc)", "Scope 3",
            "Confidence", "Status"
        ])
        st.dataframe(df, use_container_width=True, hide_index=True)
        csv = df.to_csv(index=False)
        st.download_button("Download CSV", csv, "emissions_data.csv", "text/csv")


# ════════════════════════════════════════════════════════════════════════
# Route to selected view
# ════════════════════════════════════════════════════════════════════════

if view == "Data Table":
    render_data_table()
else:
    render_review()
