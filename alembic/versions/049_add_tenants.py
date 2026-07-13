"""Add multi-tenant white-label platform tables (feature 4.1).

Creates ``tenants`` and ``tenant_usage`` and adds a nullable ``org_id`` column
to ``users`` (NULL = the default first-party tenant, so every existing user and
row keeps working unchanged).

Revision ID: 049_add_tenants
Revises: 048_merge_phase1_phase2
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "049_add_tenants"
down_revision = "048_merge_phase1_phase2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("industry", sa.String(80), nullable=True),
        sa.Column("config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("branding", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("billing_plan", sa.String(20), nullable=False, server_default="free"),
        sa.Column("ai_monthly_call_limit", sa.Integer, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "billing_plan IN ('free','starter','growth','enterprise')",
            name="ck_tenants_billing_plan",
        ),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)

    op.create_table(
        "tenant_usage",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("feature", sa.String(80), nullable=False),
        sa.Column("model", sa.String(60), nullable=True),
        sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("estimated_cost_usd", sa.Numeric(12, 6), nullable=False, server_default="0"),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_usage_tenant_id", "tenant_usage", ["tenant_id"])
    op.create_index("ix_tenant_usage_feature", "tenant_usage", ["feature"])
    op.create_index("ix_tenant_usage_created_at", "tenant_usage", ["created_at"])

    # Extend users with tenant membership.
    op.add_column(
        "users",
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_users_org_id_tenants", "users", "tenants", ["org_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_constraint("fk_users_org_id_tenants", "users", type_="foreignkey")
    op.drop_column("users", "org_id")
    op.drop_table("tenant_usage")
    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
