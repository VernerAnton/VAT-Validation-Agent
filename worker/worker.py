"""
VAT Validation Worker — standalone background polling process.

Polls sandbox MCP server for scan_job.json, runs VAT validation for each
country in the job, writes incremental progress, and sends a Telegram
notification on completion.

No web server — runs as a simple infinite loop with 10-second poll interval.
"""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import os
import re
import signal
import time
from datetime import date, datetime

import httpx

from shared.mcp_client import SyncMcpClient
from shared.constants import (
    PERMANENT_REVIEW_COUNTRIES,
    SPECIAL_RATES,
    _TEST_MODE_CODES,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_MAX_TOKENS,
)
from shared.vba_parser import parse_vba, CountryEntry
from shared.search import (
    _search_country,
    _search_country_targeted,
    _has_circular_sourcing,
)
from shared.llm import analyze_vat_with_llm
from shared.discovery import discover_country_source
from shared import db

# ─── Environment ─────────────────────────────────────────────────────────────

SANDBOX_URL       = os.environ.get("SANDBOX_URL",        "http://localhost:3003")
WEBSEARCH_URL     = os.environ.get("WEBSEARCH_URL",       "http://localhost:3002")
FILE_VAULT_URL    = os.environ.get("FILE_VAULT_URL",      "http://localhost:3001")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL         = os.environ.get("LLM_MODEL",           DEFAULT_LLM_MODEL)
LLM_MAX_TOKENS    = int(os.environ.get("LLM_MAX_TOKENS",  str(DEFAULT_LLM_MAX_TOKENS)))
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID",   "")
PROGRESS_DIR      = os.environ.get("PROGRESS_DIR",        "/data")
SCAN_DELAY        = float(os.environ.get("SCAN_DELAY",    "0.5"))
RETRY_DELAY       = float(os.environ.get("RETRY_DELAY",   "0.2"))

POLL_INTERVAL     = 10  # seconds between job polls

# ─── Global state ─────────────────────────────────────────────────────────────

_shutdown: bool = False
_current_progress: dict | None = None   # kept in memory for SIGTERM flush
_sandbox_client: SyncMcpClient | None = None
_scan_log_filename: str = ""


def _handle_sigterm(signum, frame) -> None:
    global _shutdown
    print("[WORKER] SIGTERM received — flushing progress and shutting down.")
    _shutdown = True
    if _current_progress is not None:
        _write_progress_atomic(_current_progress)


signal.signal(signal.SIGTERM, _handle_sigterm)

# ─── Sandbox log helper ───────────────────────────────────────────────────────

def _slog(msg: str) -> None:
    """Append a timestamped line to the scan log on the sandbox server."""
    if not _scan_log_filename or _sandbox_client is None:
        return
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}\n"
    try:
        _sandbox_client.call_tool("append_draft", {
            "filename": _scan_log_filename,
            "content": line,
        })
    except Exception as e:
        print(f"[SLOG ERROR] Could not write to sandbox log: {e}")


# ─── Atomic file write helpers ────────────────────────────────────────────────

