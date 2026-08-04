"""Pausing a campaign stops the campaign's mail.

``PATCH /sequences/{id}/status`` with ``{"status": "paused"}`` is the operator's
"stop this campaign" control. It sets ``status='paused'`` and syncs
``is_active=False``, and the console renders the sequence as paused.

Nothing on the send path had ever read either column. ``is_active`` gated
*enrolling* — ``POST /outreach/enrollments`` 400s on an inactive sequence — and
listing, so pausing stopped new contacts entering the campaign and did nothing
whatever about the contacts already in it. Every one of them kept receiving
steps 2..N on schedule, from a campaign the UI said was stopped, for as long as
the sequence had steps left.

This is the shape the kill switch, the unsubscribe link and the volume caps each
had before them: a control that reads as enforced on every surface except the one
where the mail leaves. It is quiet in the same way, too — a paused campaign that
keeps sending produces no error anywhere. The operator sees "paused", the
recipient sees step 3.

Both halves are pinned here, because they fail differently:

* the **queue sweep** must not dispatch a paused campaign's enrollments at all —
  that is the hot path, and filtering in SQL is what keeps a shelved 20k-contact
  blast from costing the sweep anything every five minutes;
* ``send_sequence_step`` must refuse one anyway, because a task queued *before*
  the pause landed is already in the broker and the sweep cannot recall it.

And it must be a **hold, not a stop**: the enrollment stays ``active`` and keeps
its ``next_send_at``, so resuming the sequence resumes the cadence. Pausing is
reversible by construction; a pause that ended every enrollment would need every
contact re-enrolled to undo, which is not what the button says.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep

pytestmark = pytest.mark.asyncio


async def _enrollment_due_now(
    db: AsyncSession, contact: Contact, *, is_active: bool = True
):
    """An active enrollment on step 1, due now, in a sequence of given liveness."""
    seq = OutreachSequence(
        id=_uuid.uuid4(),
        name="CNC Machining — Deal Cracker",
        is_active=is_active,
        status="active" if is_active else "paused",
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
    return seq, enrollment


def _stub_send_path(monkeypatch, db_session):
    """Neutralise everything between "enrollment is due" and the dispatcher.

    Records every dispatcher call so a test can assert on the *absence* of one —
    which is the whole question here.
    """
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

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

    sends = []

    async def _send(db, **kwargs):
        sends.append(kwargs)
        return {"status": "sent", "message_id": "m1", "thread_id": "t1"}

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return sends


# --- the send task ----------------------------------------------------------


async def test_a_paused_sequence_does_not_put_its_next_step_on_the_wire(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The regression. Before this, the dispatcher was called."""
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(
        db_session, sample_contact, is_active=False
    )
    enrollment_id = enrollment.id
    sends = _stub_send_path(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert result["reason"] == "sequence_paused"
    assert sends == [], "a paused campaign must not reach the dispatcher"


async def test_the_hold_leaves_the_enrollment_resumable(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A pause is reversible, so it must not end the enrollment or its schedule.

    If pausing set the enrollment to ``paused``/``completed``, un-pausing the
    sequence would restart nothing — the operator would have to re-enrol every
    contact, and ``pause_reason`` is NULL here so the out-of-office resume sweep
    would (correctly) refuse to touch them.
    """
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(
        db_session, sample_contact, is_active=False
    )
    enrollment_id = enrollment.id
    was_due_at = enrollment.next_send_at
    was_step = enrollment.current_step
    _stub_send_path(monkeypatch, db_session)

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    status, next_send_at, current_step = (
        await db_session.execute(
            select(
                OutreachEnrollment.status,
                OutreachEnrollment.next_send_at,
                OutreachEnrollment.current_step,
            ).where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()

    if next_send_at is not None and next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)  # SQLite drops tz

    assert status == "active"
    assert current_step == was_step, "a held step is not a consumed step"
    assert next_send_at == was_due_at, "the cadence is untouched, not deferred"

    # And nothing was recorded as sent.
    events = (
        await db_session.execute(
            select(OutreachEvent.event_type).where(
                OutreachEvent.enrollment_id == enrollment_id
            )
        )
    ).scalars().all()
    assert list(events) == []


async def test_an_active_sequence_still_sends(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The control. Without it, a gate that refused everything would pass above."""
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(
        db_session, sample_contact, is_active=True
    )
    enrollment_id = enrollment.id
    sends = _stub_send_path(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "sent", result
    assert len(sends) == 1


# --- the queue sweep --------------------------------------------------------


def _stub_sweep(monkeypatch, db_session):
    """Run ``process_outreach_queue`` against the test session, recording dispatch."""
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _for_each_org(body, label=None):
        return await body(db_session)

    monkeypatch.setattr(outreach_tasks, "for_each_org", _for_each_org)

    # The window and the optimizer decide *when* a due step goes, not whether
    # its campaign is live — which is the only question here.
    limiter = outreach_tasks.get_send_limiter()
    monkeypatch.setattr(
        type(limiter), "within_send_window", lambda self, tz=None: (True, "open")
    )

    class _NeverDefer:
        async def should_defer(self, *a, **k):
            return False

    monkeypatch.setattr(
        outreach_tasks, "get_send_time_optimizer", lambda: _NeverDefer()
    )

    dispatched = []
    monkeypatch.setattr(
        outreach_tasks.send_sequence_step,
        "delay",
        lambda enrollment_id, *a, **k: dispatched.append(str(enrollment_id)),
    )
    return dispatched


async def test_the_sweep_skips_a_paused_campaigns_enrollments(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Filtered in SQL: a shelved campaign costs the sweep nothing."""
    import app.tasks.outreach as outreach_tasks

    other = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id, first_name="Jo",
        email="jo@acme.test", source="test",
    )
    db_session.add(other)
    await db_session.flush()

    _paused_seq, paused_enr = await _enrollment_due_now(
        db_session, sample_contact, is_active=False
    )
    _live_seq, live_enr = await _enrollment_due_now(db_session, other, is_active=True)
    paused_id, live_id = str(paused_enr.id), str(live_enr.id)

    dispatched = _stub_sweep(monkeypatch, db_session)

    await outreach_tasks.process_outreach_queue.run()

    assert live_id in dispatched, "the live campaign must still go out"
    assert paused_id not in dispatched


async def test_resuming_the_sequence_puts_its_enrollments_back_in_the_sweep(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The other half of "hold, not stop": un-pausing has to actually resume."""
    import app.tasks.outreach as outreach_tasks

    seq, enrollment = await _enrollment_due_now(
        db_session, sample_contact, is_active=False
    )
    enrollment_id = str(enrollment.id)

    dispatched = _stub_sweep(monkeypatch, db_session)
    await outreach_tasks.process_outreach_queue.run()
    assert enrollment_id not in dispatched

    # What PATCH /sequences/{id}/status writes when resumed.
    seq.is_active = True
    seq.status = "active"
    await db_session.commit()

    dispatched.clear()
    await outreach_tasks.process_outreach_queue.run()
    assert enrollment_id in dispatched
