"""
Source discovery — asks Perplexity Sonar to find the authoritative VAT source
for a country.

Sonar is search-grounded by design, so this is live-search-based rather than
recalled from training data. It returns structured JSON directly; no second
model pass is used to parse or reformat its output.

Division of labour: this module's only job is finding WHERE to look.
shared.llm.analyze_vat_with_llm's only job is reading that source and
determining the rate.
"""

import os
import json
import httpx

from shared.llm import (
    OPENROUTER_URL,
    OPENROUTER_API_KEY,
    _OR_HEADERS,
    extract_json_object,
)

DISCOVERY_MODEL = os.environ.get("DISCOVERY_MODEL", "perplexity/sonar")

_SYSTEM_PROMPT = """You are a research assistant that locates official tax authority sources.

Given a country, identify:
1. The domain of that country's official government tax authority — the body that
   publishes the national VAT/GST rate. Bare domain only, no scheme and no path
   (e.g. "mra.mw", not "https://www.mra.mw/taxes").
2. The local-language term for VAT/GST in that country, if one is commonly used
   (e.g. "Mehrwertsteuer" for Germany, "TVA" for France). Use null if the country
   uses "VAT" or "GST" in English, or if you cannot determine it.

Prefer the actual tax authority over a general government portal. If the country
has no national VAT/GST system, still return its principal tax authority domain.

Return ONLY valid JSON in this exact structure, no markdown, no extra text:

{"domain": "mra.mw", "vat_term": "VAT", "confidence": "high"}

Valid values for confidence: high, medium, low
If you cannot identify a domain with reasonable certainty, set domain to null."""


def _clean_domain(value) -> str | None:
    """Normalise whatever Sonar returned into a bare domain, or None."""
    if not value or not isinstance(value, str):
        return None
    d = value.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix):]
    if d.startswith("www."):
        d = d[4:]
    d = d.split("/")[0].strip()
    # A bare domain needs at least one dot and no whitespace.
    if not d or "." not in d or " " in d:
        return None
    return d


async def discover_country_source(
    country_name: str,
    iso_code: str,
    api_key: str | None = None,
) -> dict:
    """Ask Perplexity Sonar for a country's authoritative VAT source.

    Returns {"domain": str | None, "vat_term": str | None, "confidence": str}.

    Never raises — on any API or parse failure it returns domain=None so the
    caller can fall back to an untargeted search rather than aborting a retry.
    """
    key = api_key or OPENROUTER_API_KEY
    failed = {"domain": None, "vat_term": None, "confidence": "low"}

    user_prompt = (
        f"Country: {country_name} ({iso_code})\n\n"
        f"Find the official government tax authority domain that publishes the "
        f"national VAT/GST rate for {country_name}, and the local term for VAT/GST. "
        f"Return JSON only."
    )

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={**_OR_HEADERS, "Authorization": f"Bearer {key}"},
                json={
                    "model": DISCOVERY_MODEL,
                    "messages": [
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 500,
                },
            )
            resp.raise_for_status()
            raw = resp.json()["choices"][0]["message"]["content"] or "{}"
    except Exception:
        return failed

    try:
        parsed = json.loads(extract_json_object(raw))
    except (json.JSONDecodeError, TypeError):
        return failed

    if not isinstance(parsed, dict):
        return failed

    vat_term = parsed.get("vat_term")
    if isinstance(vat_term, str):
        vat_term = vat_term.strip() or None
    elif vat_term is not None:
        vat_term = None

    return {
        "domain": _clean_domain(parsed.get("domain")),
        "vat_term": vat_term,
        "confidence": parsed.get("confidence") or "low",
    }
