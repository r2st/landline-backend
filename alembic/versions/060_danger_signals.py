"""Feature 2 — Auto-Pause on Danger Signals: danger_signal_events table.

Records one row per auto-pause trip (or, if ever persisted, a warning) with the
metric that fired, the scope (global / single account), the observed value vs the
threshold, the sample size, a JSONB snapshot of contributing sends, and the
manual-resume resolution fields.

Revision ID: 060_danger_signals
Revises: 059_launch_sender_defaults
Create Date: 2026-07-16

Chains off the last COMMITTED head (``059_launch_sender_defaults``). Sibling
Dialtone features (reply-classification, domain-health) are being developed
concurrently and each branch off 059 as well, so ``alembic heads`` will show a
branch until they land — that's expected under the concurrent-push workflow and
is reconciled with a merge migration at integration. Depending on committed
history (not a peer's in-flight file) keeps this migration stable regardless of
which sibling lands first.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "060_danger_signals"
down_revision = "059_launch_sender_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "danger_signal_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("metric", sa.String(30), nullable=False),
        sa.Column("scope", sa.String(10), nullable=False, server_default="global"),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("account_email", sa.String(255), nullable=True),
        sa.Column("window", sa.String(10), nullable=False, server_default="24h"),
        sa.Column("observed_value", sa.Float, nullable=False, server_default="0"),
        sa.Column("threshold", sa.Float, nullable=False, server_default="0"),
        sa.Column("sample_size", sa.Integer, nullable=False, server_default="0"),
        sa.Column("action_taken", sa.String(20), nullable=False, server_default="auto_paused"),
        sa.Column("details", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.Column("resolve_note", sa.Text, nullable=True),
    )
    op.create_index("ix_danger_signal_events_triggered_at", "danger_signal_events", ["triggered_at"])
    op.create_index("ix_danger_signal_events_resolved_at", "danger_signal_events", ["resolved_at"])


def downgrade() -> None:
    op.drop_index("ix_danger_signal_events_resolved_at", table_name="danger_signal_events")
    op.drop_index("ix_danger_signal_events_triggered_at", table_name="danger_signal_events")
    op.drop_table("danger_signal_events")
