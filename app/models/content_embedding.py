"""RAG content-embedding model (feature 3.4).

Stores a vector embedding for a chunk of institutional knowledge — a sent
email, a call note, a deal note, an interaction, a knowledge document — so any
AI feature can semantically retrieve the most relevant historical context.

Portability note: the ``embedding`` column uses :class:`EmbeddingVector`, a
dialect-aware type that maps to a native ``pgvector`` column on PostgreSQL (when
the ``pgvector`` package is installed) and degrades to a JSON-encoded list of
floats everywhere else (SQLite test suite, or a Postgres without pgvector). This
keeps the model importable and the tests runnable without the extension, while
production gets a real vector column. Similarity search in ``rag_service`` is
computed in Python over candidate rows so it works identically on both.
"""

import json
import uuid
from datetime import datetime

from sqlalchemy import Float, String, Text, TypeDecorator, func
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.mixins import OrgScopedMixin

# Dimension of stored embeddings. Kept modest for the local feature-hashing
# embedder in rag_service; swap the embedder + this constant together if you
# move to a hosted embeddings provider.
EMBEDDING_DIM = 1536

# Source kinds we embed. Plain tuple so new kinds don't need a migration.
EMBEDDING_SOURCE_TYPES = (
    "email",
    "call_note",
    "deal_note",
    "interaction",
    "knowledge_document",
    "sequence",
)


class EmbeddingVector(TypeDecorator):
    """A vector column that is native ``pgvector`` on Postgres, JSON elsewhere.

    * PostgreSQL + pgvector installed → ``vector(dim)`` (native).
    * PostgreSQL without pgvector      → ``JSONB`` (list of floats).
    * SQLite / other                   → ``TEXT`` holding a JSON list.

    Values are always exposed to Python as a ``list[float]``.
    """

    impl = Text
    cache_ok = True

    def __init__(self, dim: int = EMBEDDING_DIM, *args, **kwargs):
        self.dim = dim
        super().__init__(*args, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            try:
                from pgvector.sqlalchemy import Vector

                return dialect.type_descriptor(Vector(self.dim))
            except Exception:  # pragma: no cover - pgvector not installed
                return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(Text())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = list(value)
        if dialect.name == "postgresql":
            # pgvector.Vector and JSONB both accept a Python list directly.
            return value
        return json.dumps(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return list(value)
        if isinstance(value, str):
            return json.loads(value)
        return list(value)


class ContentEmbedding(Base, OrgScopedMixin):
    __tablename__ = "content_embeddings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Loose reference to the originating row (emails, deal_notes, …). Not a FK
    # because it spans many tables; a (source_type, source_id) pair is unique.
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    company_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(EmbeddingVector(EMBEDDING_DIM), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(60), nullable=False, default="hash-v1")
    # Outcome-based ranking boost (e.g. an email that got a reply > one that
    # didn't). 1.0 = neutral. Multiplied into similarity at retrieval time.
    outcome_weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<ContentEmbedding {self.source_type}:{self.source_id} w={self.outcome_weight}>"
