"""Regression tests for the soft-skip path in ``send_sequence_step``.

Background — this is the defect that stalled Dialtone outreach:

``send_outreach_email`` returns ``{"status": "skipped"}`` (not an exception) when
the dispatcher declines to send — warm-up cap reached, address suppressed, kill
switch engaged. The handler for that rolled the session back and *then* read
``step.step_number`` for its log line. ``rollback()`` expires every instance in
the session, and reading an expired attribute on an AsyncSession triggers a lazy
refresh — sync IO — which raises ``MissingGreenlet``.

So every benign soft-skip crashed the task. Celery retried it, the 5-minute queue
sweep re-dispatched it, and each attempt paid for contact research and an AI
composition before reaching the gate that was always going to decline. One
mailbox at its warm-up cap was enough to wedge the queue and burn the LLM quota.

These tests pin both halves of the fix: the soft-skip returns cleanly, and it
pushes ``next_send_at`` out so the sweep stops hot-looping.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep

pytestmark = pytest.mark.asyncio


async def _enrollment_due_now(db: AsyncSession, company: Company, contact: Contact):
    """An active enrollment sitting on step 1, due to send right now."""
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="CNC Machining — Deal Cracker", is_active=True
    )
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(),
            sequence_id=seq.id,
            step_number=1,
            delay_days=0,
            subject_template="{{first_name}}, who eats it when a part runs out of tol?",
            body_template="<p>Hi {{first_name}},</p>",
        )
    )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        current_step=1,
        status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


def _stub_pipeline(monkeypatch, db_session, send_result: dict):
    """Neutralise everything between "enrollment is due" and the dispatcher.

    Leaves the soft-skip handler itself — the code under test — untouched.
    """
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

    # Run the task's coroutine on the test's own event loop instead of the fresh
    # loop Celery would spin up (which can't nest inside pytest-asyncio's).
    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _compose(db, enrollment, contact, company, step, subject, body):
        return ai_compose.ComposeResult("send", subject, body, reason="static template")

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    class _Optimizer:
        async def decide(self, *a, **k):
            return {"action": "send"}

    monkeypatch.setattr(outreach_tasks, "get_sequence_optimizer", lambda: _Optimizer())
    monkeypatch.setattr(outreach_tasks, "record_decision", lambda *a, **k: None)

    calls = []

    async def _send(db, **kwargs):
        calls.append(kwargs)
        return dict(send_result)

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return calls


async def test_warmup_soft_skip_returns_cleanly_instead_of_crashing(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The regression: a warm-up cap must not raise MissingGreenlet."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    # Stash the id as a plain UUID: the task shares this session, and its
    # rollback expires the instance, so a later ``enrollment.id`` would be a
    # lazy load — the very thing this module exists to keep out of the code.
    enrollment_id = enrollment.id
    _stub_pipeline(
        monkeypatch, db_session,
        {"status": "skipped", "reason": "warm-up daily limit reached (day 1: 5/5)"},
    )

    # Before the fix this raised sqlalchemy.exc.MissingGreenlet.
    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert "warm-up daily limit reached" in result["reason"]
    # The step number was read before the rollback, so it survives.
    assert result["step"] == 1


async def test_soft_skip_defers_next_send_instead_of_hot_looping(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A declined send is pushed into the future so the 5-min sweep backs off."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    # Stash the id as a plain UUID: the task shares this session, and its
    # rollback expires the instance, so a later ``enrollment.id`` would be a
    # lazy load — the very thing this module exists to keep out of the code.
    enrollment_id = enrollment.id
    was_due_at = enrollment.next_send_at
    _stub_pipeline(
        monkeypatch, db_session,
        {"status": "skipped", "reason": "warm-up daily limit reached (day 1: 5/5)"},
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    # Read the columns rather than the entity: the task shares this session, so
    # the identity map still holds the instance the task's rollback expired.
    next_send_at, status = (
        await db_session.execute(
            select(OutreachEnrollment.next_send_at, OutreachEnrollment.status)
            .where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()
    if next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)

    assert next_send_at > was_due_at
    assert next_send_at > datetime.now(timezone.utc)
    # Still active — a soft skip is a "not now", never an "abandon this contact".
    assert status == "active"


async def test_soft_skip_records_no_send_event_and_does_not_advance(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A declined send must not look like a send, nor consume the step."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    # Stash the id as a plain UUID: the task shares this session, and its
    # rollback expires the instance, so a later ``enrollment.id`` would be a
    # lazy load — the very thing this module exists to keep out of the code.
    enrollment_id = enrollment.id
    _stub_pipeline(
        monkeypatch, db_session,
        {"status": "skipped", "reason": "warm-up daily limit reached (day 1: 5/5)"},
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    event_ids = (
        await db_session.execute(
            select(OutreachEvent.id).where(OutreachEvent.enrollment_id == enrollment_id)
        )
    ).scalars().all()
    assert list(event_ids) == []

    current_step = (
        await db_session.execute(
            select(OutreachEnrollment.current_step)
            .where(OutreachEnrollment.id == enrollment_id)
        )
    ).scalar_one()
    assert current_step == 1


async def test_suppressed_dispatcher_result_is_also_soft(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """``suppressed``/``halted`` travel the same handler as ``skipped``."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    # Stash the id as a plain UUID: the task shares this session, and its
    # rollback expires the instance, so a later ``enrollment.id`` would be a
    # lazy load — the very thing this module exists to keep out of the code.
    enrollment_id = enrollment.id
    _stub_pipeline(
        monkeypatch, db_session,
        {"status": "halted", "reason": "sending kill switch engaged"},
    )

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))
    assert result["status"] == "halted"
    assert result["step"] == 1