def _write_atomic(path: str, data: dict) -> None:
    """Write JSON atomically to a local file using temp + os.replace()."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _write_progress_atomic(progress: dict) -> None:
    """Write validation_progress.json atomically to PROGRESS_DIR and sandbox."""
    local_path = os.path.join(PROGRESS_DIR, "validation_progress.json")
    _write_atomic(local_path, progress)
    if _sandbox_client is not None:
        try:
            _sandbox_client.call_tool("write_draft", {
                "filename": "validation_progress.json",
                "content": json.dumps(progress),
            })
        except Exception as e:
            print(f"[PROGRESS] Warning: could not sync progress to sandbox: {e}")


def _write_results_atomic(results: dict) -> None:
    """Write scan_results.json atomically to PROGRESS_DIR and sandbox."""
    local_path = os.path.join(PROGRESS_DIR, "scan_results.json")
    _write_atomic(local_path, results)
    if _sandbox_client is not None:
        try:
            _sandbox_client.call_tool("write_draft", {
                "filename": "scan_results.json",
                "content": json.dumps(results),
            })
        except Exception as e:
            print(f"[RESULTS] Warning: could not sync results to sandbox: {e}")


# ─── Resume logic ─────────────────────────────────────────────────────────────

def _try_load_resume(sandbox: SyncMcpClient, job_id: str) -> dict | None:
    """Try to load a prior in-progress run from the sandbox, but only if
    it belongs to the same job_id — otherwise a stale progress file from
    a completed or abandoned job could incorrectly short-circuit a new scan.
    """
    try:
        raw = sandbox.call_tool("read_draft", {"filename": "validation_progress.json"})
        data = json.loads(raw)
        if data.get("status") == "in_progress" and data.get("job_id") == job_id:
            return data
    except Exception:
        pass
    return None


# ─── Job polling ──────────────────────────────────────────────────────────────

SCAN_JOB_FILE  = "scan_job.json"
RETRY_JOB_FILE = "retry_job.json"


def _poll_for_job(sandbox: SyncMcpClient, filename: str = SCAN_JOB_FILE) -> dict | None:
    """Read a job file from sandbox. Returns the dict if status=='pending'.

    Retries live in their own file rather than sharing scan_job.json: the job
    dict is captured in memory at claim time and written back wholesale on
    completion, so a retry queued into the same file mid-scan would be
    silently overwritten when the scan finished.
    """
    try:
        raw = sandbox.call_tool("read_draft", {"filename": filename})
        job = json.loads(raw)
        if job.get("status") == "pending":
            return job
    except Exception:
        pass
    return None


def _set_job_status(sandbox: SyncMcpClient, job: dict, filename: str, status: str) -> None:
    """Write a job back to its file with the given status."""
    job["status"] = status
    sandbox.call_tool("write_draft", {
        "filename": filename,
        "content": json.dumps(job),
    })


def _claim_job(sandbox: SyncMcpClient, job: dict, filename: str = SCAN_JOB_FILE) -> None:
    """Mark the job as in_progress on the sandbox server."""
    _set_job_status(sandbox, job, filename, "in_progress")


def _complete_job(sandbox: SyncMcpClient, job: dict, filename: str = SCAN_JOB_FILE) -> None:
    """Mark the job as done on the sandbox server."""
    _set_job_status(sandbox, job, filename, "done")


def _read_scan_results(sandbox: SyncMcpClient) -> dict | None:
    """Read the current scan_results.json back from the sandbox.

    The sandbox copy is authoritative: /api/scan/results serves it, so it is
    what the UI displays and therefore what a retry must patch. The local
    /data copy is a mirror and can be absent on a fresh volume.
    """
    try:
        raw = sandbox.call_tool("read_draft", {"filename": "scan_results.json"})
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


# ─── Telegram notification ────────────────────────────────────────────────────

def _send_telegram(message: str) -> None:
    """Send a Telegram message. Handles 429 with one retry. Never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] No credentials configured — skipping notification.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            resp = client.post(url, json=payload)
            if resp.status_code == 429:
                retry_after = 5
                try:
                    # Try Retry-After header first
                    if "Retry-After" in resp.headers:
                        retry_after = int(resp.headers["Retry-After"])
                    else:
                        body = resp.json()
                        retry_after = (
                            body.get("parameters", {}).get("retry_after", 5)
                        )
                except Exception:
                    retry_after = 5
                print(f"[TELEGRAM] Rate limited — retrying after {retry_after}s")
                time.sleep(retry_after)
                resp = client.post(url, json=payload)
            resp.raise_for_status()
            print("[TELEGRAM] Notification sent.")
    except Exception as e:
        print(f"[TELEGRAM] Failed to send notification: {e}")


# ─── Confidence helpers ───────────────────────────────────────────────────────

