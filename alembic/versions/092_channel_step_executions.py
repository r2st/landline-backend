"""Multi-channel step execution tracking (P2 audit — multichannel execution).

Chains off ``091_contact_anonymized_at`` (the current head).

Adds ``channel_step_executions``: one row per attempted touch on any channel
(email real; LinkedIn / phone / SMS via provider stubs), recording what was sent,
through which provider, and how it landed — so a lead's cross-channel history is
one auditable timeline and per-touch status/reply tracking has a home.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "092_channel_step_executions"
down_revision = "091_contact_anonymized_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_step_executions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("enrollment_id", UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", UUID(as_uuid=True), nullable=True),
        sa.Column("step_number", sa.Integer(), nullable=True),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=40), nullable=False, server_default="message"),
        sa.Column("provider", sa.String(length=40), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("payload", JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", JSONB(), nullable=False, server_default="{}"),
        sa.Column("reply", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status_checked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enrollment_id"], ["outreach_enrollments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_channel_step_executions_org_id", "channel_step_executions", ["org_id"]
    )
    op.create_index(
        "ix_channel_step_executions_enrollment_id",
        "channel_step_executions",
        ["enrollment_id"],
    )
    op.create_index(
        "ix_channel_step_executions_contact_id",
        "channel_step_executions",
        ["contact_id"],
    )
    op.create_index(
        "ix_channel_step_executions_channel", "channel_step_executions", ["channel"]
    )
    op.create_index(
        "ix_channel_step_executions_status", "channel_step_executions", ["status"]
    )


def downgrade() -> None:
    op.drop_index("ix_channel_step_executions_status", table_name="channel_step_executions")
    op.drop_index("ix_channel_step_executions_channel", table_name="channel_step_executions")
    op.drop_index("ix_channel_step_executions_contact_id", table_name="channel_step_executions")
    op.drop_index("ix_channel_step_executions_enrollment_id", table_name="channel_step_executions")
    op.drop_index("ix_channel_step_executions_org_id", table_name="channel_step_executions")
    op.drop_table("channel_step_executions")
