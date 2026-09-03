"""
SQLite storage for adaptive source discovery and retry audit history.

Lives on the worker's existing /data volume alongside validation_progress.json
and scan_results.json — no new volume or mount required.

IMPORTANT: this module is worker-only. Railway volumes attach to a single
service, so fastapi-app has no /data and must never import this expecting to
read data back.

Connections are short-lived (opened and closed per call) rather than one handle
held for the life of the worker process, mirroring the per-call discipline of
_write_atomic() in worker/worker.py.
"""

import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", "/data/vat_agent.db")
RESULTS_CACHE_MONTHS = int(os.environ.get("RESULTS_CACHE_MONTHS", "18"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS country_sources (
    iso_code TEXT PRIMARY KEY,
    domain TEXT,
    vat_term TEXT,
    discovered_at TEXT,
    source TEXT,
    stale INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS country_results (
    iso_code TEXT,
    rate REAL,
    confidence_score REAL,
    reasoning TEXT,
    sources_analyzed TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_country_results_iso
    ON country_results (iso_code, created_at DESC);
"""


@contextmanager
def _conn():
    """Open a short-lived connection, commit on clean exit, always close."""
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


# ─── country_sources — permanent, no expiry ──────────────────────────────────

def get_country_source(iso_code: str) -> dict | None:
    """Return the cached source for a country, or None if absent.

    A row flagged stale is still returned; the caller decides whether to
    re-discover. Inspect the "stale" key rather than assuming freshness.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT iso_code, domain, vat_term, discovered_at, source, stale "
            "FROM country_sources WHERE iso_code = ?",
            (iso_code,),
        ).fetchone()
    return dict(row) if row else None


def save_country_source(
    iso_code: str,
    domain: str,
    vat_term: str | None,
    source: str = "sonar_discovery",
) -> None:
    """Insert or replace a country's discovered source, clearing any stale flag."""
    with _conn() as conn:
        conn.execute(
            "INSERT INTO country_sources "
            "(iso_code, domain, vat_term, discovered_at, source, stale) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(iso_code) DO UPDATE SET "
            "domain=excluded.domain, vat_term=excluded.vat_term, "
            "discovered_at=excluded.discovered_at, source=excluded.source, stale=0",
            (iso_code, domain, vat_term, _now(), source),
        )


def mark_source_stale(iso_code: str) -> None:
    """Flag a cached domain as no longer producing usable results.

    The next retry for this country re-runs discovery instead of trusting a
    dead domain.
    """
    with _conn() as conn:
        conn.execute(
            "UPDATE country_sources SET stale = 1 WHERE iso_code = ?", (iso_code,)
        )


# ─── country_results — write-only audit history ──────────────────────────────

def save_result(
    iso_code: str,
    rate: float | None,
    confidence_score: float,
    reasoning: str,
    sources_analyzed: list | dict | None,
) -> None:
    """Append a retry outcome to the audit history.

    Written only by the retry flow. Normal first-pass scans never touch this.
    """
    with _conn() as conn:
        conn.execute(
            "INSERT INTO country_results "
            "(iso_code, rate, confidence_score, reasoning, sources_analyzed, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                iso_code,
                rate,
                confidence_score,
                reasoning,
                json.dumps(sources_analyzed or []),
                _now(),
            ),
        )


def get_recent_result(iso_code: str, max_age_months: int = RESULTS_CACHE_MONTHS) -> dict | None:
    """Return the most recent audit row for a country, if within max_age_months.

    DELIBERATELY NOT WIRED INTO THE RETRY PATH. Every retry always performs the
    full discovery-check -> search -> LLM sequence; this cache never short-
    circuits that work. Provided as an audit/history accessor for a future
    "previous verifications" view.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT iso_code, rate, confidence_score, reasoning, sources_analyzed, "
            "created_at FROM country_results WHERE iso_code = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (iso_code,),
        ).fetchone()

    if row is None:
        return None

    record = dict(row)
    cutoff = datetime.utcnow() - timedelta(days=max_age_months * 30)
    try:
        created = datetime.fromisoformat(record["created_at"].rstrip("Z"))
    except (ValueError, AttributeError):
        return None
    if created < cutoff:
        return None

    try:
        record["sources_analyzed"] = json.loads(record["sources_analyzed"] or "[]")
    except json.JSONDecodeError:
        record["sources_analyzed"] = []
    return record
