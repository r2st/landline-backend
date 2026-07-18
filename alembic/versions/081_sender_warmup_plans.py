"""Automated sender warm-up plans.

Chains off ``080_system_settings_per_org_unique``, the current head.

One row per sending account tracks its position on the graduated warm-up ramp
(5/day → 10 → 20 → 35 → 50 → graduated) so a brand-new mailbox never dumps full
cold volume. Mirrors ``sender_health``: a bare ``account_id`` + ``account_type``
pair (no FK, since the parent is either ``gmail_accounts`` or ``smtp_accounts``),
one plan per account.

Org-scoped like every tenant table: ``org_id`` FK → organizations, NOT NULL with
the first-party Apprend org as the ``server_default`` safety net for raw inserts.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "081_sender_warmup_plans"
down_revision = "080_system_settings_per_org_unique"
branch_labels = None
depends_on = None

# The first-party "Apprend Technologies" org id (see app.org_context.APPREND_ORG_ID).
_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "sender_warmup_plans",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_type", sa.String(10), nullable=False, server_default="gmail"),
        sa.Column("account_email", sa.String(255), nullable=True),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("current_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("status", sa.String(20), nullable=False, server_default="warming"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("account_type", "account_id", name="uq_sender_warmup_account"),
    )
    op.create_index(
        "ix_sender_warmup_plans_account_id", "sender_warmup_plans", ["account_id"],
    )
    op.create_index(
        "ix_sender_warmup_plans_org_id", "sender_warmup_plans", ["org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sender_warmup_plans_org_id", table_name="sender_warmup_plans")
    op.drop_index("ix_sender_warmup_plans_account_id", table_name="sender_warmup_plans")
    op.drop_table("sender_warmup_plans")
