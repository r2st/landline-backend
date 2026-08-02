"""Retrieval-Augmented Generation over institutional sales knowledge (feature 3.4).

Embeds sent emails, deal notes, interactions, and knowledge documents into
``ContentEmbedding`` rows, then retrieves the most relevant chunks for any query
so AI features can ground their output in what actually worked before. A natural
-language ``ask`` endpoint synthesizes an answer from the retrieved context.

Embeddings use a deterministic, dependency-free feature-hashing embedder so the
system works offline and in tests. It is intentionally pluggable: swap
``_embed`` for a hosted embeddings provider (and bump ``EMBEDDING_DIM``) to get
stronger semantics without touching the retrieval/index logic. Similarity is
cosine, computed in Python over candidate rows, so it behaves identically on
PostgreSQL/pgvector and on the SQLite test database.
"""

import hashlib
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.content_embedding import EMBEDDING_DIM, ContentEmbedding

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def _strip_html(text: str) -> str:
    return _HTML_TAG_RE.sub(" ", text or "")


class RagService:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.dim = EMBEDDING_DIM
        self.model_name = "hash-v1"

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------
    def _embed(self, text: str) -> list[float]:
        """Deterministic feature-hashing embedding, L2-normalized.

        Uses a stable md5 hash (not Python's per-process-salted ``hash``) so the
        same text maps to the same vector across workers and runs. Unigrams +
        adjacent bigrams give a little word-order signal.
        """
        vec = [0.0] * self.dim
        toks = _tokens(text)
        grams = list(toks)
        grams += [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
        for g in grams:
            h = hashlib.md5(g.encode("utf-8")).digest()
            idx = int.from_bytes(h[:4], "little") % self.dim
            sign = 1.0 if (h[4] & 1) else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    def embed_text(self, text: str) -> list[float]:
        return self._embed(text)

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        # Stored vectors are already normalized, so this is a dot product; guard
        # against any non-normalized input anyway.
        if not a or not b or len(a) != len(b):
            return 0.0
        return sum(x * y for x, y in zip(a, b))

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------
    async def index_content(
        self,
        db: AsyncSession,
        source_type: str,
        content_text: str,
        source_id=None,
        company_id=None,
        outcome_weight: float = 1.0,
        meta: Optional[dict] = None,
    ) -> Optional[ContentEmbedding]:
        """Embed and store one chunk. Idempotent per (source_type, source_id):
        an existing row is updated in place. Empty content is skipped."""
        content_text = (content_text or "").strip()
        if not content_text:
            return None

        existing = None
        if source_id is not None:
            # Upsert target. No unique constraint on (source_type, source_id),
            # so a re-index that raced with itself leaves two rows; take the
            # earliest and update it consistently rather than raising on every
            # subsequent index of that source.
            existing = (
                await db.execute(
                    select(ContentEmbedding)
                    .where(
                        ContentEmbedding.source_type == source_type,
                        ContentEmbedding.source_id == source_id,
                    )
                    .order_by(ContentEmbedding.id.asc())
                    .limit(1)
                )
            ).scalars().first()

        embedding = self._embed(content_text)
        if existing is not None:
            existing.content_text = content_text[:8000]
            existing.embedding = embedding
            existing.embedding_model = self.model_name
            existing.outcome_weight = outcome_weight
            existing.company_id = company_id
            existing.meta = meta or {}
            await db.flush()
            return existing

        row = ContentEmbedding(
            source_type=source_type,
            source_id=source_id,
            company_id=company_id,
            content_text=content_text[:8000],
            embedding=embedding,
            embedding_model=self.model_name,
            outcome_weight=outcome_weight,
            meta=meta or {},
        )
        db.add(row)
        await db.flush()
        return row

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------
    async def search(
        self,
        db: AsyncSession,
        query: str,
        top_k: Optional[int] = None,
        source_types: Optional[list[str]] = None,
        company_id=None,
        candidate_limit: int = 2000,
    ) -> list[dict]:
        """Return the top-k most relevant chunks for a query.

        Score = cosine similarity × outcome_weight, so content that produced good
        outcomes (a reply, a booked meeting) outranks equally-similar content that
        didn't. Weak matches below ``rag_min_similarity`` are dropped.
        """
        top_k = top_k or self.settings.rag_top_k
        q = select(ContentEmbedding)
        if source_types:
            q = q.where(ContentEmbedding.source_type.in_(source_types))
        if company_id is not None:
            q = q.where(ContentEmbedding.company_id == company_id)
        q = q.limit(candidate_limit)
        rows = (await db.execute(q)).scalars().all()

        qvec = self._embed(query)
        scored = []
        for r in rows:
            sim = self._cosine(qvec, r.embedding or [])
            if sim < self.settings.rag_min_similarity:
                continue
            scored.append((sim * (r.outcome_weight or 1.0), sim, r))
        scored.sort(key=lambda t: t[0], reverse=True)
        out = []
        for weighted, sim, r in scored[:top_k]:
            out.append({
                "id": str(r.id),
                "source_type": r.source_type,
                "source_id": str(r.source_id) if r.source_id else None,
                "company_id": str(r.company_id) if r.company_id else None,
                "content_text": r.content_text,
                "similarity": round(sim, 4),
                "score": round(weighted, 4),
                "outcome_weight": r.outcome_weight,
                "meta": r.meta,
            })
        return out

    async def assemble_context(
        self,
        db: AsyncSession,
        query: str,
        top_k: Optional[int] = None,
        source_types: Optional[list[str]] = None,
        company_id=None,
        max_chars: int = 3000,
    ) -> str:
        """Retrieve relevant chunks and format them as a prompt-ready context block."""
        hits = await self.search(db, query, top_k=top_k, source_types=source_types, company_id=company_id)
        parts, used = [], 0
        for i, h in enumerate(hits, 1):
            snippet = h["content_text"][:800]
            block = f"[{i}] ({h['source_type']}, relevance {h['similarity']}):\n{snippet}"
            if used + len(block) > max_chars:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)

    async def answer(
        self,
        db: AsyncSession,
        question: str,
        top_k: Optional[int] = None,
        source_types: Optional[list[str]] = None,
        company_id=None,
    ) -> dict:
        """Answer a natural-language question from the knowledge base."""
        hits = await self.search(db, question, top_k=top_k, source_types=source_types, company_id=company_id)
        if not hits:
            return {
                "answer": "I couldn't find anything relevant in the knowledge base yet. "
                          "As more emails, notes, and interactions are indexed, answers will improve.",
                "sources": [],
            }

        context = "\n\n".join(
            f"[{i}] ({h['source_type']}):\n{h['content_text'][:800]}" for i, h in enumerate(hits, 1)
        )
        prompt = (
            "You are a sales knowledge assistant for Apprend Technologies (manufacturing software). "
            "Answer the rep's question using ONLY the context below, which is drawn from real past "
            "emails, notes, and interactions. Cite sources inline like [1], [2]. If the context is "
            "insufficient, say so plainly.\n\n"
            f"CONTEXT:\n{context}\n\n"
            f"QUESTION: {question}\n\nAnswer concisely:"
        )
        try:
            from app.services.ai_service import get_ai_service

            text = await get_ai_service().complete_text(prompt, max_tokens=600)
        except Exception as e:  # pragma: no cover
            logger.warning("RAG answer synthesis failed: %s", e)
            text = ""
        if not text:
            # Degrade gracefully: return the top snippets as the "answer".
            text = "Based on the most relevant past content:\n" + "\n".join(
                f"- {h['content_text'][:200]}" for h in hits[:3]
            )
        return {
            "answer": text,
            "sources": [
                {
                    "source_type": h["source_type"],
                    "source_id": h["source_id"],
                    "similarity": h["similarity"],
                    "excerpt": h["content_text"][:200],
                }
                for h in hits
            ],
        }

    # ------------------------------------------------------------------
    # Bulk indexing (Celery)
    # ------------------------------------------------------------------
    async def index_new_content(self, db: AsyncSession, since_days: int = 30, limit: int = 500) -> dict:
        """Index recent, not-yet-embedded content across source types."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        counts = {"email": 0, "deal_note": 0, "interaction": 0, "knowledge_document": 0}

        # Which source_ids are already embedded (avoid re-embedding).
        existing_ids = set(
            (await db.execute(select(ContentEmbedding.source_id))).scalars().all()
        )

        # --- Emails (weight replies higher) ---
        try:
            from app.models.contact import Contact
            from app.models.email_template import EmailSend

            emails = (
                await db.execute(
                    select(EmailSend).where(EmailSend.sent_at >= cutoff).limit(limit)
                )
            ).scalars().all()
            for e in emails:
                if e.id in existing_ids:
                    continue
                body = _strip_html(e.body_html or "")
                text = f"Subject: {e.subject or ''}\n{body}".strip()
                if not text:
                    continue
                company_id = None
                if e.contact_id:
                    contact = (
                        await db.execute(select(Contact).where(Contact.id == e.contact_id))
                    ).scalar_one_or_none()
                    company_id = contact.company_id if contact else None
                weight = 2.0 if e.replied_at is not None else 1.0
                await self.index_content(
                    db, "email", text, source_id=e.id, company_id=company_id,
                    outcome_weight=weight, meta={"status": e.status},
                )
                counts["email"] += 1
        except Exception:
            logger.exception("Email indexing failed")

        # --- Deal notes ---
        try:
            from app.models.deal import Deal
            from app.models.deal_note import DealNote

            notes = (
                await db.execute(
                    select(DealNote).where(DealNote.created_at >= cutoff).limit(limit)
                )
            ).scalars().all()
            for n in notes:
                if n.id in existing_ids:
                    continue
                company_id = None
                deal = (await db.execute(select(Deal).where(Deal.id == n.deal_id))).scalar_one_or_none()
                if deal:
                    company_id = deal.company_id
                await self.index_content(
                    db, "deal_note", n.content, source_id=n.id, company_id=company_id,
                )
                counts["deal_note"] += 1
        except Exception:
            logger.exception("Deal-note indexing failed")

        # --- Interactions ---
        try:
            from app.models.interaction import Interaction

            rows = (
                await db.execute(
                    select(Interaction).where(Interaction.created_at >= cutoff).limit(limit)
                )
            ).scalars().all()
            for it in rows:
                if it.id in existing_ids:
                    continue
                text = " ".join(t for t in [it.subject, it.content or it.notes] if t).strip()
                if not text:
                    continue
                await self.index_content(
                    db, "interaction", text, source_id=it.id, company_id=it.company_id,
                    meta={"type": it.interaction_type},
                )
                counts["interaction"] += 1
        except Exception:
            logger.exception("Interaction indexing failed")

        # --- Knowledge documents ---
        try:
            from app.models.knowledge import KnowledgeDocument

            docs = (await db.execute(select(KnowledgeDocument).limit(limit))).scalars().all()
            for d in docs:
                if d.id in existing_ids:
                    continue
                text = f"{d.title}\n{d.content}".strip()
                await self.index_content(
                    db, "knowledge_document", text, source_id=d.id,
                    meta={"category": d.category, "key": d.key},
                )
                counts["knowledge_document"] += 1
        except Exception:
            logger.exception("Knowledge-doc indexing failed")

        counts["total"] = sum(v for k, v in counts.items() if k != "total")
        return counts


_rag: Optional[RagService] = None


def get_rag_service() -> RagService:
    global _rag
    if _rag is None:
        _rag = RagService()
    return _rag
