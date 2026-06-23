"""Email signatures table for outreach email composition.

Adds the email_signatures table and seeds a default signature for
Apprend Technologies outreach emails.

Revision ID: 019_email_signatures
Revises: 018_gmail_account_sending_fields
Create Date: 2026-06-22 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "019_email_signatures"
down_revision: Union[str, None] = "018_gmail_account_sending_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_SIGNATURE_HTML = (
    "<p style='margin:0 0 4px 0'>Best regards,</p>"
    "<p style='margin:0 0 4px 0;font-weight:bold'>Apprend Technologies</p>"
    "<p style='margin:0;font-size:13px;color:#6b7280'>Manufacturing Software Solutions</p>"
)


def upgrade() -> None:
    op.create_table(
        "email_signatures",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("content_html", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_email_signatures_name"),
    )
    # At most one signature may be the default.
    op.create_index(
        "uq_email_signatures_one_default",
        "email_signatures",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )

    # Seed a default signature
    signatures = sa.table(
        "email_signatures",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("name", sa.String),
        sa.column("content_html", sa.Text),
        sa.column("is_default", sa.Boolean),
    )
    op.bulk_insert(
        signatures,
        [
            {
                "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "name": "Default",
                "content_html": DEFAULT_SIGNATURE_HTML,
                "is_default": True,
            },
        ],
    )


def downgrade() -> None:
    op.drop_index("uq_email_signatures_one_default", table_name="email_signatures")
    op.drop_table("email_signatures")
