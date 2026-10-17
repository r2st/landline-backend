"""Autopilot Outreach campaigns.

Revision ID: 124_autopilot_campaigns
Revises: 123_discovery_documents

Three org-scoped tables: ``campaigns``, ``campaign_prospects`` and
``campaign_daily_logs``. ``uq_campaign_prospects_mailed_email`` is a partial
unique index on ``(org_id, to_email)`` over the mailed statuses, so "never send
to the same address twice across campaigns" is enforced by the database: the
claim that moves a prospect to ``sending`` fails if another already holds it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "124_autopilot_campaigns"
down_revision = "123_discovery_documents"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)
TS = postgresql.TIMESTAMP(timezone=True)


def _org():
    return sa.Column(
        "org_id", UUID, sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
    )


def _ts(name):
    return sa.Column(name, TS, nullable=False, server_default=sa.text("now()"))


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("pause_reason", sa.String(50), nullable=True),
        sa.Column("document_ids", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("document_summary", sa.Text(), nullable=True),
        sa.Column("icp_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "gmail_account_id", UUID,
            sa.ForeignKey("gmail_accounts.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("daily_send_limit", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("send_window_start", sa.String(5), nullable=False, server_default="09:00"),
        sa.Column("send_window_end", sa.String(5), nullable=False, server_default="18:00"),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="America/Toronto"),
        sa.Column("send_on_weekends", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("review_before_send", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("attach_documents", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("warmup_day", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("warmup_on", sa.Date(), nullable=True),
        sa.Column("last_discovery_on", sa.Date(), nullable=True),
        sa.Column("next_send_at", TS, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_campaigns_org_id", "campaigns", ["org_id"])
    op.create_index("ix_campaigns_user_id", "campaigns", ["user_id"])

    op.create_table(
        "campaign_prospects",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "campaign_id", UUID, sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("company_id", UUID, sa.ForeignKey("companies.id", ondelete="SET NULL"), nullable=True),
        sa.Column("contact_id", UUID, sa.ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True),
        sa.Column("to_email", sa.String(320), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("source", sa.String(30), nullable=True),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("email_subject", sa.String(500), nullable=True),
        sa.Column("email_body", sa.Text(), nullable=True),
        sa.Column(
            "email_send_id", UUID, sa.ForeignKey("email_sends.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column("gmail_message_id", sa.String(255), nullable=True),
        sa.Column("gmail_thread_id", sa.String(255), nullable=True),
        sa.Column("approved_at", TS, nullable=True),
        sa.Column("scheduled_for", TS, nullable=True),
        sa.Column("sent_at", TS, nullable=True),
        sa.Column("replied_at", TS, nullable=True),
        sa.Column("bounced_at", TS, nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_campaign_prospect_contact"),
    )
    op.create_index("ix_campaign_prospects_org_id", "campaign_prospects", ["org_id"])
    op.create_index("ix_campaign_prospects_campaign_id", "campaign_prospects", ["campaign_id"])
    op.create_index("ix_campaign_prospects_status", "campaign_prospects", ["status"])
    op.create_index("ix_campaign_prospects_to_email", "campaign_prospects", ["to_email"])
    op.create_index(
        "uq_campaign_prospects_mailed_email", "campaign_prospects", ["org_id", "to_email"],
        unique=True,
        postgresql_where=sa.text("status IN ('sending','sent','replied','bounced')"),
    )

    op.create_table(
        "campaign_daily_logs",
        sa.Column("id", UUID, primary_key=True),
        _org(),
        sa.Column(
            "campaign_id", UUID, sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("prospects_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("emails_sent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("emails_bounced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        _ts("created_at"),
        sa.UniqueConstraint("campaign_id", "date", name="uq_campaign_daily_log_day"),
    )
    op.create_index("ix_campaign_daily_logs_org_id", "campaign_daily_logs", ["org_id"])
    op.create_index("ix_campaign_daily_logs_campaign_id", "campaign_daily_logs", ["campaign_id"])


def downgrade() -> None:
    op.drop_table("campaign_daily_logs")
    op.drop_index("uq_campaign_prospects_mailed_email", table_name="campaign_prospects")
    op.drop_table("campaign_prospects")
    op.drop_table("campaigns")
