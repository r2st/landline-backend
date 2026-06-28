"""Knowledge base: industry contacts + editable reference documents.

Creates ``industry_contacts`` and ``knowledge_documents`` and seeds them from the
version-controlled source files (``data/industry_contacts.csv`` and the markdown
docs under ``docs/``). Seeding is idempotent — keyed by (company_name,
contact_email) for contacts and by ``key`` for documents — so retries are safe.

Revision ID: 027_knowledge_base
Revises: 026_industry_verticals
Create Date: 2026-06-28
"""
import sqlalchemy as sa
from alembic import op

from app.services.knowledge_data import seed_knowledge

revision = "027_knowledge_base"
down_revision = "026_industry_verticals"
branch_labels = None
depends_on = None


def _has_table(conn, table) -> bool:
    return sa.inspect(conn).has_table(table)


def upgrade():
    conn = op.get_bind()

    if not _has_table(conn, "industry_contacts"):
        op.create_table(
            "industry_contacts",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                      primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("industry", sa.String(120), nullable=False),
            sa.Column("company_name", sa.String(255), nullable=False),
            sa.Column("website", sa.String(500)),
            sa.Column("location", sa.String(255)),
            sa.Column("employee_count", sa.String(50)),
            sa.Column("contact_name", sa.String(255)),
            sa.Column("contact_title", sa.String(255)),
            sa.Column("contact_email", sa.String(320)),
            sa.Column("notes", sa.Text()),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                      nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_industry_contacts_industry", "industry_contacts", ["industry"])
        op.create_index("ix_industry_contacts_company_name", "industry_contacts", ["company_name"])
        op.create_index("ix_industry_contacts_contact_email", "industry_contacts", ["contact_email"])

    if not _has_table(conn, "knowledge_documents"):
        op.create_table(
            "knowledge_documents",
            sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True),
                      primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("key", sa.String(120), nullable=False, unique=True),
            sa.Column("title", sa.String(255), nullable=False),
            sa.Column("category", sa.String(80), nullable=False, server_default="general"),
            sa.Column("format", sa.String(20), nullable=False, server_default="markdown"),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                      nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                      nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_knowledge_documents_key", "knowledge_documents", ["key"], unique=True)
        op.create_index("ix_knowledge_documents_category", "knowledge_documents", ["category"])

    # Seed from the source files (idempotent).
    result = seed_knowledge(conn)
    print(f"  Seeded knowledge base: {result}")


def downgrade():
    op.drop_table("knowledge_documents")
    op.drop_table("industry_contacts")
