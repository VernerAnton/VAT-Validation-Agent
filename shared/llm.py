"""
LLM analysis — calls OpenRouter via raw httpx to analyze VAT rates.
analyze_vat_with_llm is async; the worker calls it via asyncio.run().
"""

import os
import re
import json
import httpx
from datetime import datetime

from shared.constants import (
    BUSINESS_CONTEXT,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_MAX_TOKENS,
    _SOURCE_TIERS,
    _BLOG_WEIGHT,
    _AGE_PENALTY_MONTHS,
    _AGE_PENALTY_CAP,
)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")

_OR_HEADERS = {
    "HTTP-Referer": "https://vat-validation-agent.up.railway.app",
    "X-Title": "VAT Validation Agent",
}


def _tier_weight_for_prompt(url: str) -> float:
    """Quick weight lookup used only for the user-prompt annotation."""
    lower = url.lower()
    for w, keywords in _SOURCE_TIERS:
        if any(k in lower for k in keywords):
            return w
    return _BLOG_WEIGHT


async def analyze_vat_with_llm(
    country_name: str,
    iso_code: str,
    stored_rate: float,
    search_results: str,
    today: str,
    model: str | None = None,
    max_tokens: int | None = None,
    api_key: str | None = None,
) -> tuple[dict, str]:
    """Ask the LLM to analyze search results and return (parsed_dict, raw_json_string)."""
    model = model or DEFAULT_LLM_MODEL
    max_tokens = max_tokens or DEFAULT_LLM_MAX_TOKENS
    key = api_key or OPENROUTER_API_KEY

    urls = re.findall(r'URL:\s*(https?://\S+)', search_results)
    tier_lines = [f"  {u} → weight {_tier_weight_for_prompt(u):.2f}" for u in urls[:10]]
    tier_note = ("Source credibility weights:\n" + "\n".join(tier_lines)) if tier_lines else ""

    system_prompt = f"""Today is {today}. You are a tax data analyst determining the correct VAT/GST rate for a specific business.
{BUSINESS_CONTEXT}
Rules:
- Use only the national/federal standard rate, not regional or provincial variations
- If a country has no national VAT system, set standard_rate to 0 and needs_human_review to false
- If evidence is weak, conflicting or unclear, set needs_human_review to true instead of guessing
- If a country has GST instead of VAT, use that rate
- Before concluding that a rate is confirmed, check whether any source mentions that the VAT/GST system itself has been reformed, restructured, abolished, or replaced. A structural change is more important than rate confirmation. If any source mentions the tax system changing, set needs_human_review to true and describe the structural change in temporal_notes even if you cannot determine the new rate with certainty.
- Treat sources older than 18 months with reduced confidence. A recent accounting firm report should be weighted more heavily than an older government planning document.
- Source age is critical. For each source in sources_analyzed, check the publication_date field. If the most authoritative source you found (highest source_type tier) has a publication_date older than 18 months from today's date of {today}, you must set needs_human_review to true and explain in review_reason that the best available source may be outdated — do not auto-accept a rate based solely on an old document regardless of how authoritative the domain is. A government document from 2021 cannot confirm a 2026 rate. If no publication_date is available for a source, treat it as potentially outdated and note this in temporal_notes.

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={**_OR_HEADERS, "Authorization": f"Bearer {key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            raw_content = resp.json()["choices"][0]["message"]["content"] or "{}"
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

    # Strip <think>...</think> reasoning blocks (emitted by Qwen and other
    # reasoning-tuned models).
    content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()

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