def _confidence_label(score: float) -> str:
    if score >= 0.85:
        return "high"
    if score >= 0.60:
        return "medium"
    return "low"


def _confidence_tier(score: float, needs_review: bool) -> str:
    if needs_review:
        return "human_review"
    if score >= 0.85:
        return "auto_accept"
    if score >= 0.60:
        return "log_audit"
    return "escalate"


# ─── Country result builder ───────────────────────────────────────────────────

def _build_result(entry: CountryEntry, analysis: dict, search_results: str) -> dict:
    """Turn a raw LLM analysis into a country result dict.

    Shared by the full-scan and targeted-retry paths so the two cannot drift —
    the null-rate coercion, escalation rules, permanent-review mandates and
    special-rate annotations must behave identically no matter which path ran.
    """
    # The LLM may legitimately return null for standard_rate when no single
    # rate exists — a country mid tax reform, for example. Note that
    # dict.get() only applies its default when the key is absent, NOT when it
    # is present with a null value, so coerce explicitly.
    _raw_rate = analysis.get("standard_rate")
    _raw_score = analysis.get("confidence_score")

    rate_undetermined = _raw_rate is None
    try:
        current_rate: float = entry.vat_rate if rate_undetermined else float(_raw_rate)
    except (TypeError, ValueError):
        rate_undetermined = True
        current_rate = entry.vat_rate

    try:
        confidence_score: float = 0.0 if _raw_score is None else float(_raw_score)
    except (TypeError, ValueError):
        confidence_score = 0.0

    needs_review: bool = bool(analysis.get("needs_human_review", False))
    review_reason: str | None = analysis.get("review_reason")
    source_agreement: str = analysis.get("source_agreement") or "insufficient"
    reasoning: str = analysis.get("reasoning") or ""

    # No determinable rate is itself grounds for review — keep the stored rate
    # rather than inventing one.
    if rate_undetermined:
        needs_review = True
        review_reason = review_reason or (
            "No single standard rate could be determined from the sources; "
            "stored rate retained pending human review."
        )

    # Auto-escalate overrides
    if source_agreement == "conflicting":
        needs_review = True
        review_reason = review_reason or "Conflicting sources detected."

    if _has_circular_sourcing(search_results):
        needs_review = True
        review_reason = (review_reason or "") + " Circular/insufficient source diversity."

    if confidence_score < 0.5:
        needs_review = True
        review_reason = (review_reason or "") + f" Low confidence score ({confidence_score:.2f})."

    # PERMANENT_REVIEW_COUNTRIES override
    if entry.iso_code in PERMANENT_REVIEW_COUNTRIES:
        perm = PERMANENT_REVIEW_COUNTRIES[entry.iso_code]
        try:
            until_date = date.fromisoformat(perm["until"])
            if until_date > date.today():
                needs_review = True
                perm_reason = perm.get("reason", "Country flagged for permanent review.")
                review_reason = perm_reason if not review_reason else f"{review_reason} | {perm_reason}"
        except (ValueError, KeyError):
            pass

    # SPECIAL_RATES annotation
    if entry.iso_code in SPECIAL_RATES:
        special = SPECIAL_RATES[entry.iso_code]
        if abs(current_rate - special["rate"]) < 0.01:
            note = special.get("note", "")
            reasoning = f"{reasoning} [Special rate note: {note}]" if reasoning else f"[Special rate note: {note}]"

    return {
        "iso_code": entry.iso_code,
        "country": entry.name,
        "stored_rate": entry.vat_rate,
        "found_rate": current_rate,
        "is_match": abs(current_rate - entry.vat_rate) < 0.01,
        "confidence": _confidence_label(confidence_score),
        "confidence_score": confidence_score,
        "confidence_tier": _confidence_tier(confidence_score, needs_review),
        "needs_human_review": needs_review,
        "review_reason": review_reason,
        "source_agreement": source_agreement,
        "temporal_notes": analysis.get("temporal_notes", ""),
        "reasoning": reasoning,
        "sources_analyzed": analysis.get("sources_analyzed", []),
        "rate_diff": abs(current_rate - entry.vat_rate),
        "is_calculated": analysis.get("is_calculated", False),
        "source_note": reasoning,
    }


