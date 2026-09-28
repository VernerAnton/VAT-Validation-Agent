"""
Preflight check for Sonar source discovery.

Asks Perplexity Sonar (via OpenRouter) for Malawi's tax authority once and
prints the result. The correct answer is "mra.mw" (already hardcoded in
COUNTRY_META), so a right answer is easy to verify by eye.

Confirms in one call that OPENROUTER_API_KEY is valid, the account has
credits, and DISCOVERY_MODEL is a real model — without running a scan.

Usage, from the repo root:
    OPENROUTER_API_KEY=sk-or-... python scripts/check_discovery.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.discovery import DISCOVERY_MODEL, discover_country_source  # noqa: E402


def main() -> int:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set.")
        return 2

    print(f"Model: {DISCOVERY_MODEL}")
    print("Asking Sonar for Malawi (MW)...")
    result = asyncio.run(discover_country_source("Malawi", "MW"))

    print(f"  domain:     {result.get('domain')}")
    print(f"  vat_term:   {result.get('vat_term')}")
    print(f"  confidence: {result.get('confidence')}")

    if result.get("error"):
        print(f"\nFAILED: {result['error']}")
        return 1
    if result.get("domain") == "mra.mw":
        print("\nOK: Sonar discovery is working.")
        return 0
    print("\nWARNING: the call worked, but the answer was not mra.mw. Check the prompt/model.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
