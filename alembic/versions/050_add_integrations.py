"""Add integration marketplace tables (feature 4.2).

Creates ``integrations`` (a tenant's configured provider connections),
``webhook_subscriptions`` (outbound event delivery targets), and
``webhook_events`` (per-delivery audit/retry records).

Revision ID: 050_add_integrations
Revises: 049_add_tenants
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "050_add_integrations"
down_revision = "049_add_tenants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integrations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("provider", sa.String(60), nullable=False),
        sa.Column("auth_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="configured"),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("credentials", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("last_synced_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("org_id", "provider", name="uq_integration_org_provider"),
    )
    op.create_index("ix_integrations_org_id", "integrations", ["org_id"])
    op.create_index("ix_integrations_provider", "integrations", ["provider"])

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("target_url", sa.String(1000), nullable=False),
        sa.Column("event_types", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("secret", sa.Text, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_webhook_subscriptions_org_id", "webhook_subscriptions", ["org_id"])

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("response_code", sa.Integer, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_webhook_events_subscription_id", "webhook_events", ["subscription_id"])
    op.create_index("ix_webhook_events_event_type", "webhook_events", ["event_type"])
    op.create_index("ix_webhook_events_status", "webhook_events", ["status"])


def downgrade() -> None:
    op.drop_table("webhook_events")
    op.drop_table("webhook_subscriptions")
    op.drop_index("ix_integrations_provider", table_name="integrations")
    op.drop_index("ix_integrations_org_id", table_name="integrations")
    op.drop_table("integrations")
