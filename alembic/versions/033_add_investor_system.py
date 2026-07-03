"""Add investor-outreach system: investors, investor_interactions, investor_documents

Revision ID: 033_add_investor_system
Revises: 032_fix_contact_group_backfill
Create Date: 2026-07-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "033_add_investor_system"
down_revision = "032_fix_contact_group_backfill"
branch_labels = None
depends_on = None


def upgrade():
    # --- investors ---
    op.create_table(
        "investors",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("firm", sa.String(255), nullable=True),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("linkedin", sa.String(500), nullable=True),
        sa.Column("website", sa.String(500), nullable=True),
        sa.Column("investment_focus", JSONB, nullable=True),
        sa.Column("investment_stage", sa.String(20), nullable=True),
        sa.Column("typical_check_size_min", sa.Integer, nullable=True),
        sa.Column("typical_check_size_max", sa.Integer, nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="researching"),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("last_contacted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('researching','contacted','interested','meeting_scheduled',"
            "'due_diligence','committed','passed')",
            name="ck_investors_status",
        ),
        sa.CheckConstraint(
            "investment_stage IS NULL OR investment_stage IN "
            "('seed','series-a','series-b','growth')",
            name="ck_investors_stage",
        ),
    )
    op.create_index("ix_investors_name", "investors", ["name"])
    op.create_index("ix_investors_firm", "investors", ["firm"])
    op.create_index("ix_investors_email", "investors", ["email"])
    op.create_index("ix_investors_investment_stage", "investors", ["investment_stage"])
    op.create_index("ix_investors_status", "investors", ["status"])
    op.create_index("ix_investors_deleted_at", "investors", ["deleted_at"])

    # --- investor_interactions ---
    op.create_table(
        "investor_interactions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False, server_default="outbound"),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("next_steps", sa.Text, nullable=True),
        sa.Column("interaction_date", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('email','call','meeting','linkedin')",
            name="ck_investor_interactions_type",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound','outbound')",
            name="ck_investor_interactions_direction",
        ),
    )
    op.create_index("ix_investor_interactions_investor_id", "investor_interactions", ["investor_id"])
    op.create_index("ix_investor_interactions_type", "investor_interactions", ["type"])
    op.create_index("ix_investor_interactions_interaction_date", "investor_interactions", ["interaction_date"])

    # --- investor_documents ---
    op.create_table(
        "investor_documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "investor_id",
            UUID(as_uuid=True),
            sa.ForeignKey("investors.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("type", sa.String(30), nullable=False, server_default="other"),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("file_path", sa.String(1000), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "type IN ('pitch_deck','financial_model','term_sheet','cap_table',"
            "'business_plan','other')",
            name="ck_investor_documents_type",
        ),
    )
    op.create_index("ix_investor_documents_investor_id", "investor_documents", ["investor_id"])
    op.create_index("ix_investor_documents_type", "investor_documents", ["type"])


def downgrade():
    op.drop_index("ix_investor_documents_type", table_name="investor_documents")
    op.drop_index("ix_investor_documents_investor_id", table_name="investor_documents")
    op.drop_table("investor_documents")

    op.drop_index("ix_investor_interactions_interaction_date", table_name="investor_interactions")
    op.drop_index("ix_investor_interactions_type", table_name="investor_interactions")
    op.drop_index("ix_investor_interactions_investor_id", table_name="investor_interactions")
    op.drop_table("investor_interactions")

    op.drop_index("ix_investors_deleted_at", table_name="investors")
    op.drop_index("ix_investors_status", table_name="investors")
    op.drop_index("ix_investors_investment_stage", table_name="investors")
    op.drop_index("ix_investors_email", table_name="investors")
    op.drop_index("ix_investors_firm", table_name="investors")
    op.drop_index("ix_investors_name", table_name="investors")
    op.drop_table("investors")
