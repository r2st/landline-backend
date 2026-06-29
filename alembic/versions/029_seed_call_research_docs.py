"""Seed cold-call & digital-sales research docs into the knowledge base.

Migration 027 created ``knowledge_documents`` and seeded the two docs that
existed then. Four new research docs have since been added under ``docs/`` and
registered in ``app.services.knowledge_data.DOCUMENT_SOURCES``:

- cold-calling-strategy
- digital-sales-strategy
- cold-call-scripts-by-industry
- ai-cold-calling-guidelines

``seed_knowledge`` is idempotent (documents keyed by ``key``), so calling it
again only inserts the rows that are missing — on a fresh DB everything is
already present from 027, and on an existing DB only the four new docs are
added. This makes them viewable from the website /knowledge page.

Revision ID: 029_seed_call_research_docs
Revises: 028_add_call_system
Create Date: 2026-06-28
"""
from alembic import op

from app.services.knowledge_data import seed_knowledge

revision = "029_seed_call_research_docs"
down_revision = "028_add_call_system"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    result = seed_knowledge(conn)
    print(f"  Seeded call research docs: {result}")


def downgrade():
    conn = op.get_bind()
    import sqlalchemy as sa

    conn.execute(
        sa.text(
            "DELETE FROM knowledge_documents WHERE key IN ("
            "'cold-calling-strategy', 'digital-sales-strategy', "
            "'cold-call-scripts-by-industry', 'ai-cold-calling-guidelines')"
        )
    )
