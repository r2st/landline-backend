"""The business-hours send window holds for every dispatch path, not just the sweep.

``within_send_window`` had exactly one caller: ``process_outreach_queue``, the
Celery beat sweep. But that sweep is not the only way an enrollment reaches
``send_sequence_step`` — ``POST /api/v1/sequences/process`` describes itself as
"the HTTP-triggered alternative to the process_outreach_queue Celery beat task"
and dispatches the send task directly. So the anti-spam gate that exists because
cold mail arriving at 3 AM on a Sunday reads as a bot was one HTTP call away
from not applying.

These tests pin the gate at the send, where every caller passes.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.send_limiter import get_send_limiter

pytestmark = pytest.mark.asyncio


def _bind_task_to_test_session(monkeypatch, db_session):
    """Run the task's coroutine on this test's loop and session.

    Same wiring as tests/test_outreach_soft_skip.py: Celery would otherwise open
    its own event loop and session, neither of which can see the rows this test
    just wrote.
    """
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)


def _set_window(monkeypatch, *, open_: bool):
    limiter = get_send_limiter()
    verdict = (True, "open (test)") if open_ else (False, "outside send window (test)")
    monkeypatch.setattr(
        type(limiter), "within_send_window", lambda self, tz=None: verdict
    )


async def _enrollment_due_now(
    db: AsyncSession, company: Company, contact: Contact
) -> OutreachEnrollment:
    """An active enrollment sitting on a due email step."""
    seq = OutreachSequence(id=_uuid.uuid4(), name="Window Seq", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(),
            sequence_id=seq.id,
            step_number=1,
            delay_days=0,
            subject_template="Hello {{first_name}}",
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
    await db.commit()
    return enrollment


async def test_send_task_refuses_outside_the_window(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The gate now lives at the send, so it applies however the task was reached."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    _bind_task_to_test_session(monkeypatch, db_session)
    _set_window(monkeypatch, open_=False)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert result.get("outside_send_window") is True


async def test_a_held_enrollment_is_deferred_not_dropped(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The step is still due — just not right now. It must come back around.

    Marking it completed or paused would cost the prospect the rest of the
    sequence because a message happened to fall on a Sunday.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    before = enrollment.next_send_at
    _bind_task_to_test_session(monkeypatch, db_session)
    _set_window(monkeypatch, open_=False)

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    db_session.expire_all()
    refreshed = (
        await db_session.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id)
        )
    ).scalar_one()

    assert refreshed.status == "active"
    after = refreshed.next_send_at
    assert after is not None
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    if before.tzinfo is None:
        before = before.replace(tzinfo=timezone.utc)
    assert after > before, "a held enrollment must be pushed out, not left hot-looping"


async def test_an_open_window_does_not_hold_the_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Guard against the gate being a blanket refusal.

    Only the window's verdict is asserted — an open window must not produce
    ``outside_send_window``. Whatever the gates below it decide is their business.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_company, sample_contact)
    enrollment_id = enrollment.id
    _bind_task_to_test_session(monkeypatch, db_session)
    _set_window(monkeypatch, open_=True)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result.get("outside_send_window") is not True