def _build_error_result(entry: CountryEntry, exc: Exception) -> dict:
    reason = f"Exception during processing: {str(exc)[:200]}"
    # A country under a standing review mandate keeps that as its stated
    # reason even when processing failed — the mandate explains the review
    # far better than an incidental stack error does.
    perm = PERMANENT_REVIEW_COUNTRIES.get(entry.iso_code)
    if perm:
        try:
            if date.fromisoformat(perm["until"]) > date.today():
                reason = f"{perm['reason']} (processing also failed: {str(exc)[:120]})"
        except (ValueError, KeyError):
            pass
    return {
        "iso_code": entry.iso_code,
        "country": entry.name,
        "stored_rate": entry.vat_rate,
        "found_rate": entry.vat_rate,
        "is_match": True,
        "confidence": "low",
        "confidence_score": 0.0,
        "confidence_tier": "human_review",
        "needs_human_review": True,
        "review_reason": reason,
        "source_agreement": "insufficient",
        "temporal_notes": "",
        "reasoning": f"Processing failed with exception: {str(exc)}",
        "sources_analyzed": [],
        "rate_diff": 0.0,
        "is_calculated": False,
        "source_note": f"Processing failed with exception: {str(exc)}",
    }


# ─── Core scan ────────────────────────────────────────────────────────────────

