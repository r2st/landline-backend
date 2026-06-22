"""Add pipeline_stage (kanban deal-board stage) to companies

Adds a dedicated kanban pipeline stage to companies, distinct from the existing
lifecycle ``stage`` column. Drives the deal-board view:
new_lead → researching → outreach → engaged → negotiating → client / churned.

Revision ID: 012_add_pipeline_stage
Revises: 011_add_contact_card_fields
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "012_add_pipeline_stage"
down_revision = "011_add_contact_card_fields"
branch_labels = None
depends_on = None

_VALID = "('new_lead','researching','outreach','engaged','negotiating','client','churned')"


def upgrade():
    op.add_column(
        "companies",
        sa.Column(
            "pipeline_stage",
            sa.String(20),
            nullable=False,
            server_default="new_lead",
        ),
    )
    op.create_check_constraint(
        "ck_companies_pipeline_stage",
        "companies",
        f"pipeline_stage IN {_VALID}",
    )
    op.create_index("ix_companies_pipeline_stage", "companies", ["pipeline_stage"])


def downgrade():
    op.drop_index("ix_companies_pipeline_stage", table_name="companies")
    op.drop_constraint("ck_companies_pipeline_stage", "companies", type_="check")
    op.drop_column("companies", "pipeline_stage")
