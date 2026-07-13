"""Add Phase 3.4 content_embeddings table for the RAG knowledge store.

Uses the native ``pgvector`` extension + a ``vector`` column with an ivfflat
cosine index when the extension is available on the server; otherwise degrades
to a JSONB column (list of floats) so the migration still succeeds on a Postgres
without pgvector. The application computes similarity in Python either way.

Revision ID: 052_add_content_embeddings
Revises: 051_add_sender_health
Create Date: 2026-07-13
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "052_add_content_embeddings"
down_revision = "051_add_sender_health"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1536


def upgrade() -> None:
    conn = op.get_bind()

    # Only enable pgvector when the server actually offers it — probing via
    # pg_available_extensions avoids aborting the migration transaction on a
    # failed CREATE EXTENSION.
    use_vector = False
    try:
        available = conn.execute(
            sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
        ).first()
        if available:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
            use_vector = True
    except Exception:
        use_vector = False

    embedding_col = sa.Column("embedding", postgresql.JSONB, nullable=False)
    if use_vector:
        try:
            from pgvector.sqlalchemy import Vector

            embedding_col = sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False)
        except Exception:
            embedding_col = sa.Column("embedding", postgresql.JSONB, nullable=False)
            use_vector = False

    op.create_table(
        "content_embeddings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("company_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("content_text", sa.Text, nullable=False),
        embedding_col,
        sa.Column("embedding_model", sa.String(60), nullable=False, server_default="hash-v1"),
        sa.Column("outcome_weight", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("meta", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_content_embeddings_source_type", "content_embeddings", ["source_type"])
    op.create_index("ix_content_embeddings_source_id", "content_embeddings", ["source_id"])
    op.create_index("ix_content_embeddings_company_id", "content_embeddings", ["company_id"])

    if use_vector:
        # ivfflat cosine index for fast native similarity search at scale.
        op.execute(
            "CREATE INDEX ix_content_embeddings_embedding "
            "ON content_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
        )


def downgrade() -> None:
    op.drop_table("content_embeddings")
