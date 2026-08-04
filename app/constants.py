"""Cross-cutting constants shared by models, schemas, services, and routers.

Kept out of ``app.config`` so models can import them without pulling in the
settings machinery (and the circular import that would come with it).
"""

# ISO-3166-1 alpha-2 countries the ``companies.country`` CHECK constraint
# permits. This is the *schema* allowlist: widening it requires an Alembic
# migration, because the database enforces it.
#
# Which of these we actually prospect and import into is a separate, narrower
# *policy* allowlist — ``settings.supported_countries`` — tunable without a
# deploy. Policy must always be a subset of this list, or inserts will fail the
# constraint.
ALLOWED_COUNTRIES: tuple[str, ...] = (
    "US", "CA", "IN", "GB", "AU", "DE", "FR", "SG", "AE",
)

# Rendered into the CHECK constraint and the migration. Single source of truth
# so the model and the migration can never drift.
ALLOWED_COUNTRIES_SQL = ", ".join(f"'{c}'" for c in ALLOWED_COUNTRIES)

# Shared by the Pydantic schemas and the ``?country=`` query params, so a filter
# can express any country the schema can store.
COUNTRY_QUERY_PATTERN = f"^({'|'.join(ALLOWED_COUNTRIES)})$"


# --- Consent regimes -------------------------------------------------------
#
# ``services.consent.check_consent`` is a *send gate*, and a country it does not
# recognise is refused — which pauses the enrollment permanently. So this table
# must cover every country the schema above admits, or widening the allowlist
# silently kills outreach to the market it was widened for. That is not
# hypothetical: the allowlist grew to nine countries, the gate still branched on
# ``US``/``CA``, and the other seven answered "Unknown jurisdiction" at the wire.
# ``tests/test_consent.py::test_every_allowed_country_has_a_regime`` pins the two
# lists together.
#
# ``OPT_IN`` means consent must exist *before* the first send (CASL's model).
# ``OPT_OUT`` means a cold send is permitted and the duty is an honoured
# unsubscribe (CAN-SPAM's model) — which this platform discharges on every send
# via the footer link and the RFC 8058 header, and enforces through
# ``services.opt_out`` and the suppression list.
#
# **This is an engineering default, not legal advice**, and it is deliberately
# per-country rather than a single global default so tightening one market is a
# one-line change with a test behind it. Where the reading is contested the
# stricter option is taken: Germany (UWG §7 requires prior consent even B2B) and
# Australia (Spam Act 2003 is a consent regime) sit with Canada in ``OPT_IN``.
OPT_IN = "opt_in"
OPT_OUT = "opt_out"

# country -> (regime name shown to operators, consent model)
COUNTRY_CONSENT_REGIMES: dict[str, tuple[str, str]] = {
    "US": ("CAN-SPAM", OPT_OUT),
    "CA": ("CASL", OPT_IN),
    "GB": ("PECR", OPT_OUT),      # corporate subscribers are outside reg. 22
    "IN": ("IN-IT-RULES", OPT_OUT),
    "AU": ("SPAM-ACT", OPT_IN),
    "DE": ("UWG", OPT_IN),        # strictest of the set — consent even for B2B
    "FR": ("GDPR-B2B", OPT_OUT),  # CNIL: professional addresses, job-related
    "SG": ("PDPA", OPT_OUT),
    "AE": ("AE-CRA", OPT_OUT),
}


# --- Country name resolution ------------------------------------------------
#
# Sources do not agree on how to spell a country. Google Places returns ISO
# alpha-2 (``parse_place`` asks for the short form); Apollo returns a full
# name ("United States", "Germany"); a business card scanned by OCR returns
# whatever was printed on it.
#
# Both normalizers in this codebase handled that by slicing the first two
# characters, which is correct only for input that was already a code. On a
# full name it does not fail — it produces a *different, plausible* code:
#
#     "Germany"        -> "GE"   (not DE — GE is Georgia)
#     "Singapore"      -> "SI"   (not SG — SI is Slovenia)
#     "United Kingdom" -> "UN"
#     "United States"  -> "UN"
#
# None of those are in ``ALLOWED_COUNTRIES``, so the record is rejected with
# "country 'UN' is not supported" — an error that blames the source's data for
# a truncation we performed. The ones that happened to survive did so by
# coincidence of spelling: India, Canada, France and Australia all begin with
# their own code.
#
# Resolution is therefore explicit. An unrecognised value returns ``None``; what
# to do about it is the caller's decision, and the two callers differ.
COUNTRY_ALIASES: dict[str, str] = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "u.s.": "US", "u.s.a.": "US", "america": "US",
    "canada": "CA", "can": "CA",
    "india": "IN", "ind": "IN", "bharat": "IN",
    "united kingdom": "GB", "great britain": "GB", "britain": "GB",
    "uk": "GB", "u.k.": "GB", "england": "GB", "scotland": "GB",
    "wales": "GB", "northern ireland": "GB",
    "australia": "AU", "aus": "AU",
    "germany": "DE", "deutschland": "DE", "ger": "DE",
    "france": "FR", "fra": "FR",
    "singapore": "SG", "sgp": "SG",
    "united arab emirates": "AE", "uae": "AE", "u.a.e.": "AE",
}


def resolve_country(raw: str | None) -> str | None:
    """Best-effort ISO alpha-2 for a country as some source spelled it.

    Returns ``None`` when the value is empty or unrecognised — deliberately, so
    a caller cannot mistake a guess for a reading. Never truncates: a value that
    isn't already a supported code and isn't a known alias has no answer here.
    """
    if raw is None:
        return None
    value = str(raw).strip()
    if not value:
        return None
    upper = value.upper()
    if upper in ALLOWED_COUNTRIES:
        return upper
    return COUNTRY_ALIASES.get(value.lower())
