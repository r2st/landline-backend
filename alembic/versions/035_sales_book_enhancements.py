"""Sales-book enhancements: framework selection, discovery scorecard,
objection tracking, optimal call windows, and multi-channel cadence.

Adds columns derived from 15+ sales books research:
- call_tasks: sales_framework, discovery_scorecard, objections_encountered,
  optimal_call_window
- outreach_sequences: cadence_pattern (Triple/Quad/Quint from Combo Prospecting)
- sequence_steps: channel (email/call/linkedin/video/direct_mail)

Revision ID: 035_sales_book
Revises: 034_sentiment_testing
Create Date: 2026-07-02
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "035_sales_book"
down_revision = "034_sentiment_testing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- call_tasks: sales methodology tracking ---
    op.add_column(
        "call_tasks",
        sa.Column("sales_framework", sa.String(30), nullable=True),
    )
    op.add_column(
        "call_tasks",
        sa.Column("discovery_scorecard", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "call_tasks",
        sa.Column("objections_encountered", postgresql.JSONB, nullable=True),
    )
    op.add_column(
        "call_tasks",
        sa.Column("optimal_call_window", sa.String(30), nullable=True),
    )

    # --- outreach_sequences: multi-channel cadence (Combo Prospecting) ---
    op.add_column(
        "outreach_sequences",
        sa.Column(
            "cadence_pattern",
            sa.String(20),
            nullable=True,
            server_default="single_channel",
        ),
    )

    # --- sequence_steps: channel for multi-channel orchestration ---
    op.add_column(
        "sequence_steps",
        sa.Column(
            "channel",
            sa.String(20),
            nullable=False,
            server_default="email",
        ),
    )


def downgrade() -> None:
    op.drop_column("sequence_steps", "channel")
    op.drop_column("outreach_sequences", "cadence_pattern")
    op.drop_column("call_tasks", "optimal_call_window")
    op.drop_column("call_tasks", "objections_encountered")
    op.drop_column("call_tasks", "discovery_scorecard")
    op.drop_column("call_tasks", "sales_framework")
