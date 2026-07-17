"""Tests for the AI-Optimized Sequence Engine (feature 1.2).

The AI provider is mocked so these run offline. Covers the optimizer's decision
sanitization, context gathering, the audit-log helper, and the fail-safe paths.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.models.email_sentiment import EmailSentimentScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.sequence_optimizer import (
    MAX_DELAY_DAYS,
    SequenceOptimizer,
    get_sequence_optimizer,
    record_decision,
)


# --------------------------------------------------------------------------- #
# record_decision — audit log on optimization_context
#
# These use a lightweight stand-in rather than a real mapped OutreachEnrollment:
# record_decision only touches the ``optimization_context`` attribute, and
# instantiating the mapped class here is both unnecessary and (under the SQLite
# test harness's custom TypeDecorators) primes a statement-cache entry that can
# corrupt a later test's UUID-filtered query.
# --------------------------------------------------------------------------- #


def test_record_decision_appends_to_context():
    enrollment = SimpleNamespace(optimization_context={})

    record_decision(enrollment, 1, {"action": "send", "reasoning": "normal", "confidence": 0.9})
    record_decision(enrollment, 2, {"action": "delay", "reasoning": "timing", "delay_days": 3})

    ctx = enrollment.optimization_context
    assert len(ctx["decisions"]) == 2
    assert ctx["decisions"][0]["step"] == 1
    assert ctx["decisions"][1]["action"] == "delay"
    assert ctx["last_action"] == "delay"


def test_record_decision_handles_none_context():
    enrollment = SimpleNamespace(optimization_context=None)
    record_decision(enrollment, 1, {"action": "send"})
    assert enrollment.optimization_context["decisions"][0]["action"] == "send"


# --------------------------------------------------------------------------- #
# _sanitize — clamp/validate raw AI decisions
# --------------------------------------------------------------------------- #


def test_sanitize_unknown_action_defaults_to_send():
    out = SequenceOptimizer._sanitize({"action": "explode", "reasoning": "x"})
    assert out["action"] == "send"


def test_sanitize_delay_is_clamped():
    out = SequenceOptimizer._sanitize({"action": "delay", "delay_days": 999})
    assert out["action"] == "delay"
    assert out["delay_days"] == MAX_DELAY_DAYS

    out2 = SequenceOptimizer._sanitize({"action": "delay", "delay_days": 0})
    assert out2["delay_days"] == 1  # floored to at least 1


def test_sanitize_switch_channel_rejects_email():
    out = SequenceOptimizer._sanitize({"action": "switch_channel", "channel": "email"})
    assert out["channel"] == "call"  # email is not a valid switch target

    out2 = SequenceOptimizer._sanitize({"action": "switch_channel", "channel": "linkedin"})
    assert out2["channel"] == "linkedin"


def test_sanitize_rewrite_without_content_falls_back_to_send():
    out = SequenceOptimizer._sanitize({"action": "rewrite", "subject": "", "body": ""})
    assert out["action"] == "send"


def test_sanitize_rewrite_with_content_keeps_copy():
    out = SequenceOptimizer._sanitize(
        {"action": "rewrite", "subject": "new subj", "body": "new body"}
    )
    assert out["action"] == "rewrite"
    assert out["subject"] == "new subj"
    assert out["body"] == "new body"


def test_sanitize_confidence_clamped():
    out = SequenceOptimizer._sanitize({"action": "send", "confidence": 5.0})
    assert out["confidence"] == 1.0
    out2 = SequenceOptimizer._sanitize({"action": "send", "confidence": "bad"})
    assert out2["confidence"] == 0.0


# --------------------------------------------------------------------------- #
# decide — fail-safe when disabled / no key
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decide_returns_send_when_disabled(db_session, sample_company, sample_contact, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sequence_optimizer_enabled", False)

    seq = OutreachSequence(id=uuid.uuid4(), name="Test Seq")
    db_session.add(seq)
    await db_session.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="s", body_template="b",
    )
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id, current_step=1
    )
    db_session.add_all([step, enrollment])
    await db_session.flush()

    optimizer = get_sequence_optimizer()
    decision = await optimizer.decide(
        db_session, enrollment, sample_contact, sample_company, step, "subj", "body"
    )
    assert decision["action"] == "send"


# --------------------------------------------------------------------------- #
# decide — full path with mocked AI + seeded engagement context
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decide_uses_ai_and_context(db_session, sample_company, sample_contact, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sequence_optimizer_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    seq = OutreachSequence(id=uuid.uuid4(), name="Test Seq")
    db_session.add(seq)
    await db_session.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=2,
        subject_template="s", body_template="b",
    )
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id, current_step=2
    )
    db_session.add_all([step, enrollment])
    await db_session.flush()

    # Seed two sent events with no reply → unanswered_sends == 2.
    for _ in range(2):
        db_session.add(
            OutreachEvent(
                id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                event_type="sent", event_metadata={},
            )
        )
    # Seed a sentiment score with an objection.
    db_session.add(
        EmailSentimentScore(
            id=uuid.uuid4(), contact_id=sample_contact.id, company_id=sample_company.id,
            sentiment_score=4.0, sentiment_label="negative", intent="objection",
            objections=["too expensive"], buying_signals=[],
        )
    )
    await db_session.flush()

    captured = {}

    async def fake_complete_json(self, prompt, max_tokens=1024):
        captured["prompt"] = prompt
        return {
            "action": "rewrite",
            "subject": "adapted subject",
            "body": "adapted body addressing price",
            "reasoning": "address the pricing objection",
            "confidence": 0.8,
        }

    # Patch at the CLASS level (not on the singleton instance): an instance-level
    # monkeypatch of the shared AIService singleton leaves a restored bound method
    # as an instance attribute that shadows later class-level patches in other
    # test files — leaking a real API call.
    import app.services.ai_service as ai_mod
    from app.services.ai_service import AIService

    monkeypatch.setattr(ai_mod, "_ai_service", None)
    monkeypatch.setattr(AIService, "_complete_json", fake_complete_json)
    optimizer = get_sequence_optimizer()

    decision = await optimizer.decide(
        db_session, enrollment, sample_contact, sample_company, step, "orig subj", "orig body"
    )

    assert decision["action"] == "rewrite"
    assert decision["subject"] == "adapted subject"
    # The gathered context must have reached the prompt.
    assert "too expensive" in captured["prompt"]
    assert "unanswered_sends" in captured["prompt"]


@pytest.mark.asyncio
async def test_decide_ai_error_falls_back_to_send(db_session, sample_company, sample_contact, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "sequence_optimizer_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
        subject_template="s", body_template="b",
    )
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id, current_step=1
    )
    db_session.add_all([step, enrollment])
    await db_session.flush()

    from app.services.ai_service import AIService

    async def boom(self, prompt, max_tokens=1024):
        raise Exception("boom")

    monkeypatch.setattr(AIService, "_complete_json", boom)
    optimizer = get_sequence_optimizer()

    decision = await optimizer.decide(
        db_session, enrollment, sample_contact, sample_company, step, "s", "b"
    )
    assert decision["action"] == "send"