def _run_scan(
    job: dict,
    sandbox: SyncMcpClient,
    search_client: SyncMcpClient,
    vault_client: SyncMcpClient,
    resumed_progress: dict | None,
) -> dict:
    """Execute the full scan for the given job. Returns the final results dict."""
    global _current_progress, _scan_log_filename

    job_id = job["job_id"]
    requested_countries: list[str] = job["countries"]
    test_mode: bool = job.get("test_mode", False)
    today = date.today().isoformat()
    year = str(date.today().year)

    # Set up scan log
    ts_stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    _scan_log_filename = f"scan_log_{ts_stamp}.txt"
    _slog(f"Scan started — job_id={job_id} test_mode={test_mode} countries={requested_countries}")

    # Fetch VBA once
    print("[SCAN] Fetching VBA file from file-vault...")
    _slog("Fetching VBA file from file-vault...")
    raw_vba = vault_client.call_tool("get_vat_file")
    all_entries, _header, _footer = parse_vba(raw_vba)
    print(f"[SCAN] Parsed {len(all_entries)} total entries from VBA.")
    _slog(f"Parsed {len(all_entries)} total VBA entries.")

    # Filter by requested countries (and test mode override)
    active_codes = set(_TEST_MODE_CODES if test_mode else requested_countries)
    entries = [e for e in all_entries if e.iso_code in active_codes]
    total = len(entries)
    print(f"[SCAN] {total} countries to validate (filtered from {len(all_entries)}).")
    _slog(f"{total} countries to validate after filter.")

    # Resume state
    last_completed_index: int = 0
    accumulated_results: dict = {}

    if resumed_progress is not None:
        last_completed_index = resumed_progress.get("last_completed_index", 0)
        accumulated_results = resumed_progress.get("results", {})
        print(f"[RESUME] Loaded {len(accumulated_results)} previously validated countries")
        _slog(f"Resuming from index {last_completed_index} with {len(accumulated_results)} prior results.")

    # Initial progress snapshot
    progress: dict = {
        "job_id": job_id,
        "status": "in_progress",
        "mode": "scan",
        "last_completed_index": last_completed_index,
        "total": total,
        "current_country": None,
        "results": accumulated_results,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    _current_progress = progress
    _write_progress_atomic(progress)

    for i, entry in enumerate(entries):
        if _shutdown:
            print("[WORKER] Shutdown flag set — stopping scan early.")
            break

        # Skip already-completed entries (resume)
        if i < last_completed_index:
            continue

        print(f"[{i + 1}/{total}] Processing {entry.iso_code} ({entry.name})...")
        _slog(f"[{i + 1}/{total}] Starting {entry.iso_code} — {entry.name}")

        # Write in-progress marker before processing
        progress["current_country"] = entry.iso_code
        progress["last_completed_index"] = i
        progress["updated_at"] = datetime.utcnow().isoformat() + "Z"
        _current_progress = progress
        _write_progress_atomic(progress)

        try:
            # Web search
            search_results = _search_country(search_client, entry, year, log_fn=print)
            _slog(f"{entry.iso_code}: search complete, result_len={len(search_results)}")

            # LLM analysis
            analysis, _raw_json = asyncio.run(
                analyze_vat_with_llm(
                    country_name=entry.name,
                    iso_code=entry.iso_code,
                    stored_rate=entry.vat_rate,
                    search_results=search_results,
                    today=today,
                    model=LLM_MODEL,
                    max_tokens=LLM_MAX_TOKENS,
                    api_key=OPENROUTER_API_KEY,
                )
            )
            result = _build_result(entry, analysis, search_results)
            confidence_score = result["confidence_score"]
            current_rate = result["found_rate"]
            needs_review = result["needs_human_review"]

            _slog(f"{entry.iso_code}: LLM analysis done — score={confidence_score:.2f}")

            found_str = f"{current_rate}%"
            stored_str = f"{entry.vat_rate}%"
            print(
                f"[{i + 1}/{total}] {entry.iso_code} {entry.name}: "
                f"stored={stored_str} found={found_str} "
                f"score={confidence_score:.2f}"
            )
            _slog(
                f"{entry.iso_code}: stored={stored_str} found={found_str} "
                f"match={result['is_match']} review={needs_review} score={confidence_score:.2f}"
            )

        except Exception as exc:
            print(f"[{i + 1}/{total}] {entry.iso_code} {entry.name}: ERROR — {exc}")
            _slog(f"{entry.iso_code}: EXCEPTION — {exc}")
            result = _build_error_result(entry, exc)

        # Accumulate result
        accumulated_results[entry.iso_code] = result
        progress["results"] = accumulated_results
        progress["last_completed_index"] = i + 1
        progress["current_country"] = entry.iso_code
        progress["updated_at"] = datetime.utcnow().isoformat() + "Z"
        _current_progress = progress
        _write_progress_atomic(progress)

        # Configurable delay between countries
        if i < total - 1 and not _shutdown:
            time.sleep(SCAN_DELAY)

    # Build final results payload
    final_results: dict = {
        "job_id": job_id,
        "status": "done",
        "last_completed_index": len(entries),
        "total": total,
        "current_country": None,
        "results": accumulated_results,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    return final_results


# ─── Targeted retry ───────────────────────────────────────────────────────────

def _resolve_source(entry: CountryEntry) -> tuple[str | None, str | None]:
    """Return (domain, vat_term) for a targeted search.

    Checks the permanent country_sources cache first; runs Sonar discovery on a
    miss or on a row previously flagged stale. The cache is purely directional
    — it says where to look and what terminology to expect, and never
    substitutes for actually performing the search and analysis.
    """
    try:
        cached = db.get_country_source(entry.iso_code)
    except Exception as e:
        print(f"[RETRY] {entry.iso_code}: cache read failed ({e}) — discovering")
        cached = None

    if cached and cached.get("domain") and not cached.get("stale"):
        print(f"[RETRY] {entry.iso_code}: using cached domain {cached['domain']}")
        _slog(f"{entry.iso_code}: cached domain {cached['domain']}")
        return cached["domain"], cached.get("vat_term")

    reason = "stale" if cached else "no cache entry"
    print(f"[RETRY] {entry.iso_code}: {reason} — running Sonar discovery")
    _slog(f"{entry.iso_code}: discovery ({reason})")

    found = asyncio.run(
        discover_country_source(entry.name, entry.iso_code, api_key=OPENROUTER_API_KEY)
    )
    domain = found.get("domain")
    vat_term = found.get("vat_term")

    if domain:
        try:
            db.save_country_source(entry.iso_code, domain, vat_term)
        except Exception as e:
            print(f"[RETRY] {entry.iso_code}: could not persist source ({e})")
        print(f"[RETRY] {entry.iso_code}: discovered {domain} (term={vat_term})")
        _slog(f"{entry.iso_code}: discovered domain={domain} term={vat_term}")
    else:
        print(f"[RETRY] {entry.iso_code}: discovery found no domain — generic search")
        _slog(f"{entry.iso_code}: discovery failed, falling back to generic search")

    return domain, vat_term


def _run_retry(
    job: dict,
    sandbox: SyncMcpClient,
    search_client: SyncMcpClient,
    vault_client: SyncMcpClient,
) -> None:
    """Re-verify specific countries against a discovered authoritative source.

    Patches those countries' entries in scan_results.json in place, leaving
    every other country — and any review_action already recorded against it —
    untouched.
    """
    global _current_progress, _scan_log_filename

    job_id = job["job_id"]
    requested: list[str] = job.get("countries", []) or []
    today = date.today().isoformat()
    year = str(date.today().year)

    ts_stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    _scan_log_filename = f"scan_log_{ts_stamp}.txt"
    _slog(f"Retry started — job_id={job_id} countries={requested}")

    raw_vba = vault_client.call_tool("get_vat_file")
    all_entries, _header, _footer = parse_vba(raw_vba)

    # Filter strictly by the requested list. Deliberately NOT reusing the main
    # scan's filter, which lets test_mode override the countries list — that
    # would silently retry the 12 test countries instead of what was asked for.
    wanted = set(requested)
    entries = [e for e in all_entries if e.iso_code in wanted]
    total = len(entries)
    print(f"[RETRY] {total} countries to re-verify.")
    _slog(f"{total} countries to re-verify.")

    scan_results = _read_scan_results(sandbox)
    if scan_results is None:
        print("[RETRY] No scan_results.json to patch — aborting retry.")
        _slog("No scan_results.json found; retry aborted.")
        return
    results_map = scan_results.setdefault("results", {})

    progress: dict = {
        "job_id": job_id,
        "status": "in_progress",
        "mode": "retry",
        "last_completed_index": 0,
        "total": total,
        "current_country": None,
        "results": results_map,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    _current_progress = progress
    _write_progress_atomic(progress)

    for i, entry in enumerate(entries):
        if _shutdown:
            print("[RETRY] Shutdown flag set — stopping early.")
            break

        print(f"[RETRY {i + 1}/{total}] {entry.iso_code} ({entry.name})")
        progress["current_country"] = entry.iso_code
        progress["last_completed_index"] = i
        progress["updated_at"] = datetime.utcnow().isoformat() + "Z"
        _current_progress = progress
        _write_progress_atomic(progress)

        try:
            domain, vat_term = _resolve_source(entry)

            search_results = _search_country_targeted(
                search_client, entry, year, domain, vat_term, log_fn=print
            )

            # A cached domain that yields nothing usable is a dead domain —
            # flag it so the next retry rediscovers instead of trusting it.
            if domain:
                n_urls = len(re.findall(r"URL:\s*(https?://\S+)", search_results))
                if n_urls == 0 or _has_circular_sourcing(search_results):
                    try:
                        db.mark_source_stale(entry.iso_code)
                        print(f"[RETRY] {entry.iso_code}: marked {domain} stale")
                        _slog(f"{entry.iso_code}: marked domain {domain} stale")
                    except Exception:
                        pass

            analysis, _raw = asyncio.run(
                analyze_vat_with_llm(
                    country_name=entry.name,
                    iso_code=entry.iso_code,
                    stored_rate=entry.vat_rate,
                    search_results=search_results,
                    today=today,
                    model=LLM_MODEL,
                    max_tokens=LLM_MAX_TOKENS,
                    api_key=OPENROUTER_API_KEY,
                )
            )

            result = _build_result(entry, analysis, search_results)
            result["re_verified"] = True
            result["re_verified_at"] = datetime.utcnow().isoformat() + "Z"
            if domain:
                result["re_verified_domain"] = domain

            try:
                db.save_result(
                    entry.iso_code,
                    result["found_rate"],
                    result["confidence_score"],
                    result["reasoning"],
                    result["sources_analyzed"],
                )
            except Exception as e:
                print(f"[RETRY] {entry.iso_code}: audit write failed ({e})")

            print(
                f"[RETRY {i + 1}/{total}] {entry.iso_code}: "
                f"stored={entry.vat_rate}% found={result['found_rate']}% "
                f"score={result['confidence_score']:.2f}"
            )
            _slog(
                f"{entry.iso_code}: re-verified stored={entry.vat_rate}% "
                f"found={result['found_rate']}% score={result['confidence_score']:.2f}"
            )

        except Exception as exc:
            print(f"[RETRY {i + 1}/{total}] {entry.iso_code}: ERROR — {exc}")
            _slog(f"{entry.iso_code}: RETRY EXCEPTION — {exc}")
            result = _build_error_result(entry, exc)
            result["re_verified"] = True

        # Patch only this country, preserving any review decision already made
        # against it via /api/scan/review.
        previous = results_map.get(entry.iso_code) or {}
        if "review_action" in previous:
            result["review_action"] = previous["review_action"]
        results_map[entry.iso_code] = result

        scan_results["results"] = results_map
        scan_results["updated_at"] = datetime.utcnow().isoformat() + "Z"
        _write_results_atomic(scan_results)

        progress["results"] = results_map
        progress["last_completed_index"] = i + 1
        progress["updated_at"] = datetime.utcnow().isoformat() + "Z"
        _current_progress = progress
        _write_progress_atomic(progress)

        if i < total - 1 and not _shutdown:
            time.sleep(RETRY_DELAY)

    progress["status"] = "done"
    progress["current_country"] = None
    progress["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _current_progress = progress
    _write_progress_atomic(progress)
    print(f"[RETRY] Job {job_id} complete.")
    _slog(f"Retry job {job_id} complete.")


# ─── Telegram summary builder ─────────────────────────────────────────────────

def _build_telegram_message(final_results: dict) -> str:
    results = final_results.get("results", {})
    total = len(results)
    mismatches = sum(1 for r in results.values() if not r.get("is_match", True))
    needs_review_count = sum(1 for r in results.values() if r.get("needs_human_review", False))
    job_id = final_results.get("job_id", "unknown")

    mismatch_lines = []
    for iso, r in results.items():
        if not r.get("is_match", True):
            stored = r.get("stored_rate", "?")
            found = r.get("found_rate", "?")
            country = r.get("country", iso)
            mismatch_lines.append(f"  • {country} ({iso}): {stored}% → {found}%")

    mismatch_section = "\n".join(mismatch_lines) if mismatch_lines else "  None"

    return (
        f"<b>VAT Scan Complete</b>\n\n"
        f"Job: <code>{job_id}</code>\n"
        f"Countries validated: {total}\n"
        f"Rate mismatches: {mismatches}\n"
        f"Needs human review: {needs_review_count}\n\n"
        f"<b>Mismatches:</b>\n{mismatch_section}"
    )


# ─── Main entry point ─────────────────────────────────────────────────────────

def main() -> None:
    global _sandbox_client

    # Ensure PROGRESS_DIR exists
    os.makedirs(PROGRESS_DIR, exist_ok=True)

    print("[WORKER] Starting VAT Validation Worker.")
    print(f"[WORKER] SANDBOX_URL={SANDBOX_URL}")
    print(f"[WORKER] WEBSEARCH_URL={WEBSEARCH_URL}")
    print(f"[WORKER] FILE_VAULT_URL={FILE_VAULT_URL}")
    print(f"[WORKER] LLM_MODEL={LLM_MODEL}")
    print(f"[WORKER] PROGRESS_DIR={PROGRESS_DIR}")

    sandbox     = SyncMcpClient(SANDBOX_URL)
    search_client = SyncMcpClient(WEBSEARCH_URL)
    vault_client  = SyncMcpClient(FILE_VAULT_URL)

    _sandbox_client = sandbox

    while not _shutdown:
        # Retries are cheap and user-initiated; check them first so a queued
        # re-verification is not stuck behind a freshly-queued full scan.
        retry_job = _poll_for_job(sandbox, RETRY_JOB_FILE)
        if retry_job is not None:
            print(f"[WORKER] Found pending retry {retry_job['job_id']} — claiming...")
            _claim_job(sandbox, retry_job, RETRY_JOB_FILE)
            try:
                _run_retry(retry_job, sandbox, search_client, vault_client)
                _complete_job(sandbox, retry_job, RETRY_JOB_FILE)
            except Exception as exc:
                print(f"[WORKER] Retry failed: {exc}")
                _slog(f"Retry failed: {exc}")
                _set_job_status(sandbox, retry_job, RETRY_JOB_FILE, "failed")
                _write_progress_atomic({
                    "job_id": retry_job.get("job_id", "unknown"),
                    "status": "failed",
                    "mode": "retry",
                    "last_completed_index": 0,
                    "total": 0,
                    "current_country": None,
                    "results": {},
                    "updated_at": datetime.utcnow().isoformat() + "Z",
                })
            continue

        job = _poll_for_job(sandbox)
        if job is None:
            time.sleep(POLL_INTERVAL)
            continue

        print(f"[WORKER] Found pending job {job['job_id']} — claiming...")
        _claim_job(sandbox, job)
        resumed_progress = _try_load_resume(sandbox, job["job_id"])
        if resumed_progress:
            n_prior = len(resumed_progress.get("results", {}))
            print(f"[RESUME] Found matching in-progress state with {n_prior} completed countries.")

        try:
            final_results = _run_scan(
                job=job,
                sandbox=sandbox,
                search_client=search_client,
                vault_client=vault_client,
                resumed_progress=resumed_progress,
            )
        except Exception as exc:
            print(f"[WORKER] Scan failed with unexpected error: {exc}")
            _slog(f"Scan failed: {exc}")
            # Write failed status
            failed_progress: dict = {
                "job_id": job.get("job_id", "unknown"),
                "status": "failed",
                "last_completed_index": 0,
                "total": 0,
                "current_country": None,
                "results": {},
                "updated_at": datetime.utcnow().isoformat() + "Z",
            }
            _write_progress_atomic(failed_progress)
            sandbox.call_tool("write_draft", {
                "filename": "scan_job.json",
                "content": json.dumps({**job, "status": "failed"}),
            })
            time.sleep(POLL_INTERVAL)
            continue

        # Write scan_results.json (final)
        _write_results_atomic(final_results)
        _slog("scan_results.json written.")

        # Send Telegram notification
        telegram_msg = _build_telegram_message(final_results)
        _send_telegram(telegram_msg)

        # Mark progress and job as done
        done_progress: dict = {
            **final_results,
            "status": "done",
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        _write_progress_atomic(done_progress)
        _complete_job(sandbox, job)

        print(f"[WORKER] Job {job['job_id']} complete.")
        _slog(f"Job {job['job_id']} complete.")

    print("[WORKER] Shutdown complete.")


if __name__ == "__main__":
    main()
