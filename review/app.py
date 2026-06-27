"""
Streamlit review interface for flagged emissions records.

Run with: streamlit run review/app.py
"""

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import func

load_dotenv()

from db.models import Company, EmissionsRecord, Source, PipelineRun, get_session

st.set_page_config(page_title="Emissions Data Review", layout="wide")
st.title("Emissions Data Review")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    st.error("DATABASE_URL not set in .env")
    st.stop()

session = get_session(DATABASE_URL)

# --- Sidebar: filters and stats ---
st.sidebar.header("Filters")

# Status filter
status_filter = st.sidebar.radio(
    "Show records",
    ["Flagged", "Pending", "Approved", "Rejected", "All"],
    index=0,
)

status_map = {
    "Flagged": "flagged",
    "Pending": "pending",
    "Approved": "approved",
    "Rejected": "rejected",
}

# Stats
st.sidebar.markdown("---")
st.sidebar.header("Stats")

for label, status in status_map.items():
    count = session.query(EmissionsRecord).filter_by(review_status=status).count()
    st.sidebar.metric(label, count)

total = session.query(EmissionsRecord).count()
st.sidebar.metric("Total records", total)

# --- Main content ---

# Build query
query = session.query(EmissionsRecord).join(Company).join(Source, isouter=True)

if status_filter != "All":
    query = query.filter(EmissionsRecord.review_status == status_map[status_filter])

query = query.order_by(Company.name, EmissionsRecord.reporting_year)
records = query.all()

if not records:
    st.info(f"No {status_filter.lower()} records to review.")
    st.stop()

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

# --- Record display ---
col1, col2 = st.columns([2, 1])

with col1:
    st.subheader(f"{company.name} — {record.reporting_year}")

    if record.flag_reason:
        st.warning(f"Flag reason: {record.flag_reason}")

    # Emissions data
    data = {
        "Scope": [],
        "Value": [],
        "Unit": [],
    }

    for label, value in [
        ("Scope 1", record.scope_1),
        ("Scope 2 (location)", record.scope_2_location),
        ("Scope 2 (market)", record.scope_2_market),
        ("Scope 3", record.scope_3),
    ]:
        data["Scope"].append(label)
        data["Value"].append(f"{value:,.0f}" if value is not None else "—")
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
            prior_data.append({
                "Year": p.reporting_year,
                "Scope 1": f"{p.scope_1:,.0f}" if p.scope_1 else "—",
                "Scope 2": f"{p.scope_2_location:,.0f}" if p.scope_2_location else "—",
                "Scope 3": f"{p.scope_3:,.0f}" if p.scope_3 else "—",
            })
        st.table(pd.DataFrame(prior_data))

with col2:
    st.markdown("**Source**")
    if source:
        st.write(f"📄 {source.title or 'Untitled'}")
        st.write(f"Type: {source.document_type}")
        if source.url:
            st.markdown(f"[Open URL]({source.url})")
        if source.s3_pdf_key:
            st.caption(f"S3: {source.s3_pdf_key}")
        if source.page_number:
            st.caption(f"Page: {source.page_number}")
    else:
        st.write("No source linked")

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

    st.markdown("**LEI**")
    st.write(company.lei or "Not found")

# --- Review actions ---
st.markdown("---")
action_cols = st.columns(5)

with action_cols[0]:
    if st.button("✓ Approve", type="primary", use_container_width=True):
        record.review_status = "approved"
        record.reviewed_by = "reviewer"
        from datetime import datetime
        record.reviewed_at = datetime.utcnow()
        session.commit()
        st.session_state.review_index = min(idx + 1, len(records) - 1)
        st.rerun()

with action_cols[1]:
    if st.button("✗ Reject", use_container_width=True):
        record.review_status = "rejected"
        record.reviewed_by = "reviewer"
        from datetime import datetime
        record.reviewed_at = datetime.utcnow()
        session.commit()
        st.session_state.review_index = min(idx + 1, len(records) - 1)
        st.rerun()

with action_cols[2]:
    if st.button("⚑ Flag", use_container_width=True):
        record.review_status = "flagged"
        session.commit()
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

# --- Edit values ---
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
        record.scope_1 = new_s1 if new_s1 > 0 else None
        record.scope_2_location = new_s2l if new_s2l > 0 else None
        record.scope_2_market = new_s2m if new_s2m > 0 else None
        record.scope_3 = new_s3 if new_s3 > 0 else None
        if notes:
            record.flag_reason = (record.flag_reason or "") + f"; Review note: {notes}"
        session.commit()
        st.success("Values updated")
        st.rerun()

# --- Overview table ---
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

    # Export
    csv = df.to_csv(index=False)
    st.download_button("Download CSV", csv, "emissions_data.csv", "text/csv")
