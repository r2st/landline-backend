"""SMTP mailbox IMAP settings: read replies/bounces for SMTP-sent sequences.

Chains off ``089_adaptive_delay_factor``, the current head.

Adds IMAP connection details to ``smtp_accounts`` so the reply/bounce sweeps —
which until now only spoke to the Gmail API — can also poll SMTP mailboxes
(Outlook/Office365/custom hosts). Without this, a contact on an SMTP-sent
sequence kept receiving follow-ups after they replied or bounced, because
nothing ever read that mailbox.

``imap_host`` is nullable: when unset the reader derives it from the SMTP host
(``smtp.example.com`` → ``imap.example.com``), which is correct for the common
providers. ``imap_port`` defaults to 993 (implicit TLS) and ``imap_use_ssl`` to
true — the near-universal modern default.
"""

from alembic import op
import sqlalchemy as sa

revision = "090_smtp_imap_settings"
down_revision = "089_adaptive_delay_factor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("smtp_accounts", sa.Column("imap_host", sa.String(length=255), nullable=True))
    op.add_column(
        "smtp_accounts",
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default="993"),
    )
    op.add_column(
        "smtp_accounts",
        sa.Column("imap_use_ssl", sa.Boolean(), nullable=False, server_default="true"),
    )
    # Whether the reply/bounce sweep should poll this mailbox at all. Some SMTP
    # relays are send-only (no IMAP); operators can turn polling off per-account.
    op.add_column(
        "smtp_accounts",
        sa.Column("imap_enabled", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("smtp_accounts", "imap_enabled")
    op.drop_column("smtp_accounts", "imap_use_ssl")
    op.drop_column("smtp_accounts", "imap_port")
    op.drop_column("smtp_accounts", "imap_host")
