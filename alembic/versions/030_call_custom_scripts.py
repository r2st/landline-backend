"""Add AI-generated custom-script columns to call_tasks.

Adds three nullable columns so each call task can cache a personalised,
AI-generated cold-call script:

- ``custom_script``       — the structured AI-generated script (JSONB)
- ``script_generated_at`` — when it was generated (for staleness/age display)
- ``script_context``      — the signals it was generated from (JSONB), so the
  script can be regenerated when the lead's situation changes.

Revision ID: 030_call_custom_scripts
Revises: 029_seed_call_research_docs
Create Date: 2026-06-28
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "030_call_custom_scripts"
down_revision = "029_seed_call_research_docs"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("call_tasks", sa.Column("custom_script", JSONB, nullable=True))
    op.add_column(
        "call_tasks",
        sa.Column("script_generated_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column("call_tasks", sa.Column("script_context", JSONB, nullable=True))


def downgrade():
    op.drop_column("call_tasks", "script_context")
    op.drop_column("call_tasks", "script_generated_at")
    op.drop_column("call_tasks", "custom_script")
