"""Discovery documents — uploaded files that companies/contacts are extracted from.

Revision ID: 123_discovery_documents
Revises: 122_email_template_attachments

Bytes live in the row (``bytea``) for the same host-vs-container reason as
template attachments; ``content`` is nulled 30 days after upload while the
extracted results in ``extracted`` are kept.

Also adds ``outreach_sequences.send_as_written``: Discovery sends copy the user
approved word for word, which the AI composer/optimizer must not rewrite.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "123_discovery_documents"
down_revision = "122_email_template_attachments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_sequences",
        sa.Column("send_as_written", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.create_table(
        "discovery_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(10), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=True),
        sa.Column("content_purged_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="parsing"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("extracted", postgresql.JSONB(), nullable=True),
        sa.Column(
            "sequence_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outreach_sequences.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_by", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
        ),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True),
            nullable=False, server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_discovery_documents_org_id", "discovery_documents", ["org_id"])
    op.create_index(
        "ix_discovery_documents_org_created", "discovery_documents", ["org_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_discovery_documents_org_created", table_name="discovery_documents")
    op.drop_index("ix_discovery_documents_org_id", table_name="discovery_documents")
    op.drop_table("discovery_documents")
    op.drop_column("outreach_sequences", "send_as_written")
