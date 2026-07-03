"""Add sentiment analysis, A/B testing experiments, and email test exchanges.

Revision ID: 034_sentiment_testing
Revises: 033_add_investor_system
Create Date: 2026-07-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "034_sentiment_testing"
down_revision = "033_add_investor_system"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Email sentiment scores
    op.create_table(
        "email_sentiment_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("email_reply_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("email_replies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("sentiment_score", sa.Float, nullable=False),
        sa.Column("sentiment_label", sa.String(20), nullable=False),
        sa.Column("intent", sa.String(30), nullable=False),
        sa.Column("intent_confidence", sa.Float, default=0.0),
        sa.Column("emotions", postgresql.JSONB, server_default="{}"),
        sa.Column("buying_signals", postgresql.JSONB, server_default="[]"),
        sa.Column("objections", postgresql.JSONB, server_default="[]"),
        sa.Column("recommended_action", sa.String(50), nullable=True),
        sa.Column("action_reasoning", sa.Text, nullable=True),
        sa.Column("analyzed_text", sa.Text, nullable=True),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_sentiment_email_reply_id", "email_sentiment_scores", ["email_reply_id"])
    op.create_index("ix_sentiment_contact_id", "email_sentiment_scores", ["contact_id"])
    op.create_index("ix_sentiment_company_id", "email_sentiment_scores", ["company_id"])
    op.create_index("ix_sentiment_intent", "email_sentiment_scores", ["intent"])
    op.create_index("ix_sentiment_created_at", "email_sentiment_scores", ["created_at"])

    # A/B test experiments
    op.create_table(
        "ab_test_experiments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("sequence_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("outreach_sequences.id", ondelete="SET NULL"), nullable=True),
        sa.Column("step_number", sa.Integer, nullable=True),
        sa.Column("variant_a_subject", sa.Text, nullable=False),
        sa.Column("variant_b_subject", sa.Text, nullable=False),
        sa.Column("variant_a_sent", sa.Integer, server_default="0"),
        sa.Column("variant_a_opened", sa.Integer, server_default="0"),
        sa.Column("variant_a_clicked", sa.Integer, server_default="0"),
        sa.Column("variant_a_replied", sa.Integer, server_default="0"),
        sa.Column("variant_b_sent", sa.Integer, server_default="0"),
        sa.Column("variant_b_opened", sa.Integer, server_default="0"),
        sa.Column("variant_b_clicked", sa.Integer, server_default="0"),
        sa.Column("variant_b_replied", sa.Integer, server_default="0"),
        sa.Column("winner", sa.String(1), nullable=True),
        sa.Column("confidence_level", sa.Float, nullable=True),
        sa.Column("is_significant", sa.Boolean, server_default="false"),
        sa.Column("status", sa.String(20), server_default="'running'"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )

    # Email test exchanges
    op.create_table(
        "email_test_exchanges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("scenario", sa.String(50), nullable=False),
        sa.Column("messages", postgresql.JSONB, server_default="[]"),
        sa.Column("industry", sa.String(100), nullable=True),
        sa.Column("persona_name", sa.String(100), nullable=True),
        sa.Column("persona_title", sa.String(100), nullable=True),
        sa.Column("persona_company", sa.String(200), nullable=True),
        sa.Column("total_messages", sa.Integer, server_default="0"),
        sa.Column("avg_sentiment", sa.Float, nullable=True),
        sa.Column("outcome", sa.String(50), nullable=True),
        sa.Column("status", sa.String(20), server_default="'active'"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("email_test_exchanges")
    op.drop_table("ab_test_experiments")
    op.drop_index("ix_sentiment_created_at", table_name="email_sentiment_scores")
    op.drop_index("ix_sentiment_intent", table_name="email_sentiment_scores")
    op.drop_index("ix_sentiment_company_id", table_name="email_sentiment_scores")
    op.drop_index("ix_sentiment_contact_id", table_name="email_sentiment_scores")
    op.drop_index("ix_sentiment_email_reply_id", table_name="email_sentiment_scores")
    op.drop_table("email_sentiment_scores")
