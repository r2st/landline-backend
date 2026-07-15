"""Autonomous lead-gen enhancements.

Adds:
* contacts.timezone            — per-contact IANA tz for timezone-aware sending
* contacts.bounce_count        — soft/hard bounce counter
* contacts.last_bounce_type    — 'hard' | 'soft'
* contacts.last_bounced_at     — last bounce timestamp
* sequence_steps.send_condition — declarative conditional branching gate

All columns are nullable or defaulted, so the migration is safe and reversible
with no backfill.

Revision ID: 056_autonomous_lead_gen
Revises: 055_phase2_orchestration_relationship_meeting
Create Date: 2026-07-15
"""

from alembic import op
import sqlalchemy as sa

revision = "056_autonomous_lead_gen"
down_revision = "055_phase2_orchestration_relationship_meeting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contacts", sa.Column("timezone", sa.String(64), nullable=True))
    op.add_column(
        "contacts",
        sa.Column("bounce_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("contacts", sa.Column("last_bounce_type", sa.String(10), nullable=True))
    op.add_column(
        "contacts",
        sa.Column("last_bounced_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.add_column(
        "sequence_steps",
        sa.Column(
            "send_condition",
            sa.String(20),
            nullable=False,
            server_default="always",
        ),
    )


def downgrade() -> None:
    op.drop_column("sequence_steps", "send_condition")
    op.drop_column("contacts", "last_bounced_at")
    op.drop_column("contacts", "last_bounce_type")
    op.drop_column("contacts", "bounce_count")
    op.drop_column("contacts", "timezone")