# --- The AI-composed subject must be cleaned too ------------------------------
#
# ``clean_subject`` ran on the static mail-merge, but AI composition *replaces*
# that subject with a new string that never passed through it. The model is shown
# the static copy as an exemplar, so it happily echoes the "{{first_name}}, ..."
# shape — and a contact with no first name (43% of the list at the time) got a
# subject line that literally began with a comma.


async def test_ai_composed_subject_is_cleaned_of_merge_artifacts(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    async def _compose_with_artifact(db, enrollment, contact, company, step, subject, body):
        return ai_compose.ComposeResult(
            "send",
            ", who eats it when a part runs out of tol?",
            "<p>Hi there,</p>",
            reason="ai",
        )

    monkeypatch.setattr(ai_compose, "compose_step", _compose_with_artifact)

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert len(calls) == 1
    assert calls[0]["subject"] == "who eats it when a part runs out of tol?"
    assert not calls[0]["subject"].startswith(",")


# --- Capacity is checked before the expensive work ----------------------------


async def test_capped_pinned_sender_short_circuits_before_ai_composition(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A follow-up pinned to a capped mailbox must not pay for an AI call.

    Thread continuity pins steps 2+ to the mailbox that started the thread, so a
    capped sender means this enrollment is going nowhere until its ramp allows
    more. Composing copy first — every 5 minutes, per enrollment — is what
    exhausted the LLM quota.
    """
    import app.tasks.outreach as outreach_tasks
    from app.models.gmail_account import GmailAccount
    from app.services import ai_compose, warmup

    account = GmailAccount(
        id=_uuid.uuid4(), email="capped@apprend.tech", google_sub=str(_uuid.uuid4()),
        refresh_token_encrypted="x", is_active=True, status="connected",
    )
    db_session.add(account)
    await db_session.flush()

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment.sender_account_id = account.id
    await db_session.commit()
    enrollment_id = enrollment.id

    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent"})

    composed = []

    async def _compose(db, *a, **k):
        composed.append(1)
        raise AssertionError("AI composition ran despite the sender having no capacity")

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    async def _no_capacity(db, acct):
        return False, "warm-up daily limit reached (day 1: 5/5)"

    monkeypatch.setattr(warmup, "check_send_allowed", _no_capacity)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["sender_capped"] is True
    assert composed == []
    assert calls == []  # and nothing was handed to the dispatcher either
