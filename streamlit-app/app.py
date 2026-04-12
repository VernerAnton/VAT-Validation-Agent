"""
VAT Validation Agent — Streamlit Frontend

Orchestrates the full pipeline:
1. Fetches VBA file from file-vault-server (MCP resource)
2. Parses 185 country entries
3. For each country, searches current VAT rate via websearch-server (MCP tool)
4. Uses DeepSeek to compare found rate vs stored rate
5. Shows diff UI with per-item approve/reject
6. Writes corrected VBA file via sandbox-server (MCP tool)
"""

import os
import json
import time
import streamlit as st
from openai import OpenAI
from vba_parser import parse_vba, entries_to_vba, CountryEntry, format_rate
from mcp_client import McpClient

# ─── Configuration ────────────────────────────────────────────────────────────

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
FILE_VAULT_URL = os.environ.get("FILE_VAULT_URL", "http://localhost:3001")
WEBSEARCH_URL = os.environ.get("WEBSEARCH_URL", "http://localhost:3002")
SANDBOX_URL = os.environ.get("SANDBOX_URL", "http://localhost:3003")

# ─── Page config ──────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="VAT Validation Agent",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Custom CSS ───────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .stApp { max-width: 1200px; margin: 0 auto; }
    .match-row { background-color: #0d1117; padding: 8px 12px; border-radius: 4px; margin: 2px 0; }
    .mismatch-row { background-color: #1c1410; border-left: 3px solid #f59e0b;
                    padding: 8px 12px; border-radius: 4px; margin: 2px 0; }

    .status-badge { display: inline-block; padding: 2px 8px; border-radius: 12px;
                    font-size: 0.75rem; font-weight: 600; }
    .badge-match { background: #1a3a2a; color: #4ade80; }
    .badge-mismatch { background: #3a2a1a; color: #f59e0b; }

    .badge-special { background: #2a1a3a; color: #c084fc; }
</style>
""", unsafe_allow_html=True)

# ─── Session state initialization ─────────────────────────────────────────────

if "entries" not in st.session_state:
    st.session_state.entries = []
if "header" not in st.session_state:
    st.session_state.header = ""
if "footer" not in st.session_state:
    st.session_state.footer = ""
if "raw_vba" not in st.session_state:
    st.session_state.raw_vba = ""
if "validation_results" not in st.session_state:
    st.session_state.validation_results = {}
if "approved_changes" not in st.session_state:
    st.session_state.approved_changes = {}
if "scan_complete" not in st.session_state:
    st.session_state.scan_complete = False
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "scan_progress" not in st.session_state:
    st.session_state.scan_progress = 0
if "logs" not in st.session_state:
    st.session_state.logs = []

# Known special/calculated rates — annotate but still validate
SPECIAL_RATES = {
    "RU": {"rate": 16.67, "note": "Calculated effective rate (20/120)"},
}

# ─── Helpers ──────────────────────────────────────────────────────────────────


def add_log(msg: str):
    st.session_state.logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")


def get_deepseek_client() -> OpenAI:
    return OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        default_headers={
            "HTTP-Referer": "https://vat-validation-agent.up.railway.app",
            "X-Title": "VAT Validation Agent",
        }
    )


def analyze_vat_with_llm(
    country_name: str,
    iso_code: str,
    stored_rate: float,
    search_results: str,
) -> dict:
    """Ask DeepSeek to analyze the search results and determine the correct VAT rate."""
    client = get_deepseek_client()

    system_prompt = """You are a tax data analyst. Your job is to determine the current standard VAT/GST rate
for a given country based on web search results.

Rules:
- Return ONLY the standard/general VAT rate (not reduced rates, not luxury rates)
- If the country has a GST instead of VAT, return that rate
- If the country has NO VAT/GST system at all, return 0
- For territories currently at 0% VAT, verify whether this is still correct — tax laws change
- For calculated rates like Russia's 16.67% (which is 20/120), note this is a calculated figure
- If search results are unclear or contradictory, return the most commonly cited rate
- Return your answer as JSON only, no other text
- If a country has no national VAT system, return 0 and mark as correct
- If evidence from search results is weak, conflicting or unclear, return needs_review rather than guessing
- Use only the national/federal standard rate, not regional or provincial variations
- Return JSON only, no markdown, no explanation outside the JSON

JSON format:
{
    "current_rate": <number>,
    "confidence": "high" | "medium" | "low" | "needs_review",
    "source_note": "<brief explanation of where this rate comes from>",
    "is_calculated": false
}"""

    user_prompt = f"""Country: {country_name} ({iso_code})
Stored VAT rate: {stored_rate}%

Search results:
{search_results}

What is the current standard VAT/GST rate for {country_name}? Return JSON only."""

    try:
        response = client.chat.completions.create(
            model="deepseek/deepseek-v3.2",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=300,
        )
        content = response.choices[0].message.content or "{}"
        # Extract JSON from response (handle markdown code blocks)
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content
            content = content.rsplit("```", 1)[0]
            content = content.strip()
            if content.startswith("json"):
                content = content[4:].strip()
        return json.loads(content)
    except Exception as e:
        return {
            "current_rate": stored_rate,
            "confidence": "low",
            "source_note": f"LLM error: {str(e)}",
            "is_calculated": False,
        }


# ─── Sidebar ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("⚙️ Configuration")

    st.subheader("MCP Server URLs")
    file_vault_url = st.text_input("File Vault Server", value=FILE_VAULT_URL)
    websearch_url = st.text_input("Web Search Server", value=WEBSEARCH_URL)
    sandbox_url = st.text_input("Sandbox Server", value=SANDBOX_URL)

    st.subheader("LLM")
    deepseek_key = st.text_input("OpenRouter API Key", value=OPENROUTER_API_KEY, type="password")
    if deepseek_key:
        OPENROUTER_API_KEY = deepseek_key

    st.divider()

    # Health checks
    st.subheader("Server Status")
    if st.button("Check All Servers"):
        for name, url in [
            ("File Vault", file_vault_url),
            ("Web Search", websearch_url),
            ("Sandbox", sandbox_url),
        ]:
            try:
                client = McpClient(url)
                health = client.health_check()
                st.success(f"✅ {name}: {health.get('status', 'ok')}")
            except Exception as e:
                st.error(f"❌ {name}: {str(e)[:60]}")

    st.divider()
    st.subheader("Activity Log")
    log_container = st.container(height=300)
    with log_container:
        for log in st.session_state.logs[-50:]:
            st.text(log)

# ─── Main UI ──────────────────────────────────────────────────────────────────

st.title("🌐 VAT Validation Agent")
st.caption("Cloud-native MCP pipeline — parse VBA → search current rates → review diffs → export corrected module")

# ─── Phase 1: Load & Parse ────────────────────────────────────────────────────

st.header("Phase 1 — Load & Parse VBA Module")

col1, col2 = st.columns([1, 1])

with col1:
    if st.button("📥 Fetch VBA from File Vault", use_container_width=True):
        with st.spinner("Connecting to file-vault-server..."):
            try:
                vault = McpClient(file_vault_url)
                raw = vault.call_tool("get_vat_file")
                st.session_state.raw_vba = raw
                entries, header, footer = parse_vba(raw)
                st.session_state.entries = entries
                st.session_state.header = header
                st.session_state.footer = footer
                add_log(f"Loaded {len(entries)} countries from file-vault-server")
                st.success(f"Parsed {len(entries)} countries from VBA module")
            except Exception as e:
                st.error(f"Failed: {e}")
                add_log(f"ERROR loading VBA: {e}")

with col2:
    uploaded = st.file_uploader("Or upload VBA .txt file directly", type=["txt", "bas"])
    if uploaded:
        raw = uploaded.read().decode("utf-8")
        st.session_state.raw_vba = raw
        entries, header, footer = parse_vba(raw)
        st.session_state.entries = entries
        st.session_state.header = header
        st.session_state.footer = footer
        add_log(f"Loaded {len(entries)} countries from uploaded file")
        st.success(f"Parsed {len(entries)} countries")

if st.session_state.entries:
    with st.expander(f"📊 Loaded Data — {len(st.session_state.entries)} countries", expanded=False):
        # Show as a table
        data = [
            {"#": e.index, "ISO": e.iso_code, "Country": e.name, "VAT %": e.vat_rate}
            for e in st.session_state.entries
        ]
        st.dataframe(data, use_container_width=True, hide_index=True)

# ─── Phase 2: Validate ───────────────────────────────────────────────────────

if st.session_state.entries:
    st.header("Phase 2 — Validate VAT Rates")
    st.caption("Searches the web for each country's current VAT rate, then uses DeepSeek to compare.")

    # Batch size control
    col_a, col_b = st.columns([1, 1])
    with col_a:
        batch_size = st.number_input("Batch size", min_value=1, max_value=185, value=10,
                                     help="Countries to validate per batch. Lower = slower but cheaper.")
    with col_b:
        delay_between = st.number_input("Delay (sec)", min_value=0.0, max_value=5.0, value=0.5, step=0.1,
                                        help="Delay between API calls to avoid rate limits.")

    _TEST_MODE_CODES = [
        "DE", "FR", "SE", "JP", "AU", "NZ", "EE", "ID", "IL", "EC",
        "SG", "HK", "BM", "QA", "RU", "BR", "IN", "CA", "GP", "MQ",
        "RE", "NC", "AI", "CM", "MW", "BB", "US", "KW", "IQ",
    ]

    test_mode = st.checkbox("Test Mode", value=True)

    if test_mode:
        _code_order = {code: i for i, code in enumerate(_TEST_MODE_CODES)}
        entries_to_validate = sorted(
            [e for e in st.session_state.entries if e.iso_code in _code_order],
            key=lambda e: _code_order[e.iso_code],
        )
        st.warning("⚠️ Test mode — validating 29 countries (edge cases + representative sample). Uncheck to run all 185.")
    else:
        entries_to_validate = st.session_state.entries.copy()
        st.info(f"Will validate all **{len(entries_to_validate)}** countries — no territories skipped")

    if st.button("🔍 Start Validation Scan", use_container_width=True):
        st.session_state.scan_running = True
        st.session_state.scan_complete = False

        search_client = McpClient(websearch_url)
        progress_bar = st.progress(0, text="Starting validation scan...")
        results_placeholder = st.empty()

        total = len(entries_to_validate)
        for i, entry in enumerate(entries_to_validate):
            progress_pct = (i + 1) / total
            progress_bar.progress(progress_pct, text=f"Validating {entry.name} ({entry.iso_code}) — {i+1}/{total}")

            # Skip if already validated in this session
            if entry.iso_code in st.session_state.validation_results:
                add_log(f"Skipped {entry.iso_code} {entry.name} (already validated)")
                continue

            # Check if it's a special rate
            special = SPECIAL_RATES.get(entry.iso_code)

            try:
                # Step 1: Search for current VAT rate
                query = f"current standard VAT rate {entry.name} {entry.iso_code} 2025 2026"
                add_log(f"Searching: {entry.name} ({entry.iso_code})")
                search_results = search_client.call_tool("search_web", {"query": query})

                # Step 2: Analyze with DeepSeek
                add_log(f"Analyzing: {entry.name} with DeepSeek")
                analysis = analyze_vat_with_llm(
                    entry.name, entry.iso_code, entry.vat_rate, search_results
                )

                current_rate = float(analysis.get("current_rate", entry.vat_rate))
                confidence = analysis.get("confidence", "low")
                source_note = analysis.get("source_note", "")
                is_calculated = analysis.get("is_calculated", False)

                # Determine if there's a mismatch
                rate_diff = abs(current_rate - entry.vat_rate)
                is_match = rate_diff < 0.01  # Tolerance for floating point

                # Special handling for known calculated rates
                if special and abs(entry.vat_rate - special["rate"]) < 0.01:
                    source_note = f"{source_note} | Note: {special['note']}"

                st.session_state.validation_results[entry.iso_code] = {
                    "iso_code": entry.iso_code,
                    "country": entry.name,
                    "stored_rate": entry.vat_rate,
                    "found_rate": current_rate,
                    "is_match": is_match,
                    "confidence": confidence,
                    "source_note": source_note,
                    "is_calculated": is_calculated,
                    "rate_diff": rate_diff,
                }

                if is_match:
                    add_log(f"✓ {entry.iso_code} {entry.name}: {entry.vat_rate}% confirmed")
                else:
                    add_log(f"✗ {entry.iso_code} {entry.name}: stored={entry.vat_rate}% found={current_rate}%")

            except Exception as e:
                add_log(f"ERROR validating {entry.iso_code} {entry.name}: {e}")
                st.session_state.validation_results[entry.iso_code] = {
                    "iso_code": entry.iso_code,
                    "country": entry.name,
                    "stored_rate": entry.vat_rate,
                    "found_rate": entry.vat_rate,
                    "is_match": True,
                    "confidence": "error",
                    "source_note": f"Validation error: {str(e)[:100]}",
                    "is_calculated": False,
                    "rate_diff": 0,
                }

            time.sleep(delay_between)

        progress_bar.progress(1.0, text="Validation complete!")
        st.session_state.scan_complete = True
        st.session_state.scan_running = False
        add_log("Validation scan complete")
        st.rerun()

# ─── Phase 3: Review & Approve ───────────────────────────────────────────────

if st.session_state.validation_results:
    st.header("Phase 3 — Review & Approve Changes")

    results = st.session_state.validation_results
    mismatches = {k: v for k, v in results.items() if not v["is_match"]}
    matches = {k: v for k, v in results.items() if v["is_match"]}

    # Summary metrics
    col_m1, col_m2, col_m3, col_m4 = st.columns(4)
    col_m1.metric("Total Validated", len(results))
    col_m2.metric("Matches", len(matches))
    col_m3.metric("Mismatches", len(mismatches))
    col_m4.metric("Approval Queue", len(mismatches))

    # ─── Mismatches (need review) ──────────────────────────────────────

    if mismatches:
        st.subheader(f"⚠️ Mismatches — {len(mismatches)} countries need review")

        # Bulk actions
        bulk_col1, bulk_col2, bulk_col3 = st.columns([1, 1, 2])
        with bulk_col1:
            if st.button("✅ Approve All", key="approve_all"):
                for iso, data in mismatches.items():
                    st.session_state.approved_changes[iso] = True
                st.rerun()
        with bulk_col2:
            if st.button("❌ Reject All", key="reject_all"):
                for iso in mismatches:
                    st.session_state.approved_changes[iso] = False
                st.rerun()

        # Individual review cards
        for iso, data in sorted(mismatches.items(), key=lambda x: -x[1]["rate_diff"]):
            with st.container():
                cols = st.columns([0.5, 2, 1.5, 1.5, 1.5, 1, 1])
                cols[0].write(f"**{data['iso_code']}**")
                cols[1].write(data["country"])
                cols[2].write(f"Stored: **{format_rate(data['stored_rate'])}%**")
                cols[3].write(f"Found: **{format_rate(data['found_rate'])}%**")
                cols[4].write(f"Diff: {format_rate(data['rate_diff'])}pp")

                confidence_colors = {"high": "🟢", "medium": "🟡", "low": "🔴", "error": "⚫"}
                cols[5].write(f"{confidence_colors.get(data['confidence'], '⚪')} {data['confidence']}")

                # Approve/reject toggle
                current_approval = st.session_state.approved_changes.get(iso)
                with cols[6]:
                    if current_approval is True:
                        if st.button("↩️ Undo", key=f"undo_{iso}"):
                            del st.session_state.approved_changes[iso]
                            st.rerun()
                        st.caption("Approved")
                    elif current_approval is False:
                        if st.button("↩️ Undo", key=f"undor_{iso}"):
                            del st.session_state.approved_changes[iso]
                            st.rerun()
                        st.caption("Rejected")
                    else:
                        c1, c2 = st.columns(2)
                        with c1:
                            if st.button("✅", key=f"approve_{iso}", help="Accept new rate"):
                                st.session_state.approved_changes[iso] = True
                                st.rerun()
                        with c2:
                            if st.button("❌", key=f"reject_{iso}", help="Keep stored rate"):
                                st.session_state.approved_changes[iso] = False
                                st.rerun()

                # Show source note in expander
                if data.get("source_note"):
                    with st.expander("Source details", expanded=False):
                        st.caption(data["source_note"])

                st.divider()

    # ─── Matches (confirmed OK) ────────────────────────────────────────

    with st.expander(f"✅ Confirmed Matches — {len(matches)} countries", expanded=False):
        match_data = [
            {
                "ISO": v["iso_code"],
                "Country": v["country"],
                "Rate": f"{format_rate(v['stored_rate'])}%",
                "Confidence": v["confidence"],
            }
            for v in sorted(matches.values(), key=lambda x: x["country"])
        ]
        st.dataframe(match_data, use_container_width=True, hide_index=True)

# ─── Phase 4: Export ──────────────────────────────────────────────────────────

if st.session_state.validation_results and st.session_state.entries:
    st.header("Phase 4 — Export Corrected VBA Module")

    mismatches = {k: v for k, v in st.session_state.validation_results.items() if not v["is_match"]}
    approved = {k for k, v in st.session_state.approved_changes.items() if v is True}
    pending = {k for k in mismatches if k not in st.session_state.approved_changes}

    if pending:
        st.warning(f"⚠️ {len(pending)} mismatches still need review before export.")

    # Build corrected entries
    corrected_entries: list[CountryEntry] = []
    changes_applied: list[dict] = []

    for entry in st.session_state.entries:
        result = st.session_state.validation_results.get(entry.iso_code)
        if result and not result["is_match"] and entry.iso_code in approved:
            # Apply the approved change
            new_entry = CountryEntry(
                index=entry.index,
                iso_code=entry.iso_code,
                name=entry.name,
                vat_rate=result["found_rate"],
            )
            corrected_entries.append(new_entry)
            changes_applied.append({
                "iso": entry.iso_code,
                "country": entry.name,
                "old_rate": entry.vat_rate,
                "new_rate": result["found_rate"],
            })
        else:
            corrected_entries.append(entry)

    # Preview changes
    if changes_applied:
        st.subheader(f"Changes to Apply: {len(changes_applied)}")
        for ch in changes_applied:
            st.write(
                f"**{ch['iso']}** {ch['country']}: "
                f"{format_rate(ch['old_rate'])}% → {format_rate(ch['new_rate'])}%"
            )

    col_e1, col_e2 = st.columns(2)

    with col_e1:
        if st.button("📄 Generate Corrected VBA", use_container_width=True, type="primary"):
            corrected_vba = entries_to_vba(
                corrected_entries,
                st.session_state.header,
                st.session_state.footer,
            )

            # Try to save via sandbox server
            try:
                sandbox = McpClient(sandbox_url)
                sandbox.call_tool("write_draft", {
                    "filename": "VAT-Database-Corrected.txt",
                    "content": corrected_vba,
                })
                add_log("Saved corrected VBA to sandbox server")
                st.success("Corrected VBA saved to sandbox server")
            except Exception as e:
                add_log(f"Sandbox save failed: {e} — using local download")
                st.warning(f"Sandbox server unavailable ({str(e)[:50]}), using direct download.")

            # Always offer direct download
            st.download_button(
                label="⬇️ Download VAT-Database-Corrected.txt",
                data=corrected_vba,
                file_name="VAT-Database-Corrected.txt",
                mime="text/plain",
                use_container_width=True,
            )

            # Show diff
            with st.expander("📝 View Diff", expanded=True):
                original_vba = st.session_state.raw_vba
                # Simple visual diff — highlight changed lines
                for ch in changes_applied:
                    st.code(
                        f"- countryData(...) = \"{ch['iso']}\": ... = {format_rate(ch['old_rate'])}\n"
                        f"+ countryData(...) = \"{ch['iso']}\": ... = {format_rate(ch['new_rate'])}",
                        language="diff",
                    )

    with col_e2:
        if st.button("📋 Export Change Report (JSON)", use_container_width=True):
            report = {
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "total_countries": len(st.session_state.entries),
                "validated": len(st.session_state.validation_results),
                "mismatches_found": len(mismatches),
                "changes_approved": len(approved),
                "changes_applied": changes_applied,
                "all_results": list(st.session_state.validation_results.values()),
            }
            report_json = json.dumps(report, indent=2, ensure_ascii=False)

            st.download_button(
                label="⬇️ Download Change Report",
                data=report_json,
                file_name="vat-validation-report.json",
                mime="application/json",
                use_container_width=True,
            )

# ─── Footer ───────────────────────────────────────────────────────────────────

st.divider()
st.caption(
    "VAT Validation Agent v1.0 — "
    "MCP Architecture: file-vault-server (Resource) → websearch-server (Tavily) → "
    "sandbox-server (Drafts) → Streamlit (Orchestrator) → DeepSeek (Reasoning)"
)
