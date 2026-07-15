"""Add agent_daily_summaries table (daily autonomous-activity digest).

Revision ID: 057_agent_daily_summaries
Revises: 056_autonomous_lead_gen
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "057_agent_daily_summaries"
down_revision = "056_autonomous_lead_gen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_daily_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("summary_date", sa.Date, nullable=False),
        sa.Column("metrics", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("narrative", sa.Text, nullable=False, server_default=""),
        sa.Column("emails_sent", sa.Integer, nullable=False, server_default="0"),
        sa.Column("leads_discovered", sa.Integer, nullable=False, server_default="0"),
        sa.Column("replies_received", sa.Integer, nullable=False, server_default="0"),
        sa.Column("bounces_received", sa.Integer, nullable=False, server_default="0"),
        sa.Column("follow_ups_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("calls_queued", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint(
        "uq_agent_daily_summaries_date", "agent_daily_summaries", ["summary_date"]
    )
    op.create_index(
        "ix_agent_daily_summaries_summary_date", "agent_daily_summaries", ["summary_date"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_daily_summaries_summary_date", table_name="agent_daily_summaries")
    op.drop_constraint("uq_agent_daily_summaries_date", "agent_daily_summaries", type_="unique")
    op.drop_table("agent_daily_summaries")
