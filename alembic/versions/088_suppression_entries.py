"""Global do-not-contact suppression list.

Chains off ``087_sender_warmup_plans``, the current head.

One row per suppressed email per org, consulted by every outreach path before a
send (Feature 2). Emails are stored lower-cased; a unique ``(org_id, email)``
keeps the list de-duplicated per tenant. Org-scoped like every tenant table.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "088_suppression_entries"
down_revision = "087_sender_warmup_plans"
branch_labels = None
depends_on = None

_APPREND_ORG_ID = "a9910000-0000-4000-8000-000000000001"


def upgrade() -> None:
    op.create_table(
        "suppression_entries",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False, server_default=_APPREND_ORG_ID,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("reason", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column(
            "created_at", sa.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "email", name="uq_suppression_org_email"),
        sa.CheckConstraint(
            "reason IN ('unsubscribe','bounce','complaint','manual')",
            name="ck_suppression_reason",
        ),
        sa.CheckConstraint(
            "source IN ('import','auto','manual')",
            name="ck_suppression_source",
        ),
    )
    op.create_index("ix_suppression_entries_email", "suppression_entries", ["email"])
    op.create_index("ix_suppression_entries_org_id", "suppression_entries", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_suppression_entries_org_id", table_name="suppression_entries")
    op.drop_index("ix_suppression_entries_email", table_name="suppression_entries")
    op.drop_table("suppression_entries")
