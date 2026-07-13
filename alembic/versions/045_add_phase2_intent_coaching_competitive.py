"""Add Phase 2 tables: intent signals, rep performance, competitors + mentions.

Covers features:
* 2.1 Intent Signal Detection  → intent_signals
* 2.4 AI Sales Coach           → rep_performance
* 2.6 Competitive Intelligence → competitors, competitor_mentions

Revision ID: 045_phase2_intent_coaching_competitive
Revises: 044_conversation_analysis
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "045_phase2_intent_coaching_competitive"
down_revision = "044_conversation_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- 2.1 Intent signals ---
    op.create_table(
        "intent_signals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("signal_type", sa.String(40), nullable=False),
        sa.Column("source", sa.String(200), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("url", sa.String(1000), nullable=True),
        sa.Column("raw_data", postgresql.JSONB, nullable=True),
        sa.Column("ai_analysis", postgresql.JSONB, nullable=True),
        sa.Column("relevance_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("urgency_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("buying_window_days", sa.Integer, nullable=True),
        sa.Column("recommended_action", sa.Text, nullable=True),
        sa.Column("outreach_angle", sa.Text, nullable=True),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("actioned", sa.Boolean, nullable=False, server_default=sa.text("false")),
    )
    op.create_index("ix_intent_signals_company_id", "intent_signals", ["company_id"])
    op.create_index("ix_intent_signals_signal_type", "intent_signals", ["signal_type"])
    op.create_index("ix_intent_signals_expires_at", "intent_signals", ["expires_at"])

    # --- 2.4 Rep performance ---
    op.create_table(
        "rep_performance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("period_type", sa.String(20), nullable=False, server_default="weekly"),
        sa.Column("period_start", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("period_end", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("metrics", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("ai_insights", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("coaching_recommendations", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("model_used", sa.String(50), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "period_type", "period_start", name="uq_rep_performance_period"),
    )
    op.create_index("ix_rep_performance_user_id", "rep_performance", ["user_id"])
    op.create_index("ix_rep_performance_period_start", "rep_performance", ["period_start"])

    # --- 2.6 Competitors ---
    op.create_table(
        "competitors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("domain", sa.String(255), nullable=True),
        sa.Column("aliases", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("products", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("strengths", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("weaknesses", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("battle_card", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_updated", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_competitors_name", "competitors", ["name"])

    # --- 2.6 Competitor mentions ---
    op.create_table(
        "competitor_mentions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("competitor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("competitors.id", ondelete="CASCADE"), nullable=False),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("companies.id", ondelete="CASCADE"), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("deals.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="email"),
        sa.Column("context", sa.Text, nullable=True),
        sa.Column("sentiment", sa.String(20), nullable=True),
        sa.Column("detected_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_competitor_mentions_competitor_id", "competitor_mentions", ["competitor_id"])
    op.create_index("ix_competitor_mentions_company_id", "competitor_mentions", ["company_id"])
    op.create_index("ix_competitor_mentions_source", "competitor_mentions", ["source"])


def downgrade() -> None:
    op.drop_table("competitor_mentions")
    op.drop_table("competitors")
    op.drop_table("rep_performance")
    op.drop_table("intent_signals")
