"""Add contact_group to contacts and industry_contacts.

Separates genuine business contacts from internal/test data so the UI and
outreach can filter them apart. Adds a nullable ``contact_group`` column
(values: ``'genuine'``, ``'test'``, or NULL) to both ``contacts`` and
``industry_contacts``, then backfills existing rows with a heuristic:

- ``industry_contacts`` — all rows are real seeded manufacturing leads -> 'genuine'.
- ``contacts`` — rows whose source/email/name carry test markers (the
  developer's own gmail / apprend addresses, a ``test*`` source, ``test`` in the
  name, or ``test`` / ``example.com`` / ``fake`` in the email) -> 'test';
  everything else -> 'genuine'.

The heuristic is kept in sync with :func:`app.services.contact_grouping.classify_group`,
which classifies contacts created after this migration (e.g. via quick-add).

Revision ID: 031_contact_group
Revises: 030_call_custom_scripts
Create Date: 2026-06-29
"""
from alembic import op
import sqlalchemy as sa

revision = "031_contact_group"
down_revision = "030_call_custom_scripts"
branch_labels = None
depends_on = None


# Email substrings that mark a row as test/internal data. Keep in sync with
# app.services.contact_grouping._TEST_EMAIL_MARKERS.
_TEST_EMAIL_PATTERNS = (
    "%test%", "%example.com%", "%example.org%", "%fake%", "%suman%", "%noreply%",
)


def upgrade():
    op.add_column("contacts", sa.Column("contact_group", sa.String(20), nullable=True))
    op.add_column("industry_contacts", sa.Column("contact_group", sa.String(20), nullable=True))

    # industry_contacts are all real seeded leads.
    op.execute("UPDATE industry_contacts SET contact_group = 'genuine'")

    # contacts: flag test rows, default the rest to genuine.
    email_clauses = " OR ".join(
        f"lower(coalesce(email, '')) LIKE '{p}'" for p in _TEST_EMAIL_PATTERNS
    )
    op.execute(
        f"""
        UPDATE contacts SET contact_group = CASE
          WHEN lower(coalesce(source, '')) LIKE 'test%' THEN 'test'
          WHEN lower(coalesce(first_name, '') || ' ' || coalesce(last_name, '')) LIKE '%test%' THEN 'test'
          WHEN {email_clauses} THEN 'test'
          ELSE 'genuine'
        END
        """
    )

    op.create_index("ix_contacts_contact_group", "contacts", ["contact_group"])
    op.create_index(
        "ix_industry_contacts_contact_group", "industry_contacts", ["contact_group"]
    )


def downgrade():
    op.drop_index("ix_industry_contacts_contact_group", table_name="industry_contacts")
    op.drop_index("ix_contacts_contact_group", table_name="contacts")
    op.drop_column("industry_contacts", "contact_group")
    op.drop_column("contacts", "contact_group")
