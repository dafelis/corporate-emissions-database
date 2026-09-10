"""
Emissions Extraction Workbench

Interactive Streamlit tool for debugging and tuning the emissions extraction
pipeline.  Run individual company/year combinations with full visibility into
each stage:  search → rank → parse → table rank → extract.

Run with:  streamlit run review/workbench.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import anthropic
from exa_py import Exa

from pipeline.parser import (
    parse_pdf,
    extract_tables_from_documents,
    parse_html,
    parse_excel,
    extract_html_text,
    detect_source_type,
)
from pipeline.extractor import EMISSIONS_SCHEMA
from data.ftse100 import FTSE_100

# ════════════════════════════════════════════════════════════════════════
# Setup
# ════════════════════════════════════════════════════════════════════════

st.set_page_config(page_title="Emissions Workbench", layout="wide")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EXA_KEY = os.environ.get("EXA_API_KEY", "")
LLAMA_KEY = os.environ.get("LLAMA_CLOUD_API_KEY", "")

missing = []
if not ANTHROPIC_KEY:
    missing.append("ANTHROPIC_API_KEY")
if not EXA_KEY:
    missing.append("EXA_API_KEY")
if not LLAMA_KEY:
    missing.append("LLAMA_CLOUD_API_KEY")
if missing:
    st.error(f"Missing API keys in .env: {', '.join(missing)}")
    st.stop()

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
exa = Exa(api_key=EXA_KEY)

# Try connecting to DB for comparison (optional)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
db_session = None
if DATABASE_URL:
    try:
        from db.models import Company, EmissionsRecord, get_session
        db_session = get_session(DATABASE_URL)
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════
# Models + pricing
# ════════════════════════════════════════════════════════════════════════

MODELS = {
    "Haiku 4.5 (cheapest)": "claude-haiku-4-5-20251001",
    "Sonnet 5": "claude-sonnet-5",
    "Opus 4.6": "claude-opus-4-6",
    "Opus 5 (most capable)": "claude-opus-5",
}

# Approximate costs per million tokens (input, output)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-opus-5": (15.0, 75.0),
}


def _track_cost(response, model_id, cost_tracker):
    """Add API call cost to the running total."""
    usage = response.usage
    in_cost, out_cost = MODEL_COSTS.get(model_id, (15.0, 75.0))
    cost = (usage.input_tokens * in_cost + usage.output_tokens * out_cost) / 1_000_000
    cost_tracker["total"] += cost
    cost_tracker["calls"] += 1
    cost_tracker["input_tokens"] += usage.input_tokens
    cost_tracker["output_tokens"] += usage.output_tokens
    return cost


# ════════════════════════════════════════════════════════════════════════
# Default prompts
# ════════════════════════════════════════════════════════════════════════

def _default_search_ranking_prompt(company_name, year):
    return (
        f"I'm looking for greenhouse gas emissions data (Scope 1, 2, 3) "
        f"from '{company_name}' for the year {year} (or covering {year}).\n\n"
        "Rank these from most to least likely to contain emissions data.\n\n"
        "SOURCE PRIORITY (strongly prefer in this order):\n"
        "1. The company's own website (investor relations, sustainability pages)\n"
        "2. Official regulatory filings (SEC EDGAR, Companies House, annual report PDFs)\n"
        "3. CDP disclosures, GRI reports hosted on official platforms\n"
        "4. Reputable ESG data providers\n"
        "AVOID: news articles, blog posts, third-party aggregators "
        "(GuruFocus, Macrotrends, etc.)\n\n"
        "LINK TYPE PRIORITY:\n"
        "1. Direct links to PDF documents (URLs ending in .pdf) — STRONGLY PREFER\n"
        "2. Specific report pages with downloadable content\n"
        "3. DEPRIORITISE: index/landing pages like 'Results, reports and presentations', "
        "'Investor relations', 'Document library' — these list reports but don't contain data\n\n"
        "Prefer PDF sustainability reports, annual reports, and ESG reports. "
        "Prefer reports from the parent/group company rather than subsidiaries. "
        "Include only results with a reasonable chance of containing the data."
    )


DEFAULT_TABLE_RANKING_SYSTEM = (
    "You are an expert at identifying greenhouse gas emissions data in tables. "
    "Given table previews, rank ALL tables from most to least likely to contain "
    "Scope 1, 2, or 3 emissions data. Assign each a relevance score 0-100. "
    "Every table must appear in the ranking."
)

DEFAULT_EXTRACTION_SYSTEM = (
    "You are an expert at extracting greenhouse gas emissions data from tables "
    "in sustainability reports. Extract Scope 1, Scope 2 (both location-based and "
    "market-based if available), and Scope 3 emissions for EVERY year present in the "
    "table — including prior-year comparison columns and historical trend data. "
    "Many reports show 2-5 years side by side; extract ALL of them, not just the "
    "most recent. "
    "Normalise all values to the same unit (prefer tonnes CO2e). "
    "If the table uses kt or Mt, convert to tonnes. "
    "IMPORTANT: Identify the reporting period for each year. Look for phrases like "
    "'year ended 31 December', 'for the 12 months to 31 March', 'calendar year', "
    "'FY2025' etc. Set period_start and period_end as YYYY-MM-DD dates. "
    "For example, 'year ended 31 March 2025' means period_start='2024-04-01', "
    "period_end='2025-03-31'. If not stated, set both to null. "
    "Note any methodology information, restatements, or caveats. "
    "If a scope is not present in the table, set its value to null. "
    "Be precise — extract the exact numbers from the table."
)

# Schemas for structured output
RANKED_RESULTS_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "title": {"type": "string"},
                },
                "required": ["url", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ranked"],
    "additionalProperties": False,
}

RANKED_TABLES_SCHEMA = {
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
}


# ════════════════════════════════════════════════════════════════════════
# Sidebar — parameters
# ════════════════════════════════════════════════════════════════════════

st.sidebar.title("🔬 Workbench")
st.sidebar.caption("Emissions extraction experiments")

# Company input
company_names = [c["name"] for c in FTSE_100]
company_input_mode = st.sidebar.radio(
    "Company input", ["FTSE 100 list", "Custom"], horizontal=True,
)
if company_input_mode == "FTSE 100 list":
    company_name = st.sidebar.selectbox("Company", company_names, index=0)
else:
    company_name = st.sidebar.text_input("Company name", "Aviva")

target_year = st.sidebar.number_input(
    "Target year", value=2024, min_value=2019, max_value=2026,
)

st.sidebar.markdown("---")

# Search
st.sidebar.subheader("Search")
num_exa_results = st.sidebar.slider("Exa search results", 5, 20, 10)
search_query_override = st.sidebar.text_area(
    "Search query (leave blank for auto)",
    value="",
    height=68,
    help="Auto-generated if blank. Edit to customise the Exa query.",
)

# Models
st.sidebar.subheader("Models")
ranking_model_name = st.sidebar.selectbox(
    "Ranking / table scoring", list(MODELS.keys()), index=0,
)
extraction_model_name = st.sidebar.selectbox(
    "Extraction", list(MODELS.keys()), index=0,
)
ranking_model = MODELS[ranking_model_name]
extraction_model = MODELS[extraction_model_name]

# Tables
st.sidebar.subheader("Tables")
num_tables = st.sidebar.slider("Max tables to extract from", 1, 20, 10)
min_table_score = st.sidebar.slider("Min table relevance score", 0, 100, 30)

# Prompts
st.sidebar.markdown("---")
st.sidebar.subheader("Prompts")

with st.sidebar.expander("Search ranking prompt"):
    search_ranking_prompt = st.text_area(
        "Prompt sent to Claude to rank Exa results",
        value=_default_search_ranking_prompt(company_name, target_year),
        height=200,
        key="search_prompt",
    )

with st.sidebar.expander("Table ranking system prompt"):
    table_ranking_prompt = st.text_area(
        "System prompt for table scoring",
        value=DEFAULT_TABLE_RANKING_SYSTEM,
        height=150,
        key="table_prompt",
    )

with st.sidebar.expander("Extraction system prompt"):
    extraction_prompt = st.text_area(
        "System prompt for data extraction",
        value=DEFAULT_EXTRACTION_SYSTEM,
        height=250,
        key="extraction_prompt",
    )

st.sidebar.markdown("---")
run_pipeline = st.sidebar.button(
    "▶ Run Pipeline", type="primary", use_container_width=True,
)


# ════════════════════════════════════════════════════════════════════════
# Main area
# ════════════════════════════════════════════════════════════════════════

st.title("Emissions Extraction Workbench")
st.caption(f"**{company_name}** — {target_year}  ·  "
           f"Ranking: {ranking_model_name}  ·  Extraction: {extraction_model_name}")

if not run_pipeline:
    # Landing page
    st.info("Configure parameters in the sidebar and click **▶ Run Pipeline** to start.")
    st.markdown("""
    ### How to use

    1. **Pick a company** and target year
    2. **Adjust parameters** — model, table count, search query, prompts
    3. **Click Run Pipeline** to execute each stage with full visibility
    4. **Review results** — see what was found, what was missed, and why
    5. **Tweak and re-run** — change any parameter and try again

    ### What to look for at each step

    | Step | Question |
    |------|----------|
    | **Search** | Did Exa find the right sustainability report? |
    | **Rank results** | Was the best document ranked #1? |
    | **Parse** | Did table extraction capture the emissions table? |
    | **Rank tables** | Did the emissions table score highest? |
    | **Extract** | Did Claude read the correct numbers? |
    """)
    st.stop()


# ── Run the pipeline ──────────────────────────────────────────────────

cost = {"total": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
pipeline_t0 = time.time()


# ── STEP 1: Search ────────────────────────────────────────────────────

st.header("1️⃣ Search")

search_query = search_query_override.strip() or (
    f"{company_name} greenhouse gas emissions scope 1 2 3 "
    f"{target_year} sustainability report ESG annual report"
)
st.code(search_query, language=None)

with st.spinner(f"Searching Exa for {num_exa_results} results…"):
    t0 = time.time()
    try:
        exa_response = exa.search(search_query, num_results=num_exa_results, type="auto")
        search_results = exa_response.results
        search_time = time.time() - t0
    except Exception as e:
        st.error(f"Search failed: {e}")
        st.stop()

st.success(f"Found **{len(search_results)}** results in {search_time:.1f}s")

for i, r in enumerate(search_results):
    is_pdf = r.url.lower().split("?")[0].endswith(".pdf")
    icon = "📄" if is_pdf else "🌐"
    st.write(f"{i + 1}. {icon} **{r.title or '(no title)'}**")
    st.caption(r.url)


# ── STEP 2: Rank search results ──────────────────────────────────────

st.header("2️⃣ Rank Search Results")

# Sort PDFs first (same as pipeline)
pdf_results = [r for r in search_results if r.url.lower().split("?")[0].endswith(".pdf")]
other_results = [r for r in search_results if not r.url.lower().split("?")[0].endswith(".pdf")]
sorted_results = pdf_results + other_results

results_text = "\n".join(
    f"{i + 1}. Title: {r.title or '(no title)'}\n   URL: {r.url}"
    for i, r in enumerate(sorted_results)
)

with st.expander("Ranking prompt sent to Claude"):
    st.text(search_ranking_prompt + "\n\nSearch results:\n" + results_text)

with st.spinner(f"Ranking with {ranking_model_name}…"):
    t0 = time.time()
    try:
        rank_resp = client.messages.create(
            model=ranking_model,
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": f"{search_ranking_prompt}\n\nSearch results:\n{results_text}",
            }],
            output_config={
                "format": {"type": "json_schema", "schema": RANKED_RESULTS_SCHEMA},
            },
        )
        _track_cost(rank_resp, ranking_model, cost)
        rank_text = next((b.text for b in rank_resp.content if b.type == "text"), None)
        ranked = json.loads(rank_text).get("ranked", []) if rank_text else []
        rank_time = time.time() - t0
    except Exception as e:
        st.error(f"Ranking failed: {e}")
        st.stop()

st.success(f"Ranked **{len(ranked)}** results in {rank_time:.1f}s")

for i, r in enumerate(ranked):
    medal = "🥇" if i == 0 else ("🥈" if i == 1 else ("🥉" if i == 2 else f"{i + 1}."))
    st.write(f"{medal} **{r['title']}**")
    st.caption(r["url"])

if not ranked:
    st.error("No results ranked — cannot proceed.")
    st.stop()

# Let user pick which result to parse
doc_options = [f"{i + 1}. {r['title']}" for i, r in enumerate(ranked)]
selected_doc_idx = st.selectbox(
    "Select document to parse (top-ranked by default)",
    range(len(doc_options)),
    format_func=lambda i: doc_options[i],
    key="doc_select",
)
top_url = ranked[selected_doc_idx]["url"]
top_title = ranked[selected_doc_idx]["title"]


# ── STEP 3: Parse document ───────────────────────────────────────────

st.header("3️⃣ Parse Document")

# Build candidate list: selected doc first, then remaining ranked docs in order
parse_candidates = [ranked[selected_doc_idx]] + [
    r for i, r in enumerate(ranked) if i != selected_doc_idx
]

table_dicts = []
tables_md = []
parse_time = 0.0
parsed_url = None
parsed_title = None

for cand_idx, candidate in enumerate(parse_candidates):
    cand_url = candidate["url"]
    cand_title = candidate["title"]
    source_type = detect_source_type(cand_url)

    if cand_idx == 0:
        st.write(f"Parsing: **{cand_title}**")
    else:
        st.info(f"⏩ Auto-fallback → trying candidate {cand_idx + 1}: **{cand_title}**")
    st.caption(f"{source_type.upper()} — {cand_url}")

    with st.spinner(f"Parsing {'(fallback) ' if cand_idx > 0 else ''}document…"):
        t0 = time.time()
        try:
            if source_type == "pdf":
                documents = parse_pdf(cand_url, LLAMA_KEY)
                table_dicts = extract_tables_from_documents(documents)
            elif source_type == "excel":
                table_dicts = parse_excel(cand_url)
            else:
                table_dicts = parse_html(cand_url)
            tables_md = [t["markdown"] for t in table_dicts]
            parse_time = time.time() - t0
            parsed_url = cand_url
            parsed_title = cand_title
            break  # success — stop trying
        except Exception as e:
            elapsed = time.time() - t0
            st.warning(f"⚠️ Failed in {elapsed:.1f}s: {e}")
            if cand_idx == len(parse_candidates) - 1:
                st.error("All candidates failed to parse.")
                st.stop()
            continue

# Update top_url/top_title for downstream steps
top_url = parsed_url
top_title = parsed_title

st.success(f"Found **{len(tables_md)}** tables in {parse_time:.1f}s")

if tables_md:
    for i, md in enumerate(tables_md):
        page = table_dicts[i].get("page_index", "?")
        with st.expander(f"Table {i}  ·  page {page}  ·  {len(md):,} chars"):
            st.code(md[:2000] + ("\n…" if len(md) > 2000 else ""), language="markdown")
else:
    st.warning("No tables found. The document may not have structured tables.")

    # Offer text fallback for HTML pages
    if source_type == "html":
        st.info("Trying text-based extraction as fallback…")
        page_text = extract_html_text(top_url)
        if page_text:
            tables_md = [page_text]
            table_dicts = [{"markdown": page_text}]
            st.success(f"Got {len(page_text):,} chars of page text")
        else:
            st.stop()
    else:
        st.stop()


# ── STEP 4: Rank tables ──────────────────────────────────────────────

st.header("4️⃣ Rank Tables")

tables_preview = "\n\n".join(
    f"TABLE {i}:\n{table[:600]}{'…' if len(table) > 600 else ''}"
    for i, table in enumerate(tables_md)
)

with st.expander("Table ranking prompt"):
    st.text(
        f"System: {table_ranking_prompt}\n\n"
        f"User: Find tables containing greenhouse gas emissions data…\n\n"
        f"(previews of {len(tables_md)} tables)"
    )

with st.spinner(f"Ranking {len(tables_md)} tables with {ranking_model_name}…"):
    t0 = time.time()
    try:
        table_rank_resp = client.messages.create(
            model=ranking_model,
            max_tokens=4096,
            system=table_ranking_prompt,
            messages=[{
                "role": "user",
                "content": (
                    "Find tables containing greenhouse gas emissions data "
                    "(Scope 1, Scope 2, Scope 3).\n\n"
                    f"Table previews:\n\n{tables_preview}"
                ),
            }],
            output_config={
                "format": {"type": "json_schema", "schema": RANKED_TABLES_SCHEMA},
            },
        )
        _track_cost(table_rank_resp, ranking_model, cost)
        tr_text = next((b.text for b in table_rank_resp.content if b.type == "text"), None)
        table_ranked = json.loads(tr_text).get("ranked", []) if tr_text else []
        table_rank_time = time.time() - t0
    except Exception as e:
        st.error(f"Table ranking failed: {e}")
        st.stop()

top_tables = [t for t in table_ranked if t["score"] >= min_table_score]
st.success(
    f"Ranked {len(table_ranked)} tables in {table_rank_time:.1f}s — "
    f"**{len(top_tables)}** above threshold ({min_table_score})"
)

for t in table_ranked:
    score = t["score"]
    idx = t["index"]
    bar_len = score // 5
    bar = "█" * bar_len + "░" * (20 - bar_len)
    selected = "✅" if score >= min_table_score else "❌"
    st.write(f"{selected} Table {idx}: **{score}** `{bar}`")

if not top_tables:
    st.warning(f"No tables scored ≥ {min_table_score}. Try lowering the threshold.")
    st.stop()


# ── STEP 5: Extract emissions ────────────────────────────────────────

st.header("5️⃣ Extract Emissions")

all_emissions = []
tables_to_try = top_tables[:num_tables]

st.write(f"Extracting from **{len(tables_to_try)}** tables with **{extraction_model_name}**")

for i, tbl in enumerate(tables_to_try):
    idx = tbl["index"]
    score = tbl["score"]

    if idx >= len(tables_md):
        st.warning(f"Table index {idx} out of range — skipping")
        continue

    table_md = tables_md[idx]

    st.subheader(f"Table {idx}  (relevance: {score})")

    with st.expander("Full table content"):
        st.code(table_md[:3000] + ("\n…" if len(table_md) > 3000 else ""), language="markdown")

    with st.spinner(f"Extracting with {extraction_model_name}…"):
        t0 = time.time()
        try:
            ext_resp = client.messages.create(
                model=extraction_model,
                max_tokens=8192,
                system=extraction_prompt,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Extract all greenhouse gas emissions data for "
                        f"{company_name} from this table:\n\n{table_md}"
                    ),
                }],
                output_config={
                    "format": {"type": "json_schema", "schema": EMISSIONS_SCHEMA},
                },
            )
            _track_cost(ext_resp, extraction_model, cost)
            ext_text = next((b.text for b in ext_resp.content if b.type == "text"), None)
            extraction = json.loads(ext_text) if ext_text else {}
            ext_time = time.time() - t0
        except Exception as e:
            st.error(f"Extraction failed: {e}")
            continue

    emissions = extraction.get("emissions", [])
    confidence = extraction.get("confidence_score", 0) or 0
    notes = extraction.get("methodology_notes", "")

    if emissions:
        if confidence >= 70:
            st.success(
                f"Confidence: **{confidence}/100** — "
                f"{len(emissions)} year(s) in {ext_time:.1f}s"
            )
        elif confidence >= 40:
            st.warning(
                f"Confidence: **{confidence}/100** — "
                f"{len(emissions)} year(s) in {ext_time:.1f}s"
            )
        else:
            st.error(
                f"Confidence: **{confidence}/100** — "
                f"{len(emissions)} year(s) in {ext_time:.1f}s"
            )

        df_data = []
        for e in emissions:
            df_data.append({
                "Year": e["reporting_year"],
                "Scope 1": e.get("scope_1"),
                "Scope 2 (loc)": e.get("scope_2_location"),
                "Scope 2 (mkt)": e.get("scope_2_market"),
                "Scope 3": e.get("scope_3"),
                "Unit": e.get("unit", ""),
                "Period": f"{e.get('period_start', '?')} → {e.get('period_end', '?')}",
            })
        st.dataframe(pd.DataFrame(df_data), use_container_width=True, hide_index=True)

        all_emissions.extend(emissions)
    else:
        st.info("No emissions data extracted from this table.")

    if notes:
        st.caption(f"📝 {notes}")

    with st.expander("Raw JSON response"):
        st.json(extraction)


# ── Summary ───────────────────────────────────────────────────────────

st.header("📊 Summary")

pipeline_time = time.time() - pipeline_t0

# Cost + timing
col_t, col_c = st.columns(2)
with col_t:
    st.metric("Total time", f"{pipeline_time:.0f}s")
with col_c:
    st.metric(
        "Estimated cost",
        f"${cost['total']:.4f}",
        help=f"{cost['calls']} API calls · "
             f"{cost['input_tokens']:,} in · {cost['output_tokens']:,} out",
    )

if all_emissions:
    # Deduplicate by year (keep first occurrence = highest-ranked table)
    seen_years = set()
    unique = []
    for e in all_emissions:
        yr = e["reporting_year"]
        if yr not in seen_years:
            unique.append(e)
            seen_years.add(yr)
    unique.sort(key=lambda x: x["reporting_year"])

    st.write(f"**Unique years found:** {sorted(seen_years)}")

    summary_data = []
    for e in unique:
        summary_data.append({
            "Year": e["reporting_year"],
            "Scope 1": e.get("scope_1"),
            "Scope 2 (loc)": e.get("scope_2_location"),
            "Scope 2 (mkt)": e.get("scope_2_market"),
            "Scope 3": e.get("scope_3"),
            "Unit": e.get("unit", ""),
        })
    st.dataframe(pd.DataFrame(summary_data), use_container_width=True, hide_index=True)

    # Highlight target year
    target_match = [e for e in unique if e["reporting_year"] == target_year]
    if target_match:
        e = target_match[0]
        st.success(f"✅ Target year **{target_year}** found:")
        tcol1, tcol2, tcol3, tcol4 = st.columns(4)
        tcol1.metric("Scope 1", f"{e.get('scope_1', '—'):,}" if e.get("scope_1") else "—")
        tcol2.metric("Scope 2 (loc)", f"{e.get('scope_2_location', '—'):,}" if e.get("scope_2_location") else "—")
        tcol3.metric("Scope 2 (mkt)", f"{e.get('scope_2_market', '—'):,}" if e.get("scope_2_market") else "—")
        tcol4.metric("Scope 3", f"{e.get('scope_3', '—'):,}" if e.get("scope_3") else "—")
    else:
        st.warning(
            f"⚠️ Target year **{target_year}** not found in extracted data. "
            "The document may not cover this year, or extraction missed it."
        )

    # Compare with database
    if db_session:
        st.subheader("Database comparison")
        try:
            db_records = (
                db_session.query(EmissionsRecord)
                .join(Company)
                .filter(Company.name == company_name)
                .order_by(EmissionsRecord.reporting_year)
                .all()
            )
            if db_records:
                db_data = []
                for r in db_records:
                    db_data.append({
                        "Year": r.reporting_year,
                        "DB Scope 1": r.scope_1,
                        "DB Scope 2 (loc)": r.scope_2_location,
                        "DB Scope 2 (mkt)": r.scope_2_market,
                        "DB Scope 3": r.scope_3,
                        "Confidence": r.confidence_score,
                    })
                st.dataframe(
                    pd.DataFrame(db_data), use_container_width=True, hide_index=True,
                )
            else:
                st.info(f"No existing records for '{company_name}' in the database.")
        except Exception as e:
            st.caption(f"Could not query database: {e}")

else:
    st.error("❌ No emissions data extracted from any table.")
    st.markdown("""
    **Possible causes:**
    - The document doesn't contain emissions data in structured tables
    - The table ranking didn't identify the right tables (check step 4)
    - The extraction prompt needs adjustment (try editing it in the sidebar)
    - Try a different/more capable extraction model
    """)
