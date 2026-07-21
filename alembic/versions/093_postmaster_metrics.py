"""Google Postmaster Tools integration (P2 audit).

Chains off ``092_channel_step_executions``.

Adds two tables:

* ``postmaster_accounts`` — OAuth connection (postmaster.readonly) to a Google
  account that has verified domains in Postmaster Tools; tokens encrypted at rest.
* ``postmaster_metrics`` — one row per (domain, date) of pulled metrics
  (reputation, user-reported spam rate, SPF/DKIM/DMARC pass rates, TLS ratios,
  delivery errors), upserted daily. Gmail exposes no feedback loop, so this is
  the only complaint/reputation signal for the primary transport.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "093_postmaster_metrics"
down_revision = "092_channel_step_executions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "postmaster_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("google_sub", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("token_expiry", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="connected"),
        sa.Column("last_pulled_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("connected_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("email", name="uq_postmaster_accounts_email"),
        sa.UniqueConstraint("google_sub", name="uq_postmaster_accounts_google_sub"),
    )
    op.create_index("ix_postmaster_accounts_org_id", "postmaster_accounts", ["org_id"])
    op.create_index("ix_postmaster_accounts_user_id", "postmaster_accounts", ["user_id"])

    op.create_table(
        "postmaster_metrics",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=True), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column("domain_reputation", sa.String(length=40), nullable=True),
        sa.Column("ip_reputations", JSONB(), nullable=False, server_default="[]"),
        sa.Column("spam_rate", sa.Float(), nullable=True),
        sa.Column("dkim_success_rate", sa.Float(), nullable=True),
        sa.Column("spf_success_rate", sa.Float(), nullable=True),
        sa.Column("dmarc_success_rate", sa.Float(), nullable=True),
        sa.Column("inbound_encryption_ratio", sa.Float(), nullable=True),
        sa.Column("outbound_encryption_ratio", sa.Float(), nullable=True),
        sa.Column("delivery_errors", JSONB(), nullable=False, server_default="[]"),
        sa.Column("raw", JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("domain", "metric_date", name="uq_postmaster_domain_date"),
    )
    op.create_index("ix_postmaster_metrics_org_id", "postmaster_metrics", ["org_id"])
    op.create_index("ix_postmaster_metrics_domain", "postmaster_metrics", ["domain"])
    op.create_index("ix_postmaster_metrics_metric_date", "postmaster_metrics", ["metric_date"])


def downgrade() -> None:
    op.drop_index("ix_postmaster_metrics_metric_date", table_name="postmaster_metrics")
    op.drop_index("ix_postmaster_metrics_domain", table_name="postmaster_metrics")
    op.drop_index("ix_postmaster_metrics_org_id", table_name="postmaster_metrics")
    op.drop_table("postmaster_metrics")
    op.drop_index("ix_postmaster_accounts_user_id", table_name="postmaster_accounts")
    op.drop_index("ix_postmaster_accounts_org_id", table_name="postmaster_accounts")
    op.drop_table("postmaster_accounts")
