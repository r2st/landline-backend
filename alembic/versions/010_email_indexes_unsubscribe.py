"""Add missing email_sends indexes and contacts.unsubscribed column

Revision ID: 010_email_indexes_unsubscribe
Revises: 009_add_company_news
Create Date: 2026-06-20
"""
from alembic import op
import sqlalchemy as sa

revision = "010_email_indexes_unsubscribe"
down_revision = "009_add_company_news"
branch_labels = None
depends_on = None


def upgrade():
    # Add missing indexes on email_sends
    op.create_index("ix_email_sends_to_email", "email_sends", ["to_email"])
    op.create_index("ix_email_sends_status", "email_sends", ["status"])
    # Note: ix_email_sends_sent_at already exists from migration 005

    # Add unsubscribed column to contacts
    op.add_column(
        "contacts",
        sa.Column("unsubscribed", sa.Boolean, server_default="false", nullable=False),
    )
    op.create_index("ix_contacts_unsubscribed", "contacts", ["unsubscribed"])


def downgrade():
    op.drop_index("ix_contacts_unsubscribed")
    op.drop_column("contacts", "unsubscribed")
    op.drop_index("ix_email_sends_status")
    op.drop_index("ix_email_sends_to_email")
