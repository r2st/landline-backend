"""Add email communication columns and email_replies table

Revision ID: 023_email_communication
Revises: 022_deal_stage_and_activity
Create Date: 2026-06-25
"""
from alembic import op

revision = "023_email_communication"
down_revision = "022_deal_stage_and_activity"
branch_labels = None
depends_on = None


def upgrade():
    # Add new columns to email_sends for thread tracking
    op.execute("""
        ALTER TABLE email_sends
        ADD COLUMN IF NOT EXISTS gmail_message_id VARCHAR(255)
    """)
    op.execute("""
        ALTER TABLE email_sends
        ADD COLUMN IF NOT EXISTS gmail_thread_id VARCHAR(255)
    """)
    op.execute("""
        ALTER TABLE email_sends
        ADD COLUMN IF NOT EXISTS body_html TEXT
    """)
    op.execute("""
        ALTER TABLE email_sends
        ADD COLUMN IF NOT EXISTS from_email VARCHAR(320)
    """)
    op.execute("""
        ALTER TABLE email_sends
        ADD COLUMN IF NOT EXISTS sender_account_id UUID REFERENCES gmail_accounts(id) ON DELETE SET NULL
    """)

    # Index on gmail_thread_id for email_sends
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_sends_gmail_thread_id
        ON email_sends (gmail_thread_id)
    """)

    # Create email_replies table
    op.execute("""
        CREATE TABLE IF NOT EXISTS email_replies (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            email_send_id     UUID REFERENCES email_sends(id) ON DELETE SET NULL,
            contact_id        UUID REFERENCES contacts(id) ON DELETE SET NULL,
            company_id        UUID REFERENCES companies(id) ON DELETE SET NULL,
            gmail_account_id  UUID REFERENCES gmail_accounts(id) ON DELETE SET NULL,
            gmail_message_id  VARCHAR(255) UNIQUE,
            gmail_thread_id   VARCHAR(255),
            from_email        VARCHAR(320) NOT NULL,
            to_email          VARCHAR(320),
            subject           VARCHAR(500),
            body_text         TEXT,
            body_html         TEXT,
            snippet           VARCHAR(500),
            received_at       TIMESTAMPTZ NOT NULL,
            is_read           BOOLEAN DEFAULT FALSE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # Indexes for email_replies
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_replies_gmail_thread_id
        ON email_replies (gmail_thread_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_replies_from_email
        ON email_replies (from_email)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_replies_contact_id
        ON email_replies (contact_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_email_replies_company_id
        ON email_replies (company_id)
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS email_replies")
    op.execute("DROP INDEX IF EXISTS ix_email_sends_gmail_thread_id")
    op.execute("ALTER TABLE email_sends DROP COLUMN IF EXISTS sender_account_id")
    op.execute("ALTER TABLE email_sends DROP COLUMN IF EXISTS from_email")
    op.execute("ALTER TABLE email_sends DROP COLUMN IF EXISTS body_html")
    op.execute("ALTER TABLE email_sends DROP COLUMN IF EXISTS gmail_thread_id")
    op.execute("ALTER TABLE email_sends DROP COLUMN IF EXISTS gmail_message_id")
