"""Re-backfill contact_group with a tighter 'test' email match.

Migration 031 matched ``test`` anywhere in the email, which wrongly flagged
genuine contacts whose domain merely *contains* the substring (e.g.
``rhart@granitestateprecision.com`` — "grani-TEST-ate"). This recomputes
``contacts.contact_group`` matching ``test`` only as a delimited token, in sync
with :func:`app.services.contact_grouping.classify_group`.

``industry_contacts`` are unaffected (all 'genuine').

Revision ID: 032_fix_contact_group_backfill
Revises: 031_contact_group
Create Date: 2026-06-29
"""
from alembic import op

revision = "032_fix_contact_group_backfill"
down_revision = "031_contact_group"
branch_labels = None
depends_on = None


# Unambiguous email substrings -> test. Keep in sync with
# app.services.contact_grouping._TEST_EMAIL_MARKERS.
_TEST_EMAIL_PATTERNS = ("%example.com%", "%example.org%", "%fake%", "%suman%", "%noreply%")
# "test" as a delimited token (POSIX regex), matching _TEST_TOKEN_RE.
_TEST_EMAIL_TOKEN = r"(^|[^a-z0-9])test([^a-z0-9]|$)"


def upgrade():
    email_clauses = " OR ".join(
        f"lower(coalesce(email, '')) LIKE '{p}'" for p in _TEST_EMAIL_PATTERNS
    )
    op.execute(
        f"""
        UPDATE contacts SET contact_group = CASE
          WHEN lower(coalesce(source, '')) LIKE 'test%' THEN 'test'
          WHEN lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) LIKE '%test%' THEN 'test'
          WHEN {email_clauses} THEN 'test'
          WHEN lower(coalesce(email, '')) ~ '{_TEST_EMAIL_TOKEN}' THEN 'test'
          ELSE 'genuine'
        END
        """
    )


def downgrade():
    # No-op: 031's (looser) backfill is the prior state and isn't worth recreating.
    pass
