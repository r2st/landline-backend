"""Close the blind spot behind the Dialtone reply-rate audit (2026-07-31).

Production evidence: ``ai_email_drafts`` had zero rows despite 39 step-1 sends
since AI composition shipped, and every metric the sequence path reports
(sent, opened, config flags, even the LLM provider's own ``is_configured``
check) looked healthy regardless. Nothing recorded whether a given send used
the AI-personalized copy or the static template, and two of
``compose_step``'s fallback paths logged nothing at all — so "AI composition
is enabled" and "AI composition is actually shipping" were indistinguishable
from outside the code.

These tests pin the fix: every fallback path now logs, and every sent event
carries an ``ai_generated``/``compose_reason`` breadcrumb that
``funnel_analytics`` rolls up into ``ai_composed_rate``.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import ai_compose
from app.services.ai_compose import compose_step
from app.services.funnel_analytics import compute_funnel

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# The two previously-silent fallback paths now log
# ---------------------------------------------------------------------------


async def _setup(db, company, contact, step_number=1):
    seq = OutreachSequence(id=_uuid.uuid4(), name="Seq")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=_uuid.uuid4(), sequence_id=seq.id, step_number=step_number, delay_days=0,
        subject_template="Quick question about {{company}}",
        body_template="<p>Hi there, we do manufacturing software.</p>",
    )
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=step_number, status="active",
    )
    db.add_all([step, enr])
    await db.flush()
    return seq, step, enr


async def test_no_api_key_now_logs_a_warning(
    db_session, sample_company, sample_contact, monkeypatch, caplog
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_compose_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    import app.services.llm_providers as llm_providers
    monkeypatch.setattr(llm_providers, "has_any_provider_key", lambda s: False)

    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    with caplog.at_level("WARNING", logger="app.services.ai_compose"):
        result = await compose_step(
            db_session, enr, sample_contact, sample_company, step,
            "static subject", "<p>static body</p>",
        )

    assert result.action == "send"
    assert result.ai_generated is False
    assert any("no provider key configured" in r.message for r in caplog.records), (
        "the 'no API key' gate must be visible in logs, not just in the send outcome"
    )


async def test_generator_template_fallback_now_logs_a_warning(
    db_session, sample_company, sample_contact, monkeypatch, caplog
):
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_compose_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    async def fake_generate(*a, **k):
        # The generator's own internal fallback: it tried, every provider in
        # the chain failed, and it returned its non-AI template instead of
        # raising. This is exactly the shape a fully-exhausted provider chain
        # produces on every single call.
        return {"subject": "s", "body": "b", "ai_generated": False}

    import app.services.ai_email_generator as gen
    monkeypatch.setattr(gen, "generate_email", fake_generate)

    _, step, enr = await _setup(db_session, sample_company, sample_contact)

    with caplog.at_level("WARNING", logger="app.services.ai_compose"):
        result = await compose_step(
            db_session, enr, sample_contact, sample_company, step,
            "static subject", "<p>static body</p>",
        )

    assert result.action == "send"
    assert result.ai_generated is False
    assert any("fell back to its own template" in r.message for r in caplog.records), (
        "a fully-exhausted provider chain must leave a trace — this is the exact "
        "silent failure that let 39 step-1 sends ship as static copy unnoticed"
    )


# ---------------------------------------------------------------------------
# The sent event now says whether AI composed it, and why not when it didn't
# ---------------------------------------------------------------------------


async def _enrollment_due_now(db: AsyncSession, company: Company, contact: Contact):
    seq = OutreachSequence(id=_uuid.uuid4(), name="CNC Machining — Deal Cracker", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="{{first_name}}, quick question",
            body_template="<p>Hi {{first_name}},</p>",
        )
    )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return seq, enrollment


def _stub_pipeline(monkeypatch, db_session, send_result: dict, compose_result):
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _compose(db, enrollment, contact, company, step, subject, body):
        return compose_result

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    class _Optimizer:
        async def decide(self, *a, **k):
            return {"action": "send"}

    monkeypatch.setattr(outreach_tasks, "get_sequence_optimizer", lambda: _Optimizer())
    monkeypatch.setattr(outreach_tasks, "record_decision", lambda *a, **k: None)

    async def _send(db, **kwargs):
        return dict(send_result)

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)


async def test_sent_event_records_static_fallback_reason(
    db_session, sample_company, sample_contact, monkeypatch
):
    import app.tasks.outreach as outreach_tasks

    seq, enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(
        monkeypatch, db_session, {"status": "sent", "message_id": "m1"},
        ai_compose.ComposeResult(
            "send", "static subject", "<p>static body</p>",
            ai_generated=False, reason="no API key",
        ),
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    event = (
        await db_session.execute(
            select(OutreachEvent).where(OutreachEvent.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    assert event.event_metadata["ai_generated"] is False
    assert event.event_metadata["compose_reason"] == "no API key"


async def test_sent_event_records_ai_composed(
    db_session, sample_company, sample_contact, monkeypatch
):
    import app.tasks.outreach as outreach_tasks

    seq, enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(
        monkeypatch, db_session, {"status": "sent", "message_id": "m1"},
        ai_compose.ComposeResult(
            "send", "Your 3-week quote turnaround",
            "<p>Noticed you quote by hand.</p>",
            ai_generated=True, research_used=True,
            reason="AI-composed as first_touch",
        ),
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    event = (
        await db_session.execute(
            select(OutreachEvent).where(OutreachEvent.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    assert event.event_metadata["ai_generated"] is True
    assert event.event_metadata["research_used"] is True
    assert event.event_metadata["compose_reason"] == "AI-composed as first_touch"


# ---------------------------------------------------------------------------
# funnel_analytics surfaces the AI-composed rate
# ---------------------------------------------------------------------------


async def _seq(db, name):
    seq = OutreachSequence(id=_uuid.uuid4(), name=name)
    db.add(seq)
    await db.flush()
    return seq


async def _contact(db):
    company = Company(id=_uuid.uuid4(), name="Corp", source="test", country="US")
    db.add(company)
    await db.flush()
    c = Contact(
        id=_uuid.uuid4(), company_id=company.id,
        email=f"{_uuid.uuid4().hex[:8]}@corp.io", first_name="Lead", source="test",
    )
    db.add(c)
    await db.flush()
    return c


async def _enrollment(db, contact, seq):
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


def _sent_event(enr, step, *, ai_generated):
    return OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enr.id, step_number=step, event_type="sent",
        event_metadata={"ai_generated": ai_generated},
        occurred_at=datetime.now(timezone.utc),
    )


async def test_funnel_reports_zero_ai_composed_rate_when_every_send_is_static(db_session):
    """The exact production shape this audit found: configured, but 0% AI."""
    seq = await _seq(db_session, "AllStatic")
    for _ in range(3):
        enr = await _enrollment(db_session, await _contact(db_session), seq)
        db_session.add(_sent_event(enr, 1, ai_generated=False))
    await db_session.commit()

    funnel = await compute_funnel(db_session, sequence_id=seq.id)
    assert funnel["totals"]["sent"] == 3
    assert funnel["totals"]["ai_composed_rate"] == 0.0


async def test_funnel_reports_partial_ai_composed_rate(db_session):
    seq = await _seq(db_session, "Mixed")
    enr1 = await _enrollment(db_session, await _contact(db_session), seq)
    enr2 = await _enrollment(db_session, await _contact(db_session), seq)
    enr3 = await _enrollment(db_session, await _contact(db_session), seq)
    enr4 = await _enrollment(db_session, await _contact(db_session), seq)
    db_session.add_all([
        _sent_event(enr1, 1, ai_generated=True),
        _sent_event(enr2, 1, ai_generated=False),
        _sent_event(enr3, 1, ai_generated=False),
        _sent_event(enr4, 1, ai_generated=False),
    ])
    await db_session.commit()

    funnel = await compute_funnel(db_session, sequence_id=seq.id)
    assert funnel["totals"]["ai_composed_rate"] == 25.0
    step = funnel["sequences"][0]["steps"][0]
    assert step["ai_composed_rate"] == 25.0


async def test_funnel_ai_composed_rate_is_zero_when_no_sends(db_session):
    funnel = await compute_funnel(db_session)
    assert funnel["totals"]["ai_composed_rate"] == 0.0
