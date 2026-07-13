"""Tests for feature 3.4 — Knowledge-Augmented Selling (RAG)."""

import uuid

import pytest

from app.models.content_embedding import ContentEmbedding
from app.services.rag_service import get_rag_service

pytestmark = pytest.mark.asyncio


async def test_embedding_deterministic_and_normalized():
    rag = get_rag_service()
    a = rag.embed_text("cnc machining quoting turnaround")
    b = rag.embed_text("cnc machining quoting turnaround")
    assert a == b  # deterministic across calls
    assert len(a) == rag.dim
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6  # unit norm


async def test_cosine_similar_texts_rank_higher():
    rag = get_rag_service()
    q = rag.embed_text("reduce quoting time for a stamping shop")
    close = rag.embed_text("we cut quoting time for a metal stamping shop")
    far = rag.embed_text("the weather today is sunny and warm")
    assert rag._cosine(q, close) > rag._cosine(q, far)


async def test_index_and_search(db_session):
    rag = get_rag_service()
    await rag.index_content(db_session, "email", "How we cut CNC machining quote time from 2 days to 20 minutes")
    await rag.index_content(db_session, "email", "Injection molding scrap rate reduction case study")
    await rag.index_content(db_session, "knowledge_document", "Company holiday schedule and PTO policy")

    hits = await rag.search(db_session, "cnc machining quoting speed", top_k=2)
    assert hits
    assert "CNC machining" in hits[0]["content_text"]


async def test_outcome_weight_boosts_ranking(db_session):
    rag = get_rag_service()
    # Two near-identical emails; the one that got a reply is weighted higher.
    await rag.index_content(db_session, "email", "objection handling for price on cnc parts",
                            source_id=uuid.uuid4(), outcome_weight=1.0)
    await rag.index_content(db_session, "email", "objection handling for price on cnc parts",
                            source_id=uuid.uuid4(), outcome_weight=2.0)
    hits = await rag.search(db_session, "how to handle price objection cnc", top_k=2)
    assert len(hits) == 2
    assert hits[0]["outcome_weight"] == 2.0


async def test_index_content_idempotent(db_session):
    rag = get_rag_service()
    sid = uuid.uuid4()
    await rag.index_content(db_session, "email", "first version", source_id=sid)
    await rag.index_content(db_session, "email", "second version", source_id=sid)
    rows = (await db_session.execute(ContentEmbedding.__table__.select())).all()
    same_source = [r for r in rows if r.source_id == sid]
    assert len(same_source) == 1
    assert same_source[0].content_text == "second version"


async def test_answer_degrades_without_ai(db_session):
    rag = get_rag_service()
    await rag.index_content(db_session, "email", "We reduced quoting time for a stamping shop dramatically")
    # No valid AI key in tests → answer falls back to top snippets, still returns sources.
    result = await rag.answer(db_session, "how did we help with quoting?")
    assert "answer" in result
    assert result["sources"]


async def test_knowledge_ask_and_search_api(client):
    r = await client.post("/api/v1/knowledge/documents", json={
        "key": "quoting-playbook", "title": "Quoting Playbook",
        "content": "To speed up quoting for CNC machining shops, integrate the ERP and automate estimates.",
        "category": "playbook",
    })
    assert r.status_code in (200, 201), r.text

    r = await client.post("/api/v1/knowledge/index", json={})
    assert r.status_code == 200, r.text
    assert r.json()["total"] >= 1

    r = await client.post("/api/v1/knowledge/search", json={"question": "speed up cnc quoting"})
    assert r.status_code == 200
    hits = r.json()
    assert isinstance(hits, list) and hits

    r = await client.post("/api/v1/knowledge/ask", json={"question": "how do we speed up quoting?"})
    assert r.status_code == 200
    assert "answer" in r.json()
    assert "sources" in r.json()
