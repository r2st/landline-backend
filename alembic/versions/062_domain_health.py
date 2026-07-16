"""Feature 5 — Domain Health Dashboard: domain_health table.

Revision ID: 062_domain_health
Revises: 059_launch_sender_defaults
Create Date: 2026-07-16

One row per sending domain (deduplicated across Gmail/SMTP accounts). Stores the
latest SPF/DKIM/DMARC verdicts, DNSBL blacklist results, a composite 0-100
health score + band, and the prior score for change-only alerting.

NOTE on chaining: Features 1-5 are being built in parallel, so several migrations
branch off the committed head ``059_launch_sender_defaults`` (Feature 1's
``060_reply_classification`` and Feature 2's ``060_danger_signals`` also chain off
059). This migration deliberately anchors to the stable committed head rather
than another feature's in-flight migration; the resulting multiple heads are
reconciled by an Alembic merge migration once the parallel features land.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "062_domain_health"
down_revision = "059_launch_sender_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "domain_health",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("spf", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("dkim", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("dmarc", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("blacklists", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("health_score", sa.Integer, nullable=False, server_default="0"),
        sa.Column("band", sa.String(20), nullable=False, server_default="at_risk"),
        sa.Column("coverage", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("last_checked", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("previous_score", sa.Integer, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("domain", name="uq_domain_health_domain"),
    )
    op.create_index("ix_domain_health_domain", "domain_health", ["domain"])


def downgrade() -> None:
    op.drop_index("ix_domain_health_domain", table_name="domain_health")
    op.drop_table("domain_health")
