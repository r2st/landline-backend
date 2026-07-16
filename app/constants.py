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
