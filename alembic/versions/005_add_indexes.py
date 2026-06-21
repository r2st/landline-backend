"""Add missing indexes for performance

Revision ID: 005_add_indexes
Revises: 004_gmail_accounts
Create Date: 2026-06-20
"""
from alembic import op

revision = "005_add_indexes"
down_revision = "004_gmail_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_email_sends_sent_at", "email_sends", ["sent_at"])
    op.create_index("ix_email_sends_contact_id", "email_sends", ["contact_id"])
    op.create_index("ix_outreach_enrollments_contact_id", "outreach_enrollments", ["contact_id"])
    op.create_index("ix_outreach_enrollments_sequence_id", "outreach_enrollments", ["sequence_id"])
    op.create_index("ix_interactions_company_id", "interactions", ["company_id"])
    op.create_index("ix_interactions_contact_id", "interactions", ["contact_id"])
    op.create_index("ix_interactions_occurred_at", "interactions", ["occurred_at"])


def downgrade():
    op.drop_index("ix_interactions_occurred_at")
    op.drop_index("ix_interactions_contact_id")
    op.drop_index("ix_interactions_company_id")
    op.drop_index("ix_outreach_enrollments_sequence_id")
    op.drop_index("ix_outreach_enrollments_contact_id")
    op.drop_index("ix_email_sends_contact_id")
    op.drop_index("ix_email_sends_sent_at")
