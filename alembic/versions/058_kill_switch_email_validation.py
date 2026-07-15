"""Global sending kill switch + email validation.

Adds the ``system_settings`` key/value table (home of the ``sending_enabled``
kill switch and the persisted warm-up start date), a ``kill_switch_paused``
marker on outreach enrollments so a re-enable resumes only kill-switch-paused
enrollments, and per-contact email-validation columns (MX check result).

Revision ID: 058_kill_switch_email_validation
Revises: 057_agent_daily_summaries
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "058_kill_switch_email_validation"
down_revision = "057_agent_daily_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("key", sa.String(100), nullable=False),
        sa.Column("value", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_unique_constraint("uq_system_settings_key", "system_settings", ["key"])
    op.create_index("ix_system_settings_key", "system_settings", ["key"])

    op.add_column(
        "outreach_enrollments",
        sa.Column("kill_switch_paused", sa.Boolean(), nullable=False, server_default="false"),
    )

    # Per-contact email validation (feature: pre-send MX check).
    # email_status: NULL (unchecked) | 'valid' | 'invalid' | 'unknown'
    op.add_column("contacts", sa.Column("email_status", sa.String(10), nullable=True))
    op.add_column("contacts", sa.Column("email_checked_at", sa.TIMESTAMP(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("contacts", "email_checked_at")
    op.drop_column("contacts", "email_status")
    op.drop_column("outreach_enrollments", "kill_switch_paused")
    op.drop_index("ix_system_settings_key", table_name="system_settings")
    op.drop_constraint("uq_system_settings_key", "system_settings", type_="unique")
    op.drop_table("system_settings")
