"""Add suggestion_outcomes table for the smart-suggestions feedback loop (Feature 1.6).

Revision ID: 047_suggestion_outcomes
Revises: 046_lead_prioritization
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "047_suggestion_outcomes"
down_revision = "046_lead_prioritization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suggestion_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("suggestion_key", sa.String(255), nullable=False),
        sa.Column("suggestion_type", sa.String(50), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(60), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="suggested"),
        sa.Column("outcome", sa.String(50), nullable=True),
        sa.Column("payload", postgresql.JSONB, server_default="{}"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('suggested','accepted','dismissed','completed')",
            name="ck_suggestion_outcomes_status",
        ),
    )
    op.create_index("ix_suggestion_outcomes_user_id", "suggestion_outcomes", ["user_id"])
    op.create_index("ix_suggestion_outcomes_key", "suggestion_outcomes", ["suggestion_key"])
    op.create_index("ix_suggestion_outcomes_type", "suggestion_outcomes", ["suggestion_type"])
    op.create_index("ix_suggestion_outcomes_company_id", "suggestion_outcomes", ["company_id"])
    op.create_index("ix_suggestion_outcomes_status", "suggestion_outcomes", ["status"])
    op.create_index("ix_suggestion_outcomes_created_at", "suggestion_outcomes", ["created_at"])


def downgrade() -> None:
    for ix in (
        "ix_suggestion_outcomes_created_at",
        "ix_suggestion_outcomes_status",
        "ix_suggestion_outcomes_company_id",
        "ix_suggestion_outcomes_type",
        "ix_suggestion_outcomes_key",
        "ix_suggestion_outcomes_user_id",
    ):
        op.drop_index(ix, table_name="suggestion_outcomes")
    op.drop_table("suggestion_outcomes")
