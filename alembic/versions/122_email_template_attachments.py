"""Email template attachments — files sent with every email from a template.

Revision ID: 122_email_template_attachments
Revises: 121_org_llm_keys

The bytes are stored in the row (``bytea``). The API runs on the host and the
scheduled-send sweep runs in the Celery container, so a file written to disk by
one is not readable by the other; the per-template cap in
``app.services.template_attachments`` keeps the table small.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "122_email_template_attachments"
down_revision = "121_org_llm_keys"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_template_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "template_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("email_templates.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_email_template_attachments_org_id", "email_template_attachments", ["org_id"],
    )
    op.create_index(
        "ix_email_template_attachments_template_id",
        "email_template_attachments", ["template_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_template_attachments_template_id", table_name="email_template_attachments"
    )
    op.drop_index(
        "ix_email_template_attachments_org_id", table_name="email_template_attachments"
    )
    op.drop_table("email_template_attachments")
