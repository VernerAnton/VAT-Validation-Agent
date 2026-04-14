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
import re
import json
import time
import traceback
import urllib.parse
from datetime import datetime
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
if "raw_llm_responses" not in st.session_state:
    st.session_state.raw_llm_responses = {}
if "current_log_file" not in st.session_state:
    st.session_state.current_log_file = ""
if "scan_log_buffer" not in st.session_state:
    st.session_state.scan_log_buffer = ""

# Known special/calculated rates — annotate but still validate
SPECIAL_RATES = {
    "RU": {"rate": 16.67, "note": "Calculated effective rate (20/120)"},
}

# Per-country search metadata: official tax authority domain and local VAT term
COUNTRY_META: dict[str, dict] = {
    "DE": {"domain": "bzst.de",                "vat_term": "Mehrwertsteuer"},
    "FR": {"domain": "impots.gouv.fr",          "vat_term": "TVA"},
    "SE": {"domain": "skatteverket.se",         "vat_term": "moms"},
    "JP": {"domain": "nta.go.jp",               "vat_term": "消費税"},
    "AU": {"domain": "ato.gov.au",              "vat_term": "GST"},
    "NZ": {"domain": "ird.govt.nz",             "vat_term": "GST"},
    "EE": {"domain": "emta.ee",                 "vat_term": "käibemaks"},
    "ID": {"domain": "pajak.go.id",             "vat_term": "PPN"},
    "IL": {"domain": "taxes.gov.il",            "vat_term": "מע\"מ"},
    "EC": {"domain": "sri.gob.ec",              "vat_term": "IVA"},
    "SG": {"domain": "iras.gov.sg",             "vat_term": "GST"},
    "HK": {"domain": "ird.gov.hk",              "vat_term": "GST"},
    "BM": {"domain": "gov.bm",                  "vat_term": "VAT"},
    "QA": {"domain": "dhf.gov.qa",              "vat_term": "VAT"},
    "RU": {"domain": "nalog.gov.ru",            "vat_term": "НДС"},
    "BR": {"domain": "receita.fazenda.gov.br",  "vat_term": "IVA"},
    "IN": {"domain": "cbic.gov.in",             "vat_term": "GST"},
    "CA": {"domain": "canada.ca",               "vat_term": "GST"},
    "GP": {"domain": "impots.gouv.fr",          "vat_term": "TVA"},
    "MQ": {"domain": "impots.gouv.fr",          "vat_term": "TVA"},
    "RE": {"domain": "impots.gouv.fr",          "vat_term": "TVA"},
    "NC": {"domain": "gouv.nc",                 "vat_term": "TVA"},
    "AI": {"domain": "gov.ai",                  "vat_term": "VAT"},
    "CM": {"domain": "dgi.cm",                  "vat_term": "TVA"},
    "MW": {"domain": "mra.mw",                  "vat_term": "VAT"},
    "BB": {"domain": "bra.gov.bb",              "vat_term": "VAT"},
    "US": {"domain": "irs.gov",                 "vat_term": "sales tax"},
    "KW": {"domain": "mof.gov.kw",              "vat_term": "VAT"},
    "IQ": {"domain": "mof.gov.iq",              "vat_term": "VAT"},
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


# ─── Source-tier weighting ─────────────────────────────────────────────────────

_SOURCE_TIERS: list[tuple[float, tuple]] = [
    (1.00, ("gov.", ".gov", "government")),
    (0.95, ("ec.europa.eu", "oecd.org")),
    (0.90, ("taxsummaries.pwc.com", "ey.com", "deloitte.com", "kpmg.com")),
    (0.85, ("taxfoundation.org", "ibfd.org")),
    (0.70, ("reuters.com", "bloomberg.com")),
]
_BLOG_WEIGHT = 0.2


def _tier_weight(url: str) -> float:
    """Return the credibility weight (0.2–1.0) for a given source URL."""
    lower = url.lower()
    for weight, keywords in _SOURCE_TIERS:
        if any(k in lower for k in keywords):
            return weight
    return _BLOG_WEIGHT


def _has_circular_sourcing(search_results: str) -> bool:
    """Return True if the combined search results lack adequate source diversity."""
    urls = re.findall(r'URL:\s*(https?://\S+)', search_results)
    if len(urls) < 2:
        return True

    domains: set[str] = set()
    for u in urls:
        try:
            host = urllib.parse.urlparse(u).hostname or ""
            parts = host.split(".")
            domains.add(".".join(parts[-2:]) if len(parts) >= 2 else host)
        except Exception:
            pass
    if len(domains) < 2:
        return True

    has_authoritative = any(_tier_weight(u) >= 0.9 for u in urls)
    has_secondary = any(0.7 <= _tier_weight(u) < 0.9 for u in urls)
    return not (has_authoritative or has_secondary)


def _search_country(
    search_client: McpClient,
    entry: CountryEntry,
    year: str,
    log_fn=None,
) -> str:
    """Run three targeted queries for a country and return combined results."""
    meta = COUNTRY_META.get(entry.iso_code)
    if meta:
        q_a = f"{entry.name} standard VAT rate {year} site:{meta['domain']}"
        q_b = f"{entry.name} VAT rate {year} site:taxsummaries.pwc.com"
        q_c = f"{entry.name} {meta['vat_term']} {year}"
    else:
        q_a = f"{entry.name} standard VAT GST rate {year}"
        q_b = f"{entry.name} VAT rate {year} site:taxsummaries.pwc.com"
        q_c = f"{entry.name} ({entry.iso_code}) VAT tax rate official {year}"

    parts: list[str] = []
    for label, q in [
        ("Official source", q_a),
        ("PwC Tax Summaries", q_b),
        ("Local language / general", q_c),
    ]:
        if log_fn:
            log_fn(f"QUERY | {entry.iso_code} | {label} | {q}")
        try:
            result = search_client.call_tool("search_web", {"query": q})
            if log_fn:
                _n_urls = len(re.findall(r'URL:\s*(https?://\S+)', result))
                log_fn(f"RESULT | {entry.iso_code} | {label} | urls={_n_urls} | len={len(result)}")
            parts.append(f"[{label} — query: {q}]\n{result}")
        except Exception as e:
            if log_fn:
                log_fn(f"QUERY FAILED | {entry.iso_code} | {label} | {e}")
            parts.append(f"[{label} — FAILED: {e}]")
    return "\n\n".join(parts)


def _sandbox_log(sandbox_client: McpClient, msg: str) -> None:
    """Append a timestamped entry to the in-memory buffer and sync to sandbox."""
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    st.session_state.scan_log_buffer += f"[{ts}] {msg}\n"
    try:
        sandbox_client.call_tool("write_draft", {
            "filename": st.session_state.current_log_file,
            "content": st.session_state.scan_log_buffer,
        })
    except Exception:
        pass  # Never let log I/O break the scan


def analyze_vat_with_llm(
    country_name: str,
    iso_code: str,
    stored_rate: float,
    search_results: str,
    today: str,
) -> tuple[dict, str]:
    """Ask the LLM to analyze search results and return (parsed_dict, raw_json_string)."""
    client = get_deepseek_client()

    # Build source-tier annotation for the user prompt
    urls = re.findall(r'URL:\s*(https?://\S+)', search_results)
    tier_lines = [f"  {u} → weight {_tier_weight(u):.2f}" for u in urls[:10]]
    tier_note = ("Source credibility weights:\n" + "\n".join(tier_lines)) if tier_lines else ""

    system_prompt = f"""Today is {today}. You are a tax data analyst determining the current standard VAT/GST rate for a country.

Rules:
- Use only the national/federal standard rate, not regional or provincial variations
- If a country has no national VAT system, set standard_rate to 0 and needs_human_review to false
- If evidence is weak, conflicting or unclear, set needs_human_review to true instead of guessing
- If a country has GST instead of VAT, use that rate

Return ONLY valid JSON in this exact structure, no markdown, no extra text:

{{
  "sources_analyzed": [
    {{
      "url": "https://example.com",
      "claimed_rate": 20.0,
      "direct_quote": "the standard VAT rate is 20 percent",
      "source_type": "government",
      "publication_date": "2026-01-01"
    }}
  ],
  "source_agreement": "all_agree",
  "temporal_notes": "no recent changes found",
  "reasoning": "Multiple authoritative sources confirm the rate",
  "standard_rate": 20.0,
  "confidence_score": 0.95,
  "needs_human_review": false,
  "review_reason": null
}}

Valid values for source_type: government, accounting_firm, supranational, news, blog
Valid values for source_agreement: all_agree, majority_agree, conflicting, insufficient
confidence_score must be a number between 0.0 and 1.0
If a field has no value use null, never use empty string for optional fields"""

    user_prompt = f"""Today's date: {today}
Country: {country_name} ({iso_code})
Stored VAT rate: {stored_rate}%

{tier_note}

Search results:
{search_results}

Determine the current standard VAT/GST rate for {country_name}. Return JSON only."""

    raw_content = ""
    try:
        response = client.chat.completions.create(
            model="qwen/qwen3.6-plus",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=1500,
        )
        raw_content = response.choices[0].message.content or "{}"
    except Exception as e:
        return {
            "sources_analyzed": [],
            "source_agreement": "conflicting",
            "temporal_notes": "",
            "reasoning": f"API call failed: {str(e)}",
            "standard_rate": stored_rate,
            "confidence_score": 0.0,
            "needs_human_review": True,
            "review_reason": f"API call failed: {str(e)[:100]}",
        }, raw_content

    # Strip markdown code fences if present
    content = raw_content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        content = content.rsplit("```", 1)[0].strip()
        if content.startswith("json"):
            content = content[4:].strip()

    try:
        parsed = json.loads(content)
        return parsed, raw_content
    except json.JSONDecodeError as e:
        return {
            "sources_analyzed": [],
            "source_agreement": "conflicting",
            "temporal_notes": "",
            "reasoning": f"JSON parse error: {str(e)} — raw response: {content[:300]}",
            "standard_rate": stored_rate,
            "confidence_score": 0.0,
            "needs_human_review": True,
            "review_reason": f"JSON parse failed: {str(e)[:100]}",
        }, raw_content


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

    st.divider()
    st.subheader("📋 Scan Logs")
    if st.session_state.current_log_file:
        st.caption(f"Current: `{st.session_state.current_log_file}`")

    if st.button("📋 View Scan Logs", key="view_logs_btn"):
        try:
            _sb = McpClient(sandbox_url)
            raw_list = _sb.call_tool("list_drafts", {})
            all_files = [ln.strip() for ln in raw_list.splitlines() if ln.strip()]
            log_files = sorted(
                [f for f in all_files if f.startswith("scan_log_")],
                reverse=True,
            )
            st.session_state._log_file_list = log_files
        except Exception as e:
            st.error(f"Failed to list logs: {str(e)[:80]}")

    _log_files = st.session_state.get("_log_file_list", [])
    if _log_files:
        _selected = st.selectbox("Select log", _log_files, key="log_file_select")
        if st.button("Load log", key="load_log_btn"):
            try:
                _sb = McpClient(sandbox_url)
                _content = _sb.call_tool("read_draft", {"filename": _selected})
                st.session_state._loaded_log_content = _content
                st.session_state._loaded_log_name = _selected
            except Exception as e:
                st.error(f"Failed to load: {str(e)[:80]}")
        if st.session_state.get("_loaded_log_content"):
            st.caption(st.session_state.get("_loaded_log_name", ""))
            st.code(st.session_state._loaded_log_content, language="text")
    elif st.session_state.get("_log_file_list") is not None:
        st.info("No scan log files found on sandbox server.")

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

    test_mode = st.checkbox("Test Mode", value=True, key="test_mode")

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

        # ── Persistent log setup ──────────────────────────────────────────
        _log_ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        st.session_state.current_log_file = f"scan_log_{_log_ts}.txt"
        st.session_state.scan_log_buffer = ""
        _log_sb = McpClient(sandbox_url)
        _slog = lambda msg: _sandbox_log(_log_sb, msg)

        _today = datetime.now().strftime("%d %B %Y")
        _year = datetime.now().strftime("%Y")
        total = len(entries_to_validate)
        _slog(
            f"SCAN STARTED | countries={total}"
            f" | test_mode={st.session_state.get('test_mode', False)}"
            f" | model=qwen/qwen3.6-plus:free"
        )
        for i, entry in enumerate(entries_to_validate):
            progress_pct = (i + 1) / total
            progress_bar.progress(progress_pct, text=f"Validating {entry.name} ({entry.iso_code}) — {i+1}/{total}")

            # Skip if already validated in this session
            if entry.iso_code in st.session_state.validation_results:
                add_log(f"Skipped {entry.iso_code} {entry.name} (already validated)")
                continue

            _slog(f"COUNTRY START | {entry.iso_code} | {entry.name}")

            # Check if it's a special rate
            special = SPECIAL_RATES.get(entry.iso_code)

            try:
                # Step 1: Three targeted searches per country
                add_log(f"Searching (3 queries): {entry.name} ({entry.iso_code})")
                search_results = _search_country(search_client, entry, _year, log_fn=_slog)
                circular = _has_circular_sourcing(search_results)

                # Step 2: Analyze with LLM (reasoning-first schema)
                add_log(f"Analyzing: {entry.name} with LLM")
                _slog(
                    f"LLM CALL | {entry.iso_code}"
                    f" | model=qwen/qwen3.6-plus:free"
                    f" | search_len={len(search_results)}"
                )
                analysis, raw_response = analyze_vat_with_llm(
                    entry.name, entry.iso_code, entry.vat_rate, search_results, _today
                )
                st.session_state.raw_llm_responses[entry.iso_code] = raw_response
                _parse_ok = bool(raw_response) and not analysis.get("reasoning", "").startswith(
                    ("JSON parse error", "API call failed")
                )
                _slog(
                    f"LLM RESPONSE | {entry.iso_code}"
                    f" | raw_len={len(raw_response)}"
                    f" | parsed={'OK' if _parse_ok else 'ERROR'}"
                )

                current_rate = float(analysis.get("standard_rate", entry.vat_rate))
                confidence_score = float(analysis.get("confidence_score", 0.0))
                source_agreement = analysis.get("source_agreement", "conflicting")
                needs_review = bool(analysis.get("needs_human_review", False))
                review_reason = analysis.get("review_reason", "")

                # Auto-escalate overrides
                if source_agreement == "conflicting":
                    needs_review = True
                    review_reason = review_reason or "Sources contradict each other"
                if circular:
                    needs_review = True
                    review_reason = review_reason or "Circular or insufficient sourcing detected"
                if confidence_score < 0.5:
                    needs_review = True
                    review_reason = review_reason or f"Confidence too low ({confidence_score:.2f})"

                # Map score to tier and backward-compat confidence string
                if confidence_score >= 0.9:
                    confidence_tier = "auto_accept"
                elif confidence_score >= 0.7:
                    confidence_tier = "log_audit"
                elif confidence_score >= 0.5:
                    confidence_tier = "human_review"
                else:
                    confidence_tier = "escalate"

                confidence_str = {
                    "auto_accept": "high",
                    "log_audit": "medium",
                    "human_review": "low",
                    "escalate": "low",
                }[confidence_tier]

                # Determine rate mismatch
                rate_diff = abs(current_rate - entry.vat_rate)
                is_match = rate_diff < 0.01

                # Annotate known calculated rates
                reasoning = analysis.get("reasoning", "")
                if special and abs(entry.vat_rate - special["rate"]) < 0.01:
                    reasoning = f"{reasoning} | Note: {special['note']}"

                st.session_state.validation_results[entry.iso_code] = {
                    "iso_code": entry.iso_code,
                    "country": entry.name,
                    "stored_rate": entry.vat_rate,
                    "found_rate": current_rate,
                    "is_match": is_match,
                    "confidence": confidence_str,
                    "source_note": reasoning,
                    "is_calculated": analysis.get("is_calculated", False),
                    "rate_diff": rate_diff,
                    # Extended fields
                    "sources_analyzed": analysis.get("sources_analyzed", []),
                    "confidence_score": confidence_score,
                    "confidence_tier": confidence_tier,
                    "needs_human_review": needs_review,
                    "review_reason": review_reason,
                    "source_agreement": source_agreement,
                    "temporal_notes": analysis.get("temporal_notes", ""),
                    "reasoning": reasoning,
                }

                if is_match:
                    add_log(f"✓ {entry.iso_code} {entry.name}: {entry.vat_rate}% confirmed (score={confidence_score:.2f})")
                else:
                    add_log(f"✗ {entry.iso_code} {entry.name}: stored={entry.vat_rate}% found={current_rate}% (score={confidence_score:.2f})")

                _outcome = "match" if is_match else ("review" if needs_review else "mismatch")
                _slog(
                    f"COUNTRY DONE | {entry.iso_code} | {entry.name}"
                    f" | outcome={_outcome}"
                    f" | score={confidence_score:.2f}"
                    f" | stored={entry.vat_rate}% | found={current_rate}%"
                )

            except Exception as e:
                _tb = traceback.format_exc()
                add_log(f"ERROR validating {entry.iso_code} {entry.name}: {e}")
                _slog(f"EXCEPTION | {entry.iso_code} | {entry.name} | {str(e)} | traceback={_tb[:600]}")
                st.session_state.raw_llm_responses[entry.iso_code] = ""
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
                    "sources_analyzed": [],
                    "confidence_score": 0.0,
                    "confidence_tier": "escalate",
                    "needs_human_review": True,
                    "review_reason": f"Exception during validation: {str(e)[:100]}",
                    "source_agreement": "conflicting",
                    "temporal_notes": "",
                    "reasoning": "",
                }

            time.sleep(delay_between)

        _slog(f"SCAN COMPLETED | validated={len(st.session_state.validation_results)}")
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
                score = data.get("confidence_score")
                review_flag = " 🔴" if data.get("needs_human_review") else ""
                score_str = f" ({score:.2f})" if isinstance(score, float) else ""
                cols[5].write(f"{confidence_colors.get(data['confidence'], '⚪')} {data['confidence']}{score_str}{review_flag}")

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

                # Show source details
                with st.expander("Source details", expanded=False):
                    if data.get("needs_human_review") and data.get("review_reason"):
                        st.warning(f"Review required: {data['review_reason']}")
                    if data.get("temporal_notes"):
                        st.caption(f"**Temporal notes:** {data['temporal_notes']}")
                    if data.get("reasoning"):
                        st.caption(f"**Reasoning:** {data['reasoning']}")
                    elif data.get("source_note"):
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

