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
    # Contact indexes
    op.create_index("ix_contacts_first_name", "contacts", ["first_name"])
    op.create_index("ix_contacts_last_name", "contacts", ["last_name"])
    op.create_index("ix_contacts_source", "contacts", ["source"])

    # Company indexes
    op.create_index("ix_companies_name", "companies", ["name"])
    op.create_index("ix_companies_stage", "companies", ["stage"])
    op.create_index("ix_companies_country", "companies", ["country"])
    op.create_index("ix_companies_industry_segment", "companies", ["industry_segment"])

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
