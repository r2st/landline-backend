"""Add indexes on contact/company columns and lost_reason to deals

Adds indexes on contacts.first_name, contacts.last_name, contacts.source,
companies.name, companies.stage, companies.country, companies.industry_segment
for faster filtering/sorting. Also adds deals.lost_reason column.

Revision ID: 021_indexes_and_deal_lost_reason
Revises: 020_ab_testing_and_scheduling
Create Date: 2026-06-23
"""
from alembic import op
import sqlalchemy as sa

revision = "021_indexes_and_deal_lost_reason"
down_revision = "020_ab_testing_and_scheduling"
branch_labels = None
depends_on = None


def upgrade():
    # Contact indexes (IF NOT EXISTS for idempotency)
    op.execute("CREATE INDEX IF NOT EXISTS ix_contacts_first_name ON contacts (first_name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_contacts_last_name ON contacts (last_name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_contacts_source ON contacts (source)")

    # Company indexes (IF NOT EXISTS for idempotency)
    op.execute("CREATE INDEX IF NOT EXISTS ix_companies_name ON companies (name)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_companies_stage ON companies (stage)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_companies_country ON companies (country)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_companies_industry_segment ON companies (industry_segment)")

    # Deal lost_reason column
    op.add_column("deals", sa.Column("lost_reason", sa.String(255), nullable=True))


def downgrade():
    op.drop_column("deals", "lost_reason")

    op.drop_index("ix_companies_industry_segment", table_name="companies")
    op.drop_index("ix_companies_country", table_name="companies")
    op.drop_index("ix_companies_stage", table_name="companies")
    op.drop_index("ix_companies_name", table_name="companies")

    op.drop_index("ix_contacts_source", table_name="contacts")
    op.drop_index("ix_contacts_last_name", table_name="contacts")
    op.drop_index("ix_contacts_first_name", table_name="contacts")
