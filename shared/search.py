"""
Search helpers — web queries and source credibility weighting.
Uses SyncMcpClient; called from the worker process.
"""

import re
import urllib.parse
from datetime import datetime, timedelta

from shared.constants import (
    COUNTRY_META,
    _SOURCE_TIERS,
    _BLOG_WEIGHT,
    _AGE_PENALTY_MONTHS,
    _AGE_PENALTY_CAP,
)


def _tier_weight(url: str, publication_date: str | None = None) -> float:
    """Return the credibility weight (0.2–1.0) for a given source URL.

    If *publication_date* (``"YYYY-MM-DD"``) is provided and the source is
    older than 18 months, the weight is capped at 0.70 regardless of domain
    authority — a 5-year-old government PDF should not outweigh a recent
    KPMG alert.
    """
    lower = url.lower()
    weight = _BLOG_WEIGHT
    for w, keywords in _SOURCE_TIERS:
        if any(k in lower for k in keywords):
            weight = w
            break

    if publication_date and publication_date != "unknown":
        try:
            pub = datetime.strptime(publication_date, "%Y-%m-%d")
            if datetime.now() - pub > timedelta(days=_AGE_PENALTY_MONTHS * 30):
                weight = min(weight, _AGE_PENALTY_CAP)
        except (ValueError, TypeError):
            pass

    return weight


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


def _search_country(search_client, entry, year: str, log_fn=None) -> str:
    """Run four targeted queries for a country and return combined results."""
    meta = COUNTRY_META.get(entry.iso_code)
    if meta:
        q_a = f"{entry.name} standard VAT rate {year} site:{meta['domain']}"
        q_b = f"{entry.name} VAT rate {year} site:taxsummaries.pwc.com"
        q_c = f"{entry.name} {meta['vat_term']} {year}"
    else:
        q_a = f"{entry.name} standard VAT GST rate {year}"
        q_b = f"{entry.name} VAT rate {year} site:taxsummaries.pwc.com"
        q_c = f"{entry.name} ({entry.iso_code}) VAT tax rate official {year}"

    # 4th query — always run, regardless of COUNTRY_META — designed to
    # surface structural tax reforms (abolitions, replacements) that the
    # standard rate-focused queries would miss.
    q_reform = f"{entry.name} VAT GST tax reform abolished replaced {int(year) - 1} {year}"

    parts: list[str] = []
    for label, q in [
        ("Official source", q_a),
        ("PwC Tax Summaries", q_b),
        ("Local language / general", q_c),
        ("Reform detection", q_reform),
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
