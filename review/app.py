"""
Streamlit interface for the corporate emissions database.

Views:
  - Data Table: all companies × years pivot with colour-coded approval status
  - Single Company: per-company year-by-year table with source popups
  - Review: single-record review with approve/reject/flag/edit actions

Run with: streamlit run review/app.py
"""

import base64
import json
import os
import sys

# Ensure the project root is on the Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
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

view = st.sidebar.radio("View", ["Data Table", "Single Company", "Review"], index=0)

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
# Source-popup helpers (shared by Data Table + Single Company views)
# ════════════════════════════════════════════════════════════════════════

def _collect_source_ids(records):
    """Return the set of non-null source_id and market_data_source_id values."""
    ids = set()
    for r in records:
        if r.source_id:
            ids.add(r.source_id)
        mkt = getattr(r, "market_data_source_id", None)
        if mkt:
            ids.add(mkt)
    return ids


def _build_provenance_data(financial_records):
    """Build provenance lookup keyed by "sourceId:fieldName" for financial popups."""
    prov_data = {}
    for fin in financial_records:
        notes_raw = getattr(fin, "extraction_notes", None)
        if not notes_raw:
            continue
        try:
            notes_obj = json.loads(notes_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(notes_obj, dict):
            continue
        provenance = notes_obj.get("provenance", {})
        entity_name = notes_obj.get("entity_name", "")
        lei = notes_obj.get("lei", "")
        src_id = str(fin.source_id) if fin.source_id else None
        mkt_src_id = str(fin.market_data_source_id) if getattr(fin, "market_data_source_id", None) else None
        if not provenance:
            continue
        for field, prov in provenance.items():
            if not isinstance(prov, dict):
                continue
            # equity_value and evic use market_data_source_id
            if field in ("equity_value", "evic"):
                use_id = mkt_src_id
            else:
                use_id = src_id
            if not use_id:
                continue
            key = f"{use_id}:{field}:{fin.reporting_year}"
            ext_date = ""
            if getattr(fin, "extraction_date", None):
                ext_date = fin.extraction_date.strftime("%Y-%m-%d %H:%M")
            entry = {
                "lei": lei,
                "entity_name": entity_name,
                "concept": prov.get("concept", ""),
                "value": prov.get("value"),
                "unit": prov.get("unit", ""),
                "period": prov.get("period", ""),
                "decimals": prov.get("decimals"),
                "year": fin.reporting_year,
                "calculated": prov.get("calculated", False),
                "components": prov.get("components", []),
                "ticker": prov.get("ticker", ""),
                "share_price": prov.get("share_price"),
                "shares": prov.get("shares"),
                "shares_source": prov.get("shares_source", ""),
                "extracted": ext_date,
            }
            prov_data[key] = entry

            # For gross_debt with components, create entries for individual LT/ST debt
            if field == "gross_debt" and prov.get("calculated") and prov.get("components"):
                for comp in prov["components"]:
                    if not isinstance(comp, dict):
                        continue
                    concept_lower = comp.get("concept", "").lower()
                    if any(kw in concept_lower for kw in ("noncurrent", "longterm", "long_term")):
                        comp_key = f"{use_id}:_debt_lt:{fin.reporting_year}"
                    elif any(kw in concept_lower for kw in ("current", "shortterm", "short_term")):
                        comp_key = f"{use_id}:_debt_st:{fin.reporting_year}"
                    else:
                        continue
                    prov_data[comp_key] = {
                        "lei": lei,
                        "entity_name": entity_name,
                        "concept": comp.get("concept", ""),
                        "value": comp.get("value"),
                        "unit": prov.get("unit", ""),
                        "period": comp.get("period", prov.get("period", "")),
                        "decimals": comp.get("decimals", prov.get("decimals")),
                        "year": fin.reporting_year,
                        "calculated": False,
                        "components": [],
                        "extracted": ext_date,
                    }

            # For equity_value, create share price entry from provenance
            if field == "equity_value" and prov.get("share_price") is not None:
                sp_key = f"{use_id}:_share_price:{fin.reporting_year}"
                prov_data[sp_key] = {
                    "lei": lei,
                    "entity_name": entity_name,
                    "concept": "yfinance:ClosePrice",
                    "value": prov.get("share_price"),
                    "unit": prov.get("unit", ""),
                    "period": prov.get("period", ""),
                    "year": fin.reporting_year,
                    "calculated": False,
                    "components": [],
                    "ticker": prov.get("ticker", ""),
                    "extracted": ext_date,
                }
    return prov_data


def _build_source_data(session_obj, source_ids, include_screenshots=False):
    """Build a dict keyed by str(source_id) for embedding in JavaScript."""
    if not source_ids:
        return {}
    sources = session_obj.query(Source).filter(Source.id.in_(source_ids)).all()
    result = {}
    for s in sources:
        entry = {
            "title": s.title or "Untitled",
            "url": s.url or "",
            "type": s.document_type or "Unknown",
            "page": (s.page_number + 1) if s.page_number is not None else None,
            "created": s.fetched_at.strftime("%Y-%m-%d %H:%M") if s.fetched_at else "",
        }
        if include_screenshots and s.screenshot_path and os.path.exists(s.screenshot_path):
            try:
                with open(s.screenshot_path, "rb") as img_f:
                    b64 = base64.b64encode(img_f.read()).decode()
                entry["screenshot"] = "data:image/png;base64," + b64
            except Exception:
                pass
        result[str(s.id)] = entry
    return result


# ── CSS for the source-popup modal ────────────────────────────────────

_POPUP_STYLES = """
/* Modal backdrop + dialog */
.modal-backdrop {
    display:none; position:fixed;
    top:0; left:0; right:0; bottom:0;
    background:rgba(0,0,0,0.45); z-index:999;
}
.source-modal {
    display:none; position:fixed;
    top:50%; left:50%; transform:translate(-50%,-50%);
    background:#fff; border-radius:12px;
    box-shadow:0 8px 32px rgba(0,0,0,0.25);
    z-index:1000; max-width:720px; width:90%;
    max-height:85vh; overflow:hidden;
    animation:modalIn .2s ease;
}
@keyframes modalIn {
    from { opacity:0; transform:translate(-50%,-45%); }
    to   { opacity:1; transform:translate(-50%,-50%); }
}
.modal-hdr {
    display:flex; justify-content:space-between; align-items:center;
    padding:16px 20px 12px; border-bottom:1px solid #eee;
}
.modal-hdr h3 { margin:0; font-size:15px; color:#333; }
.modal-x {
    background:none; border:none; font-size:22px; cursor:pointer;
    color:#999; padding:2px 6px; line-height:1; border-radius:4px;
}
.modal-x:hover { background:#f0f0f0; color:#333; }
.modal-body {
    padding:16px 20px 20px; overflow-y:auto; max-height:calc(85vh - 60px);
}
.modal-body .src-title { font-weight:600; font-size:14px; margin-bottom:8px; }
.modal-body .src-meta  { font-size:13px; color:#666; margin-bottom:4px; }
.modal-body .src-link  { margin-top:10px; }
.modal-body .src-link a { color:#1a73e8; text-decoration:none; font-size:13px; }
.modal-body .src-link a:hover { text-decoration:underline; }
.modal-body .src-screenshot {
    margin-top:12px; border:1px solid #eee;
    border-radius:6px; overflow:hidden;
}
.modal-body .src-screenshot img {
    width:100%; display:block; cursor:zoom-in;
}
/* Full-screen lightbox for expanded image */
.img-lightbox {
    display:none; position:fixed;
    top:0; left:0; right:0; bottom:0;
    background:rgba(0,0,0,0.85); z-index:2000;
    cursor:zoom-out; overflow:auto;
    padding:20px;
}
.img-lightbox.open { display:flex; align-items:flex-start; justify-content:center; }
.img-lightbox img {
    max-width:95vw;
    width:auto; height:auto;
    object-fit:contain; border-radius:4px;
    box-shadow:0 4px 24px rgba(0,0,0,0.5);
    margin:auto;
}
@media (prefers-color-scheme: dark) {
    .source-modal { background:#1e1e2e; }
    .modal-hdr   { border-color:#333; }
    .modal-hdr h3 { color:#e0e0e0; }
    .modal-x     { color:#888; }
    .modal-x:hover { background:#2a2a3a; color:#ddd; }
    .modal-body .src-title { color:#e0e0e0; }
    .modal-body .src-meta  { color:#999; }
    .modal-body .src-link a { color:#6db3f2; }
    .modal-body .src-screenshot { border-color:#333; }
}
/* Clickable-number styling */
.has-source {
    cursor:pointer;
    color:#1a73e8 !important;
    text-decoration:underline;
    text-decoration-color:rgba(26,115,232,0.3);
    text-underline-offset:2px;
}
.has-source:hover {
    text-decoration-color:rgba(26,115,232,0.8);
    background:rgba(26,115,232,0.06);
}
@media (prefers-color-scheme: dark) {
    .has-source { color:#6db3f2 !important; text-decoration-color:rgba(109,179,242,0.3); }
    .has-source:hover { text-decoration-color:rgba(109,179,242,0.8); background:rgba(109,179,242,0.06); }
}
"""

# ── JavaScript for source popup (.replace("__SOURCES__", json)) ───────

_POPUP_JS = r"""
var SOURCES=__SOURCES__;
var PROVENANCE=__PROVENANCE__;
var FIN_BASIS=__FIN_BASIS__;
function fmtNum(v){
    if(v==null) return '—';
    if(Math.abs(v)>=1e9) return (v/1e9).toFixed(2)+'bn';
    if(Math.abs(v)>=1e6) return (v/1e6).toFixed(1)+'m';
    return v.toLocaleString();
}
function fmtPeriod(p){
    if(!p) return '—';
    var parts=p.replace(/T00:00:00/g,'').split('/');
    return parts.join(' to ');
}
function showSource(sid,field,yr){
    var s=SOURCES[String(sid)]; if(!s) return;
    var h='<div class="src-title">📄 '+esc(s.title)+'</div>';
    h+='<div class="src-meta">Type: '+esc(s.type);
    if(s.page) h+=' &nbsp;|&nbsp; Page: '+s.page;
    h+='</div>';
    if(s.url) h+='<div class="src-link"><a href="'+esc(s.url)+'" target="_blank" rel="noopener">Open source document ↗</a></div>';
    // Show provenance details for Tier 1/2 financial fields
    if(field){
        var pk=String(sid)+':'+field+(yr?':'+yr:'');
        var pv=PROVENANCE[pk];
        if(pv){
            if(pv.calculated && pv.components && pv.components.length>0){
                // Show calculation formula with full detail
                var formula=field==='equity_value'?'Market cap':field.replace(/_/g,' ');
                formula=formula.charAt(0).toUpperCase()+formula.slice(1);
                h+='<div style="margin-top:14px;padding:14px;background:#f0f7ff;border-radius:6px;border:1px solid #d0e3f7">';
                h+='<div style="font-size:11px;text-transform:uppercase;color:#888;margin-bottom:8px;letter-spacing:0.5px">Calculated value</div>';
                // Formula line
                var fparts=[];
                for(var ci=0;ci<pv.components.length;ci++){
                    var comp=pv.components[ci];
                    var cname=comp.concept||'?';
                    cname=cname.replace(/^ifrs-full:/,'').replace(/^us-gaap:/,'').replace(/^yfinance:/,'');
                    fparts.push(cname+' ('+fmtNum(comp.value)+')');
                }
                var joiner=(field==='equity_value')?' × ':' + ';
                h+='<div style="font-size:14px;font-weight:600;color:#111;margin-bottom:10px">'+esc(formula)+' = '+fparts.join(joiner)+'</div>';
                // Unit
                var calcUnit=(pv.unit||'').replace('iso4217:','');
                if(calcUnit) h+='<div style="font-size:12px;color:#111;margin-bottom:4px">Units: '+esc(calcUnit)+'</div>';
                // Per-component periods
                for(var ci2=0;ci2<pv.components.length;ci2++){
                    var comp2=pv.components[ci2];
                    var cn2=comp2.concept||'?';
                    cn2=cn2.replace(/^ifrs-full:/,'').replace(/^us-gaap:/,'').replace(/^yfinance:/,'');
                    var cp2=comp2.period||pv.period||'';
                    if(cp2) h+='<div style="font-size:12px;color:#111">Period — '+esc(cn2)+': '+fmtPeriod(cp2)+'</div>';
                }
                h+='</div>';
            } else {
                // Standard provenance table for directly extracted values
                h+='<table style="margin-top:14px;font-size:13px;border-collapse:collapse;width:100%">';
                var rows=[
                    ['LEI',pv.lei||'—'],
                    ['Entity name',pv.entity_name||'—'],
                    ['XBRL concept','<code>'+esc(pv.concept)+'</code>'],
                    ['Value',fmtNum(pv.value)+' <span style="color:#888">(raw: '+(pv.value!=null?pv.value.toLocaleString():'—')+')</span>'],
                    ['Unit',esc((pv.unit||'').replace('iso4217:',''))],
                    ['Period',fmtPeriod(pv.period)],
                    ['Decimals',pv.decimals!=null?String(pv.decimals):'—']
                ];
                for(var i=0;i<rows.length;i++){
                    h+='<tr><td style="padding:4px 10px 4px 0;color:#888;white-space:nowrap;vertical-align:top">'+rows[i][0]+'</td>';
                    h+='<td style="padding:4px 0;font-weight:500">'+rows[i][1]+'</td></tr>';
                }
                h+='</table>';
            }
            if(pv.extracted) h+='<div style="margin-top:10px;font-size:11px;color:#999">Extracted: '+esc(pv.extracted)+' UTC</div>';
        }
    }
    if(s.screenshot) h+='<div class="src-screenshot"><img src="'+s.screenshot+'" onclick="expandImg(this.src)" title="Click to expand"></div>';
    if(s.created) h+='<div style="margin-top:10px;font-size:11px;color:#999">Source fetched: '+esc(s.created)+' UTC</div>';
    document.getElementById('modal-title').textContent='Source';
    document.getElementById('modal-body').innerHTML=h;
    document.getElementById('modal-backdrop').style.display='block';
    document.getElementById('source-modal').style.display='block';
}
function showYfinance(){
    var h='<div class="src-title">📊 Yahoo Finance</div>';
    h+='<div class="src-meta">Market data retrieved via yfinance Python library</div>';
    h+='<div class="src-meta" style="margin-top:8px">Equity value = market capitalisation at fiscal year-end</div>';
    h+='<div class="src-meta">Enterprise value = equity + debt − cash</div>';
    document.getElementById('modal-title').textContent='Market Data Source';
    document.getElementById('modal-body').innerHTML=h;
    document.getElementById('modal-backdrop').style.display='block';
    document.getElementById('source-modal').style.display='block';
}
function showFinBasis(yr){
    var fb=FIN_BASIS[String(yr)]; if(!fb) return;
    var h='<div style="font-weight:600;margin-bottom:10px">Period by field — '+yr+'</div>';
    h+='<table style="border-collapse:collapse;width:100%;font-size:13px">';
    var keys=Object.keys(fb).sort();
    for(var i=0;i<keys.length;i++){
        var p=fb[keys[i]].replace(/T00:00:00/g,'');
        if(p.indexOf('/')>=0){var pp=p.split('/');p=pp[0]+' to '+pp[1];}
        h+='<tr><td style="padding:4px 10px 4px 0;color:#888;white-space:nowrap">'+esc(keys[i])+'</td>';
        h+='<td style="padding:4px 0;font-weight:500">'+esc(p)+'</td></tr>';
    }
    h+='</table>';
    document.getElementById('modal-title').textContent='Financial Basis';
    document.getElementById('modal-body').innerHTML=h;
    document.getElementById('modal-backdrop').style.display='block';
    document.getElementById('source-modal').style.display='block';
}
function closeModal(){
    document.getElementById('modal-backdrop').style.display='none';
    document.getElementById('source-modal').style.display='none';
}
function expandImg(src){
    var lb=document.getElementById('img-lightbox');
    lb.querySelector('img').src=src;
    lb.classList.add('open');
}
function closeLightbox(){
    document.getElementById('img-lightbox').classList.remove('open');
}
function esc(s){var d=document.createElement('div');d.textContent=s||'';return d.innerHTML;}
document.addEventListener('keydown',function(e){
    if(e.key==='Escape'){
        var lb=document.getElementById('img-lightbox');
        if(lb && lb.classList.contains('open')) closeLightbox();
        else closeModal();
    }
});
"""

# ── Modal HTML container ──────────────────────────────────────────────

_POPUP_MODAL_HTML = """
<div id="modal-backdrop" class="modal-backdrop" onclick="closeModal()"></div>
<div id="source-modal" class="source-modal">
    <div class="modal-hdr">
        <h3 id="modal-title">Source</h3>
        <button class="modal-x" onclick="closeModal()">&times;</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
</div>
<div id="img-lightbox" class="img-lightbox" onclick="closeLightbox()">
    <img src="" alt="Expanded screenshot">
</div>
"""


def _source_popup_block(source_data, provenance_data=None, fin_basis_data=None):
    """Return modal container + <script> with embedded source data."""
    safe_json = json.dumps(source_data).replace("</", "<\\/")
    safe_prov = json.dumps(provenance_data or {}).replace("</", "<\\/")
    safe_fb = json.dumps(fin_basis_data or {}).replace("</", "<\\/")
    js = (_POPUP_JS
          .replace("__SOURCES__", safe_json)
          .replace("__PROVENANCE__", safe_prov)
          .replace("__FIN_BASIS__", safe_fb))
    return _POPUP_MODAL_HTML + "\n<script>" + js + "</script>"


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

    # Show all years from 2019 to present (matching pipeline target range)
    current_year = datetime.utcnow().year
    all_years = list(range(2019, current_year + 1))

    if not emissions and not financials:
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
        ("Gross Debt", "gross_debt", "financial"),
        ("Share Price", "_share_price", "market_component"),
        ("Shares Out", "shares_outstanding", "financial"),
        ("Mkt Cap (calc)", "equity_value", "market"),
        ("EVIC (calc)", "evic", "market"),
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
    _MONTH_NAMES_DT = {1: "January", 2: "February", 3: "March", 4: "April",
                        5: "May", 6: "June", 7: "July", 8: "August",
                        9: "September", 10: "October", 11: "November", 12: "December"}

    def record_basis(rec):
        """Return e.g. 'End December 2025' from a record's period_end."""
        if not rec or not rec.period_end:
            return "—"
        m, y = rec.period_end.month, rec.period_end.year
        return f"End {_MONTH_NAMES_DT[m]} {y}"

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

    # ── Build source data for popups ──────────────────────────────────
    source_ids = _collect_source_ids(emissions) | _collect_source_ids(financials)
    source_data = _build_source_data(session, source_ids, include_screenshots=False)

    # ── Build HTML table ───────────────────────────────────────────────
    html_parts = []

    # Count basis columns: 1 per visible category
    n_basis = (1 if show_emissions else 0) + (1 if show_financials else 0)

    # Styles: body (for iframe), table, popup
    html_parts.append("<style>")
    html_parts.append("""
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        margin: 0; padding: 4px; background: #ffffff; color: #333;
    }
    @media (prefers-color-scheme: dark) {
        body { background: #0e1117; color: #e0e0e0; }
    }
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
    """)
    html_parts.append(_POPUP_STYLES)
    html_parts.append("</style>")

    html_parts.append('<div class="data-table-wrap"><table class="data-table">')

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
    n_visible = 0
    for company in companies:
        # Skip companies with no data at all
        has_any = any(
            (company.id, y) in em_by_key or (company.id, y) in fin_by_key
            for y in all_years
        )
        if not has_any:
            continue
        n_visible += 1

        html_parts.append(f"<tr><td class='company-name'>{company.name}</td>")

        for year in all_years:
            em = em_by_key.get((company.id, year))
            fin = fin_by_key.get((company.id, year))

            for label, field_name, source_type in fields:
                if source_type == "emissions":
                    value = getattr(em, field_name, None) if em else None
                    status = em.review_status if em else None
                    src_id = em.source_id if em else None
                elif source_type in ("market", "market_component"):
                    if source_type == "market_component":
                        value = getattr(fin, "share_price_at_fy_end", None) if fin else None
                    else:
                        value = getattr(fin, field_name, None) if fin else None
                    status = fin.review_status if fin else None
                    src_id = getattr(fin, "market_data_source_id", None) if fin else None
                else:
                    value = getattr(fin, field_name, None) if fin else None
                    status = fin.review_status if fin else None
                    src_id = fin.source_id if fin else None

                if value is not None:
                    css_class = "approved" if status == "approved" else "not-approved"
                    display_val = f"{value:,.2f}" if source_type == "market_component" else fmt_num(value)
                    if src_id:
                        html_parts.append(
                            f"<td class='{css_class} has-source' "
                            f"onclick=\"showSource({src_id},'{field_name}',{year})\">{display_val}</td>"
                        )
                    else:
                        html_parts.append(f"<td class='{css_class}'>{display_val}</td>")
                else:
                    html_parts.append("<td class='no-data'>—</td>")

            # Basis columns at end
            if show_emissions:
                em_basis = record_basis(em)
                em_basis_class = "not-approved" if em_basis == "—" else "approved"
                html_parts.append(
                    f"<td class='{em_basis_class}' style='text-align:center'>{em_basis}</td>"
                )
            if show_financials:
                fin_basis = record_basis(fin)
                fin_basis_class = "not-approved" if fin_basis == "—" else "approved"
                html_parts.append(
                    f"<td class='{fin_basis_class}' style='text-align:center'>{fin_basis}</td>"
                )

        html_parts.append("</tr>")

    html_parts.append("</tbody></table></div>")

    # Append modal + JS (with provenance)
    dt_provenance = _build_provenance_data(financials)
    html_parts.append(_source_popup_block(source_data, dt_provenance))

    # Render via components.html (iframe with JS support)
    table_height = max(500, 100 + n_visible * 35)
    components.html("".join(html_parts), height=table_height, scrolling=True)

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
                em_source = session.query(Source).get(em.source_id) if em.source_id else None
                row["Emissions Source URL"] = em_source.url if em_source else ""
                row["Emissions Source Title"] = em_source.title if em_source else ""
            if fin:
                row["Financial Status"] = fin.review_status
                row["Revenue"] = getattr(fin, "revenue", None)
                row["Gross Debt"] = getattr(fin, "gross_debt", None)
                row["Lease Liabilities"] = getattr(fin, "lease_liabilities", None)
                row["NCI"] = getattr(fin, "non_controlling_interests", None)
                row["Preference Shares"] = getattr(fin, "preference_shares", None)
                row["Shares Outstanding"] = getattr(fin, "shares_outstanding", None)
                row["Equity Value"] = getattr(fin, "equity_value", None)
                row["EVIC"] = getattr(fin, "evic", None)
                row["Currency"] = getattr(fin, "currency", None)
                row["Source Tier"] = getattr(fin, "source_tier", None)
                if getattr(fin, "period_start", None) and getattr(fin, "period_end", None):
                    row["Financial Period"] = f"{fin.period_start} to {fin.period_end}"
                fin_source = session.query(Source).get(fin.source_id) if fin.source_id else None
                row["Financial Source URL"] = fin_source.url if fin_source else ""
                row["Financial Source Title"] = fin_source.title if fin_source else ""

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
        # Safe accessor for columns that may not exist in older DBs
        def _fa(attr, default=None):
            return getattr(fin_record, attr, default)

        tier_label = {1: "Tier 1 (XBRL API)", 2: "Tier 2 (yfinance)", 3: "Tier 3 (PDF/LLM)"}.get(
            _fa("source_tier"), "Unknown")
        st.subheader(f"Financial Data — {tier_label}")
        if _fa("period_start") and _fa("period_end"):
            st.caption(f"📅 Financial period: {fin_record.period_start.strftime('%d %b %Y')} – {fin_record.period_end.strftime('%d %b %Y')}")
        elif _fa("fiscal_year_end"):
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

        def _conf_icon(conf):
            if conf == "high":
                return "🟢"
            if conf == "medium":
                return "🟡"
            if conf == "low":
                return "🔴"
            return ""

        ccy = _fa("currency") or ""

        def _short_ref(ref):
            if not ref:
                return "—"
            for prefix in ("ifrs-full:", "us-gaap:", "dei:"):
                ref = ref.replace(prefix, "")
            return ref

        # Parse provenance for calculated-field labelling
        _prov_fields = {}
        try:
            _notes_obj = json.loads(fin_record.extraction_notes) if _fa("extraction_notes") else {}
            _prov_fields = _notes_obj.get("provenance", {}) if isinstance(_notes_obj, dict) else {}
        except (json.JSONDecodeError, TypeError):
            pass

        def _calc_label(base, field_key):
            p = _prov_fields.get(field_key, {})
            if isinstance(p, dict) and p.get("calculated"):
                return base + " (calc)"
            return base

        fin_col1, fin_col2 = st.columns(2)
        with fin_col1:
            st.markdown("**PCAF EVIC inputs (from filings):**")
            pcaf_rows = [
                (_calc_label("Revenue", "revenue"), _fa("revenue"), _fa("revenue_confidence"), _fa("revenue_ref")),
                (_calc_label("Gross debt", "gross_debt"), _fa("gross_debt"), _fa("gross_debt_confidence"), _fa("gross_debt_ref")),
                (_calc_label("Lease liabilities", "lease_liabilities"), _fa("lease_liabilities"), _fa("lease_liabilities_confidence"), _fa("lease_liabilities_ref")),
                (_calc_label("NCI", "non_controlling_interests"), _fa("non_controlling_interests"), _fa("nci_confidence"), _fa("nci_ref")),
                ("Preference shares", _fa("preference_shares"), None, _fa("preference_shares_ref")),
                ("Shares outstanding", _fa("shares_outstanding"), _fa("shares_outstanding_confidence"), _fa("shares_outstanding_ref")),
            ]
            st.table(pd.DataFrame({
                "Metric": [r[0] for r in pcaf_rows],
                "Value": [_fmt_currency(r[1], ccy) for r in pcaf_rows],
                "Conf.": [_conf_icon(r[2]) for r in pcaf_rows],
                "XBRL concept": [_short_ref(r[3]) for r in pcaf_rows],
            }))
            if _fa("gross_debt_components"):
                try:
                    components_list = json.loads(fin_record.gross_debt_components)
                    if components_list:
                        parts = []
                        for comp in components_list:
                            if isinstance(comp, dict):
                                cname = comp.get("concept", "?")
                                for pfx in ("ifrs-full:", "us-gaap:", "yfinance:"):
                                    cname = cname.replace(pfx, "")
                                cval = comp.get("value")
                                if cval is not None:
                                    parts.append(f"{cname} ({cval:,.0f})")
                                else:
                                    parts.append(cname)
                            else:
                                parts.append(str(comp))
                        st.caption(f"Gross debt = {' + '.join(parts)}")
                except (json.JSONDecodeError, TypeError):
                    pass
            if _fa("is_financial_institution"):
                st.warning("Classified as financial institution (bank/insurer/asset manager)")

        with fin_col2:
            st.markdown("**Market data + EVIC:**")
            st.table(pd.DataFrame({
                "Metric": ["Equity value (market cap)", "EVIC (calculated)"],
                "Value": [
                    _fmt_currency(_fa("equity_value"), _fa("equity_currency")),
                    _fmt_currency(_fa("evic"), _fa("equity_currency") or ccy),
                ],
            }))
            if _fa("fiscal_year_end"):
                st.caption(f"As at fiscal year-end: {fin_record.fiscal_year_end}")
            if _fa("evic") and _fa("equity_value"):
                debt_part = (_fa("gross_debt") or 0)
                nci_part = (_fa("non_controlling_interests") or 0)
                st.caption(
                    f"EVIC = {_fmt_currency(_fa('equity_value'))} (market cap) "
                    f"+ {_fmt_currency(debt_part)} (total debt) "
                    f"+ {_fmt_currency(nci_part)} (NCI)"
                )
            if _fa("validation_flags"):
                try:
                    flags = json.loads(fin_record.validation_flags)
                    if flags:
                        st.warning(f"Validation flags: {', '.join(flags)}")
                except (json.JSONDecodeError, TypeError):
                    pass

        # Financial source info
        fin_source = fin_record.source if getattr(fin_record, "source_id", None) else None
        if fin_source:
            st.markdown("**Financial source:**")
            st.write(f"📄 {fin_source.title or 'Untitled'}")
            st.write(f"Type: {fin_source.document_type}")
            if fin_source.url:
                st.markdown(f"[Open URL]({fin_source.url})")
            if fin_source.page_number is not None:
                st.caption(f"Page: {fin_source.page_number + 1}")

            if fin_source.screenshot_path and os.path.exists(fin_source.screenshot_path):
                st.markdown("**Financial source table (PDF screenshot):**")
                st.image(fin_source.screenshot_path, use_container_width=True)
            elif fin_source.html_snippet:
                st.markdown("**Financial source table:**")
                st.markdown(fin_source.html_snippet, unsafe_allow_html=True)

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
# SINGLE COMPANY VIEW
# ════════════════════════════════════════════════════════════════════════

def render_single_company():
    st.title("Single Company Table")

    companies = session.query(Company).order_by(Company.name).all()
    if not companies:
        st.info("No companies in the database yet.")
        return

    options = {f"{c.id} — {c.name}": c.id for c in companies}
    selected = st.selectbox("Select company", list(options.keys()))
    company_id = options[selected]
    company = session.query(Company).get(company_id)

    # Load data for this company
    emissions = (
        session.query(EmissionsRecord)
        .filter_by(company_id=company_id)
        .order_by(EmissionsRecord.reporting_year)
        .all()
    )
    financials = (
        session.query(FinancialRecord)
        .filter_by(company_id=company_id)
        .order_by(FinancialRecord.reporting_year)
        .all()
    )

    em_by_year = {e.reporting_year: e for e in emissions}
    fin_by_year = {f.reporting_year: f for f in financials}

    all_years = sorted(set(em_by_year.keys()) | set(fin_by_year.keys()))

    if not all_years:
        st.info(f"No data for {company.name} yet.")
        return

    st.subheader(company.name)
    if company.ticker:
        st.caption(f"Ticker: {company.ticker}")

    # ── Build source data (with screenshots for single-company view) ──
    source_ids = _collect_source_ids(emissions) | _collect_source_ids(financials)
    source_data = _build_source_data(session, source_ids, include_screenshots=True)
    provenance_data = _build_provenance_data(financials)

    # ── Helpers ────────────────────────────────────────────────────────
    _MONTH_NAMES_SC = {1: "January", 2: "February", 3: "March", 4: "April",
                        5: "May", 6: "June", 7: "July", 8: "August",
                        9: "September", 10: "October", 11: "November", 12: "December"}

    def record_basis(rec):
        if not rec or not rec.period_end:
            return "—"
        m, y = rec.period_end.month, rec.period_end.year
        return f"End {_MONTH_NAMES_SC[m]} {y}"

    def fmt(value):
        if value is None:
            return "—"
        if abs(value) >= 1_000_000_000:
            return f"{value / 1_000_000_000:,.2f}bn"
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:,.1f}m"
        if abs(value) >= 1_000:
            return f"{value / 1_000:,.1f}k"
        if value != int(value):
            return f"{value:,.2f}".rstrip("0").rstrip(".")
        return f"{int(value):,}"

    # ── Field definitions ─────────────────────────────────────────────
    fields = [
        ("S1", "scope_1", "emissions"),
        ("S2 loc", "scope_2_location", "emissions"),
        ("S2 mkt", "scope_2_market", "emissions"),
        ("S3", "scope_3", "emissions"),
        ("Revenue", "revenue", "financial"),
        ("LT Debt", "_debt_lt", "debt_component"),
        ("ST Debt", "_debt_st", "debt_component"),
        ("Gross Debt (calc)", "gross_debt", "financial"),
        ("Lease Liab.", "lease_liabilities", "financial"),
        ("NCI", "non_controlling_interests", "financial"),
        ("Pref Shares", "preference_shares", "financial"),
        ("Share Price", "_share_price", "market_component"),
        ("Shares Out", "shares_outstanding", "financial"),
        ("Mkt Cap (calc)", "equity_value", "market"),
        ("EVIC (calc)", "evic", "market"),
        ("Tier", "source_tier", "financial"),
        ("Em. Basis", "_em_basis", "basis"),
        ("Fin. Basis", "_fin_basis", "basis"),
    ]

    # Pre-extract debt component values per year from provenance
    def _get_debt_components(fin_record):
        """Extract LT and ST debt component values and provenance from a financial record."""
        if not fin_record:
            return None, None, None, None
        comps_raw = getattr(fin_record, "gross_debt_components", None)
        if not comps_raw:
            return None, None, None, None
        try:
            comps = json.loads(comps_raw)
        except (json.JSONDecodeError, TypeError):
            return None, None, None, None
        lt_val = st_val = None
        lt_concept = st_concept = None
        for comp in comps:
            if not isinstance(comp, dict):
                continue
            concept = comp.get("concept", "").lower()
            val = comp.get("value")
            if any(kw in concept for kw in ("noncurrent", "longterm", "long_term", "longtermdebt")):
                lt_val = val
                lt_concept = comp.get("concept", "")
            elif any(kw in concept for kw in ("current", "shortterm", "short_term", "shorttermborr")):
                st_val = val
                st_concept = comp.get("concept", "")
        return lt_val, lt_concept, st_val, st_concept

    debt_components_by_year = {}
    for year_key, fin_rec in fin_by_year.items():
        lt, lt_c, sht, sht_c = _get_debt_components(fin_rec)
        debt_components_by_year[year_key] = {
            "lt_val": lt, "lt_concept": lt_c,
            "st_val": sht, "st_concept": sht_c,
        }

    # ── Fin basis helper ────────────────────────────────────────────
    fin_basis_details = {}  # {year: {field_label: period_str}} for inconsistent years

    def _render_fin_basis(fin_rec, prov_data, yr):
        """Render the financial basis cell from provenance periods."""
        if not fin_rec:
            return "<td class='no-data' style='text-align:center'>—</td>"

        sid = str(fin_rec.source_id) if fin_rec.source_id else None
        mkt_sid = str(fin_rec.market_data_source_id) if getattr(fin_rec, "market_data_source_id", None) else None

        # Collect periods from provenance for each EVIC-relevant field
        fin_fields_to_check = [
            ("Revenue", "revenue", sid),
            ("Gross Debt", "gross_debt", sid),
            ("Lease Liab.", "lease_liabilities", sid),
            ("NCI", "non_controlling_interests", sid),
            ("Pref Shares", "preference_shares", sid),
            ("Equity", "equity_value", mkt_sid),
        ]

        field_periods = {}
        for flabel, fname, use_sid in fin_fields_to_check:
            if not use_sid or not prov_data:
                continue
            pk = f"{use_sid}:{fname}:{yr}"
            pv = prov_data.get(pk)
            if pv and pv.get("period"):
                field_periods[flabel] = pv["period"]

        if not field_periods:
            fye = getattr(fin_rec, "fiscal_year_end", None)
            if fye:
                return f"<td style='text-align:center'>{_month_label(fye.month, fye.year)}</td>"
            return "<td class='no-data' style='text-align:center'>—</td>"

        _MONTH_NAMES = {1: "January", 2: "February", 3: "March", 4: "April",
                        5: "May", 6: "June", 7: "July", 8: "August",
                        9: "September", 10: "October", 11: "November", 12: "December"}

        def _month_label(month, year):
            return f"End {_MONTH_NAMES[month]} {year}"

        def _period_to_month_year(period_str):
            """Normalize a period end date to (month, year).

            Treats Dec 29-31 and Jan 1 of next year as End December.
            For other months, uses the last day's month.
            """
            p = period_str.replace("T00:00:00", "")
            if "/" in p:
                end = p.split("/")[-1]
            else:
                end = p
            try:
                parts = end.split("-")
                y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
            except (ValueError, IndexError):
                return None
            # Jan 1 = End December of prior year
            if m == 1 and d == 1:
                return (12, y - 1)
            # Dec 29-31 all count as End December
            if m == 12 and d >= 29:
                return (12, y)
            # Last few days of any month = that month's end
            return (m, y)

        normalized = set()
        for period in field_periods.values():
            result = _period_to_month_year(period)
            if result:
                normalized.add(result)

        if len(normalized) == 1:
            m, y = normalized.pop()
            return f"<td style='text-align:center'>{_month_label(m, y)}</td>"

        # Inconsistent — store data for modal popup
        fin_basis_details[yr] = field_periods
        return (
            f"<td style='text-align:center'>"
            f"<span class='has-source' onclick='showFinBasis({yr})' "
            f"title='Click to see period details'>Inconsistent</span></td>"
        )

    # ── Build HTML table ──────────────────────────────────────────────
    html = []

    # Styles
    html.append("<style>")
    html.append("""
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        margin: 0; padding: 4px; background: #ffffff; color: #333;
    }
    @media (prefers-color-scheme: dark) {
        body { background: #0e1117; color: #e0e0e0; }
    }
    .sc-table {
        border-collapse: collapse;
        font-size: 13px;
        width: 100%;
    }
    .sc-table th {
        background: #f0f2f6;
        border: 1px solid #ddd;
        padding: 6px 10px;
        text-align: center;
        font-weight: 600;
        position: sticky;
        top: 0;
        z-index: 1;
    }
    .sc-table td {
        border: 1px solid #eee;
        padding: 5px 10px;
        text-align: right;
    }
    .sc-table td:first-child {
        text-align: center;
        font-weight: 600;
    }
    .sc-table tr:hover { background: #f8f9fb; }
    .no-data { color: #ccc; }
    @media (prefers-color-scheme: dark) {
        .sc-table th { background: #262730; border-color: #444; color: #fafafa; }
        .sc-table td { border-color: #333; color: #e0e0e0; }
        .sc-table td:first-child { color: #fafafa; }
        .sc-table tr:hover { background: #1a1c24; }
        .no-data { color: #444; }
    }
    """)
    html.append(_POPUP_STYLES)
    html.append("</style>")

    # Table header
    html.append('<table class="sc-table"><thead><tr><th>Year</th>')
    for label, _, _ in fields:
        html.append(f"<th>{label}</th>")
    html.append("</tr></thead><tbody>")

    # Table rows
    for year in all_years:
        em = em_by_year.get(year)
        fin = fin_by_year.get(year)
        html.append(f"<tr><td>{year}</td>")

        for label, field_name, source_type in fields:
            if source_type == "basis":
                if field_name == "_em_basis":
                    basis = record_basis(em)
                    cls = "no-data" if basis == "—" else ""
                    html.append(f"<td class='{cls}' style='text-align:center'>{basis}</td>")
                else:
                    # Fin basis: derive from provenance periods
                    fin_basis_html = _render_fin_basis(fin, provenance_data, year)
                    html.append(fin_basis_html)
            elif source_type == "emissions":
                value = getattr(em, field_name, None) if em else None
                src_id = em.source_id if em else None
                if value is not None:
                    if src_id:
                        html.append(
                            f"<td class='has-source' onclick='showSource({src_id})'>"
                            f"{fmt(value)}</td>"
                        )
                    else:
                        html.append(f"<td>{fmt(value)}</td>")
                else:
                    html.append("<td class='no-data'>—</td>")
            elif source_type == "debt_component":
                # Derived from gross_debt_components JSON
                dc = debt_components_by_year.get(year, {})
                if field_name == "_debt_lt":
                    value = dc.get("lt_val")
                else:
                    value = dc.get("st_val")
                src_id = fin.source_id if fin else None
                if value is not None and src_id:
                    html.append(
                        f"<td class='has-source' onclick=\"showSource({src_id},'{field_name}',{year})\">"
                        f"{fmt(value)}</td>"
                    )
                elif value is not None:
                    html.append(f"<td>{fmt(value)}</td>")
                else:
                    html.append("<td class='no-data'>—</td>")
            elif source_type == "market_component":
                # Share price from equity_value provenance
                mkt_src_id = getattr(fin, "market_data_source_id", None) if fin else None
                value = getattr(fin, "share_price_at_fy_end", None) if fin else None
                if value is not None and mkt_src_id:
                    html.append(
                        f"<td class='has-source' onclick=\"showSource({mkt_src_id},'{field_name}',{year})\">"
                        f"{value:,.2f}</td>"
                    )
                elif value is not None:
                    html.append(f"<td>{value:,.2f}</td>")
                else:
                    html.append("<td class='no-data'>—</td>")
            elif source_type == "market":
                # Mkt Cap and EVIC use market_data_source_id
                value = getattr(fin, field_name, None) if fin else None
                mkt_src_id = getattr(fin, "market_data_source_id", None) if fin else None
                mkt_calc = ('<span style="font-size:9px;color:#888;vertical-align:super" '
                            'title="Calculated from components"> calc</span>') if field_name in ("evic", "equity_value") else ""
                if value is not None:
                    if mkt_src_id:
                        html.append(
                            f"<td class='has-source' onclick=\"showSource({mkt_src_id},'{field_name}',{year})\">"
                            f"{fmt(value)}{mkt_calc}</td>"
                        )
                    else:
                        html.append(f"<td>{fmt(value)}{mkt_calc}</td>")
                else:
                    html.append("<td class='no-data'>—</td>")
            else:  # financial
                value = getattr(fin, field_name, None) if fin else None
                src_id = fin.source_id if fin else None
                # Check if this field is calculated (from provenance)
                is_calc = False
                if fin and src_id and provenance_data:
                    pk = f"{src_id}:{field_name}:{year}"
                    fp = provenance_data.get(pk)
                    if fp and fp.get("calculated"):
                        is_calc = True
                calc_badge = ('<span style="font-size:9px;color:#888;vertical-align:super" '
                              'title="Calculated from components"> calc</span>') if is_calc else ""
                if value is not None:
                    if field_name == "source_tier":
                        html.append(f"<td style='text-align:center'>{int(value)}</td>")
                    elif src_id:
                        html.append(
                            f"<td class='has-source' onclick=\"showSource({src_id},'{field_name}',{year})\">"
                            f"{fmt(value)}{calc_badge}</td>"
                        )
                    else:
                        html.append(f"<td>{fmt(value)}{calc_badge}</td>")
                else:
                    html.append("<td class='no-data'>—</td>")

        html.append("</tr>")

    html.append("</tbody></table>")

    # Append modal + JS (with provenance for financial field popups)
    html.append(_source_popup_block(source_data, provenance_data, fin_basis_details))

    # Render
    table_height = max(500, 70 + len(all_years) * 34)
    components.html("".join(html), height=table_height, scrolling=True)

    # ── CSV download (outside iframe) ─────────────────────────────────
    csv_rows = []
    for year in all_years:
        em = em_by_year.get(year)
        fin = fin_by_year.get(year)
        dc = debt_components_by_year.get(year, {})
        csv_rows.append({
            "Year": year,
            "S1": fmt(em.scope_1) if em else "—",
            "S2 loc": fmt(em.scope_2_location) if em else "—",
            "S2 mkt": fmt(em.scope_2_market) if em else "—",
            "S3": fmt(em.scope_3) if em else "—",
            "Revenue": fmt(getattr(fin, "revenue", None)) if fin else "—",
            "LT Debt": fmt(dc.get("lt_val")) if dc.get("lt_val") is not None else "—",
            "ST Debt": fmt(dc.get("st_val")) if dc.get("st_val") is not None else "—",
            "Gross Debt (calc)": fmt(getattr(fin, "gross_debt", None)) if fin else "—",
            "Lease Liab.": fmt(getattr(fin, "lease_liabilities", None)) if fin else "—",
            "NCI": fmt(getattr(fin, "non_controlling_interests", None)) if fin else "—",
            "Pref Shares": fmt(getattr(fin, "preference_shares", None)) if fin else "—",
            "Share Price": f"{fin.share_price_at_fy_end:,.2f}" if fin and getattr(fin, "share_price_at_fy_end", None) else "—",
            "Shares Out": fmt(getattr(fin, "shares_outstanding", None)) if fin else "—",
            "Mkt Cap (calc)": fmt(getattr(fin, "equity_value", None)) if fin else "—",
            "EVIC (calc)": fmt(getattr(fin, "evic", None)) if fin else "—",
            "Tier": getattr(fin, "source_tier", None) if fin else "—",
            "Em. Basis": record_basis(em),
            "Fin. Basis": record_basis(fin),
        })
    df = pd.DataFrame(csv_rows)
    csv = df.to_csv(index=False)
    safe_name = company.name.lower().replace(" ", "_").replace("&", "and")
    st.download_button("Download CSV", csv, f"{safe_name}_data.csv", "text/csv")


# ════════════════════════════════════════════════════════════════════════
# Route to selected view
# ════════════════════════════════════════════════════════════════════════

if view == "Data Table":
    render_data_table()
elif view == "Single Company":
    render_single_company()
else:
    render_review()