# ─── Reasoning Diary (test mode only) ────────────────────────────────────────

_DIARY_CODES = [
    "DE", "FR", "SE", "JP", "AU", "NZ", "EE", "ID", "IL", "EC",
    "SG", "HK", "BM", "QA", "RU", "BR", "IN", "CA", "GP", "MQ",
    "RE", "NC", "AI", "CM", "MW", "BB", "US", "KW", "IQ",
]

if st.session_state.validation_results and st.session_state.get("test_mode", False):
    st.header("🔍 Agent Reasoning Diary")
    st.caption("Full LLM reasoning trace for each validated country. Visible in test mode only.")

    diary_isos = [c for c in _DIARY_CODES if c in st.session_state.validation_results]

    for iso in diary_isos:
        data = st.session_state.validation_results[iso]
        score = data.get("confidence_score", 0.0)

        color_icon = "🟢" if score >= 0.9 else ("🟡" if score >= 0.7 else "🔴")
        review_tag = "  ⚠️ review flagged" if data.get("needs_human_review") else ""
        expander_label = f"{color_icon} {data['country']} ({iso}) — confidence {score:.2f}{review_tag}"

        with st.expander(expander_label, expanded=False):

            # ── Sources analyzed ──────────────────────────────────────────
            sources = data.get("sources_analyzed", [])
            if sources:
                st.subheader("Sources Analyzed")
                for src in sources:
                    if isinstance(src, dict):
                        url = src.get("url", "—")
                        s_type = src.get("source_type", "—")
                        claimed = src.get("claimed_rate")
                        quote = src.get("direct_quote", "")
                        pub_date = src.get("publication_date", "unknown")
                        weight = _tier_weight(url) if url.startswith("http") else _BLOG_WEIGHT
                        claimed_str = f"{claimed}%" if claimed is not None else "—"
                        st.markdown(
                            f"**{url}**  \n"
                            f"Type: `{s_type}` &nbsp;·&nbsp; "
                            f"Claimed rate: `{claimed_str}` &nbsp;·&nbsp; "
                            f"Published: `{pub_date}` &nbsp;·&nbsp; "
                            f"Tier weight: `{weight:.2f}`"
                        )
                        if quote:
                            st.caption(f"> {quote}")
                        st.divider()
                    else:
                        st.write(f"• {src}")
            else:
                st.caption("No sources recorded.")

            # ── Source agreement ──────────────────────────────────────────
            agreement = data.get("source_agreement", "—")
            agreement_display = {
                "all_agree":      "✅ All sources agree",
                "majority_agree": "🟡 Majority agree",
                "conflicting":    "❌ Sources conflicting",
                "insufficient":   "⚠️ Insufficient sources",
            }.get(agreement, f"— {agreement}")
            st.write(f"**Source Agreement:** {agreement_display}")

            # ── Temporal notes ────────────────────────────────────────────
            if data.get("temporal_notes"):
                st.write(f"**Temporal Notes:** {data['temporal_notes']}")

            # ── Full reasoning ────────────────────────────────────────────
            if data.get("reasoning"):
                st.write("**Reasoning:**")
                st.info(data["reasoning"])

            # ── Confidence score with visual bar ──────────────────────────
            filled = min(int(round(score * 10)), 10)
            bar = "█" * filled + "░" * (10 - filled)
            st.write(f"**Confidence Score:** {score:.2f}  `{bar}`")

            # ── Human review flag ─────────────────────────────────────────
            if data.get("needs_human_review"):
                st.error(f"🔴 Human review required — {data.get('review_reason', '—')}")
            else:
                st.success("✅ No human review required")

            # ── Raw LLM response ──────────────────────────────────────────
            raw = st.session_state.raw_llm_responses.get(iso, "")
            if raw:
                if st.toggle("Show raw LLM response", value=False, key=f"raw_{iso}"):
                    st.code(raw, language="json")

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
