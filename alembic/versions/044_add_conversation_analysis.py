"""Add conversation_analyses table (feature 1.5 Conversation Intelligence).

Revision ID: 044_conversation_analysis
Revises: 043_company_research
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "044_conversation_analysis"
down_revision = "043_company_research"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("gmail_thread_id", sa.String(255), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("momentum_score", sa.Float, nullable=False, server_default="50"),
        sa.Column("momentum_label", sa.String(20), nullable=False, server_default="'stable'"),
        sa.Column("stakeholder_map", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("unresolved_objections", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("recommended_action", sa.Text, nullable=True),
        sa.Column("next_message", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("signals", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("analyzed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_analyses_thread_id", "conversation_analyses", ["gmail_thread_id"])
    op.create_index("ix_conversation_analyses_company_id", "conversation_analyses", ["company_id"])
    op.create_index("ix_conversation_analyses_contact_id", "conversation_analyses", ["contact_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_analyses_contact_id", table_name="conversation_analyses")
    op.drop_index("ix_conversation_analyses_company_id", table_name="conversation_analyses")
    op.drop_index("ix_conversation_analyses_thread_id", table_name="conversation_analyses")
    op.drop_table("conversation_analyses")
