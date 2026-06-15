import os

DEFAULT_LLM_MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3.6-plus")
DEFAULT_LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "6000"))

BUSINESS_CONTEXT = """
Business context for VAT determination:
- Company: Imagine Clarity — a meditation and mindfulness app
- Product type: Digital subscription service (app-based content streaming)
- Content category: Educational and wellness — meditation guides, audio content, video teachings
- Transaction type: B2C (business to consumer) — selling directly to individual end users
- Not a physical good, not a luxury item, not a financial service
- Revenue model: Fixed subscription price, VAT extracted quarterly from revenue

When determining the correct VAT rate, always apply the rate that specifically
governs digital subscription services or electronic services sold B2C to consumers
in that country. If a country has a different rate for digital services vs physical
goods, use the digital services rate. If a country has a reduced rate for
educational or cultural content, note this in temporal_notes and flag for human
review — it may apply to meditation/wellness content. If a country exempts
educational digital content from VAT entirely, flag for human review with
the exemption noted.
"""

PERMANENT_REVIEW_COUNTRIES = {
    "BR": {
        "until": "2034-01-01",
        "reason": "Brazil is mid tax reform (CBS/IBS dual VAT system replacing ICMS/PIS/COFINS). Full transition completes 2033. No single confirmed standard rate exists until then. Human review required every quarter."
    }
}

SPECIAL_RATES = {
    "RU": {"rate": 16.67, "note": "Calculated effective rate (20/120)"},
}

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

_SOURCE_TIERS: list[tuple[float, tuple]] = [
    (1.00, ("gov.", ".gov", "government")),
    (0.95, ("ec.europa.eu", "oecd.org")),
    (0.90, ("taxsummaries.pwc.com", "ey.com", "deloitte.com", "kpmg.com")),
    (0.85, ("taxfoundation.org", "ibfd.org")),
    (0.70, ("reuters.com", "bloomberg.com")),
]
_BLOG_WEIGHT = 0.2
_AGE_PENALTY_MONTHS = 18
_AGE_PENALTY_CAP = 0.70

_TEST_MODE_CODES = [
    "DE", "FR", "EE", "ID", "MW", "MQ", "GP", "RU", "BR", "HK", "US", "AI",
]
