"""Tests for Conversation Intelligence (feature 1.5).

The email history fetch and Claude are mocked so these run offline. Covers the
momentum labelling, thread rendering/signals, full analysis persistence, and the
``/emails/thread/{id}/intelligence`` endpoint.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services.conversation_intelligence import (
    ConversationIntelligenceService,
    _momentum_label,
    get_conversation_intelligence_service,
)

_NOW = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)


def _thread_messages(company_id=None, contact_id=None):
    return [
        {
            "direction": "sent", "from_email": "suman@apprend.co", "to_email": "vp@acme.com",
            "subject": "quick question", "body_text": "Hey, saw you run 24/7.",
            "timestamp": _NOW, "contact_id": contact_id, "company_id": company_id,
            "is_read": True,
        },
        {
            "direction": "received", "from_email": "vp@acme.com", "to_email": "suman@apprend.co",
            "subject": "Re: quick question", "body_text": "Interested but your price seems high.",
            "timestamp": _NOW + timedelta(hours=2), "contact_id": contact_id, "company_id": company_id,
            "contact_name": "VP Ops", "is_read": True,
        },
    ]


_FAKE_ANALYSIS = {
    "momentum_score": 72,
    "momentum_reasoning": "prospect replied with interest",
    "stakeholder_map": [
        {"name": "VP Ops", "email": "vp@acme.com", "role": "decision_maker", "joined_step": 2, "signal": "replied"}
    ],
    "unresolved_objections": [
        {"objection": "price seems high", "raised_by": "vp@acme.com", "addressed": False, "notes": "not yet answered"}
    ],
    "recommended_action": "Send an ROI breakdown addressing price.",
    "next_message": {"subject": "the math", "body": "Here's the scrap math.", "rationale": "address price"},
}


# --------------------------------------------------------------------------- #
# _momentum_label
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score,label",
    [(90, "accelerating"), (70, "accelerating"), (50, "stable"), (45, "stable"),
     (30, "decelerating"), (25, "decelerating"), (10, "stalled"), (0, "stalled")],
)
def test_momentum_label_boundaries(score, label):
    assert _momentum_label(score) == label


# --------------------------------------------------------------------------- #
# _render_thread
# --------------------------------------------------------------------------- #


def test_render_thread_computes_signals():
    service = ConversationIntelligenceService()
    transcript, signals = service._render_thread(_thread_messages())
    assert signals["message_count"] == 2
    assert signals["sent_count"] == 1
    assert signals["received_count"] == 1
    assert signals["unique_participants"] == 1
    # HTML-free prose reaches the transcript.
    assert "24/7" in transcript
    assert "price seems high" in transcript


def test_render_thread_strips_html():
    service = ConversationIntelligenceService()
    msgs = [{
        "direction": "received", "from_email": "x@y.com",
        "body_html": "<p>Hello <b>there</b></p>", "timestamp": _NOW, "is_read": True,
    }]
    transcript, _ = service._render_thread(msgs)
    assert "Hello" in transcript and "there" in transcript
    assert "<p>" not in transcript


# --------------------------------------------------------------------------- #
# analyze_thread
# --------------------------------------------------------------------------- #


@pytest.fixture
def _mock_analysis(monkeypatch):
    """Patch thread fetch + Claude for offline analysis."""
    async def fake_complete_json(self, prompt, max_tokens=1024):
        fake_complete_json.last_prompt = prompt
        return dict(_FAKE_ANALYSIS)

    import app.services.ai_service as ai_mod
    from app.services.ai_service import AIService

    # Reset the singleton so a leaked instance-attribute from another test file
    # can't shadow the class-level patch.
    monkeypatch.setattr(ai_mod, "_ai_service", None)
    monkeypatch.setattr(AIService, "_complete_json", fake_complete_json)
    return fake_complete_json


@pytest.mark.asyncio
async def test_analyze_thread_persists(db_session, sample_company, sample_contact, monkeypatch, _mock_analysis):
    from app.services.email_history import EmailHistoryService

    async def fake_get_thread(self, db, thread_id):
        return {
            "gmail_thread_id": thread_id,
            "messages": _thread_messages(sample_company.id, sample_contact.id),
            "lead_id": sample_company.id,
        }

    monkeypatch.setattr(EmailHistoryService, "get_thread", fake_get_thread)

    service = get_conversation_intelligence_service()
    analysis = await service.analyze_thread(db_session, "thread-123")

    assert analysis is not None
    assert analysis.momentum_score == 72
    assert analysis.momentum_label == "accelerating"
    assert len(analysis.unresolved_objections) == 1
    assert analysis.message_count == 2
    assert analysis.company_id == sample_company.id
    assert analysis.contact_id == sample_contact.id
    # Transcript reached the prompt.
    assert "price seems high" in _mock_analysis.last_prompt

    # get_latest returns the stored analysis.
    latest = await service.get_latest(db_session, "thread-123")
    assert latest.id == analysis.id


@pytest.mark.asyncio
async def test_analyze_thread_empty_returns_none(db_session, monkeypatch):
    from app.services.email_history import EmailHistoryService

    async def empty_thread(self, db, thread_id):
        return {"gmail_thread_id": thread_id, "messages": []}

    monkeypatch.setattr(EmailHistoryService, "get_thread", empty_thread)
    service = get_conversation_intelligence_service()
    assert await service.analyze_thread(db_session, "empty") is None


# --------------------------------------------------------------------------- #
# Endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_thread_intelligence_endpoint(client, monkeypatch, _mock_analysis):
    from app.services.email_history import EmailHistoryService

    async def fake_get_thread(self, db, thread_id):
        return {
            "gmail_thread_id": thread_id,
            "messages": _thread_messages(),
            "lead_id": None,
        }

    monkeypatch.setattr(EmailHistoryService, "get_thread", fake_get_thread)

    r = await client.get("/api/v1/emails/thread/abc-thread/intelligence")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["momentum_score"] == 72
    assert body["momentum_label"] == "accelerating"
    assert body["next_message"]["subject"] == "the math"


@pytest.mark.asyncio
async def test_thread_intelligence_endpoint_404_on_empty(client, monkeypatch):
    from app.services.email_history import EmailHistoryService

    async def empty_thread(self, db, thread_id):
        return {"gmail_thread_id": thread_id, "messages": []}

    monkeypatch.setattr(EmailHistoryService, "get_thread", empty_thread)
    r = await client.get("/api/v1/emails/thread/nope/intelligence")
    assert r.status_code == 404
