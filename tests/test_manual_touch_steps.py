"""A non-email sequence step holds the cadence until the human does the touch.

The defect these pin: a "call"/"linkedin"/"video"/"direct_mail" step filed a
``FollowUp`` and advanced the sequence in the same breath. Two things followed
from that.

**The spacing was measured from the wrong event.** "Call them, then email two
days later" sent the email two days after the *reminder was created*. A rep who
made the call on Thursday had already sent the follow-up on Tuesday, referring to
a conversation that had not happened.

**A touch nobody made was indistinguishable from one they did.** No event was
written at all, so a five-step cadence where the call queue was never worked
reported the same shape as one where every call was made. The step was marked
done because a reminder had been filed — the cadence reporting on its own
paperwork.

So the step now holds, and resolves exactly one of three ways: completed
(cadence resumes from the completion), expired (cadence resumes, touch recorded
as *not made*), or still waiting. The tests below cover each, plus the two things
the hold must never do — strand an enrollment forever, or let a late completion
advance a cadence that already moved on.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import manual_touch

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------


async def _call_then_email(
    db: AsyncSession,
    contact: Contact,
    *,
    channel: str = "call",
    timeout_hours: int | None = None,
    follow_up_delay_days: int = 2,
):
    """A two-step cadence: a manual touch, then an email ``N`` days later.

    Returns ``(enrollment, step1, step2)``. The enrollment sits on step 1, due
    now.
    """
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Injection Molding — Combo", is_active=True
    )
    db.add(seq)
    await db.flush()

    step1 = SequenceStep(
        id=_uuid.uuid4(),
        sequence_id=seq.id,
        step_number=1,
        delay_days=0,
        channel=channel,
        task_timeout_hours=timeout_hours,
        subject_template="(call — no subject)",
        body_template="Ask about their scrap rate on the 400-tonne press.",
    )
    step2 = SequenceStep(
        id=_uuid.uuid4(),
        sequence_id=seq.id,
        step_number=2,
        delay_days=follow_up_delay_days,
        channel="email",
        subject_template="Following up on our call, {{first_name}}",
        body_template="<p>Good speaking with you.</p>",
    )
    db.add_all([step1, step2])

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
    return enrollment, step1, step2


def _stub_celery(monkeypatch, db_session):
    """Run ``send_sequence_step`` on the test's session and event loop."""
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)


async def _events(db: AsyncSession, enrollment_id) -> list[tuple[str, int]]:
    rows = (
        await db.execute(
            select(OutreachEvent.event_type, OutreachEvent.step_number)
            .where(OutreachEvent.enrollment_id == enrollment_id)
            .order_by(OutreachEvent.occurred_at)
        )
    ).all()
    return [(t, n) for t, n in rows]


async def _reload(db: AsyncSession, enrollment_id):
    return (
        await db.execute(
            select(
                OutreachEnrollment.current_step,
                OutreachEnrollment.status,
                OutreachEnrollment.next_send_at,
            ).where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()


def _aware(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- filing the task --------------------------------------------------------


async def test_manual_step_files_a_task_and_holds_the_cadence(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The step files a task, records it, and does NOT advance."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(db_session, sample_contact)
    enrollment_id = enrollment.id

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "channel_task_created"
    assert result["channel"] == "call"

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    assert follow_up.step_number == 1
    assert follow_up.channel == "call"
    assert follow_up.status == "pending"

    # The whole point: still on step 1, waiting on a human.
    current_step, status, next_send_at = await _reload(db_session, enrollment_id)
    assert current_step == 1
    assert status == "active"
    # Held out to the deadline, not left due — otherwise the 5-minute sweep
    # re-dispatches this enrollment forever while the task sits open.
    assert _aware(next_send_at) > datetime.now(timezone.utc)

    assert await _events(db_session, enrollment_id) == [("task_created", 1)]


async def test_the_filed_task_is_due_now_not_at_the_deadline(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A task that only becomes due when it stops mattering is never worked."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(db_session, sample_contact)

    await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment.id)
        )
    ).scalar_one()
    assert _aware(follow_up.due_date) <= datetime.now(timezone.utc) + timedelta(minutes=1)


# --- completion -------------------------------------------------------------


async def test_completing_the_task_advances_and_spaces_from_the_completion(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The follow-up email is two days after the CALL, not after the reminder."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(
        db_session, sample_contact, follow_up_delay_days=2
    )
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()

    # The rep makes the call a day after it was queued.
    called_at = datetime.now(timezone.utc) + timedelta(days=1)
    follow_up.status = "completed"
    follow_up.completed_at = called_at
    await db_session.flush()

    advanced = await manual_touch.complete_task(db_session, follow_up)
    await db_session.commit()

    assert advanced is not None
    assert advanced["next_step"] == 2

    current_step, status, next_send_at = await _reload(db_session, enrollment_id)
    assert current_step == 2
    assert status == "active"
    # Two days after the CALL (~3 days out), not two days after the filing.
    assert _aware(next_send_at) > called_at + timedelta(days=1, hours=12)

    assert await _events(db_session, enrollment_id) == [
        ("task_created", 1),
        ("task_completed", 1),
    ]


async def test_completing_through_the_api_advances_the_cadence(
    db_session: AsyncSession, client, sample_company: Company,
    sample_contact: Contact, monkeypatch,
):
    """PATCHing the task done is what a rep actually does — it must advance."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(db_session, sample_contact)
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up_id = follow_up.id

    resp = await client.patch(
        f"/api/v1/follow-ups/{follow_up_id}",
        json={"status": "completed"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # The task list needs to know this one is a sequence step, not a to-do.
    assert body["enrollment_id"] == str(enrollment_id)
    assert body["step_number"] == 1
    assert body["channel"] == "call"

    db_session.expire_all()
    current_step, status, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2
    assert status == "active"
    assert ("task_completed", 1) in await _events(db_session, enrollment_id)


# --- expiry -----------------------------------------------------------------


async def test_an_unworked_task_expires_and_the_touch_is_recorded_as_not_made(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Nobody called. The cadence resumes and says the touch did not happen."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(
        db_session, sample_contact, timeout_hours=24
    )
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    # Age the task past its deadline. The deadline is derived from
    # ``created_at`` + the step's timeout, so moving the creation back is what
    # "two days went by" looks like to the code under test.
    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up.created_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await db_session.flush()
    await db_session.commit()

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))
    assert result["status"] == "channel_task_expired"

    current_step, status, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2
    assert status == "active"

    events = await _events(db_session, enrollment_id)
    assert events == [("task_created", 1), ("task_expired", 1)]
    # Emphatically NOT a send: a touch nobody made must never reach the
    # denominator of a reply rate or the bounce rail's send count.
    assert not any(t == "sent" for t, _ in events)

    # And the stale task stops sitting in somebody's work queue.
    db_session.expire_all()
    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    assert follow_up.status == "dismissed"


async def test_a_task_still_inside_its_window_keeps_waiting(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A re-dispatch before the deadline must not expire the touch."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(
        db_session, sample_contact, timeout_hours=72
    )
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    # Force a second dispatch while the task is open and in date.
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.__table__.c.id == enrollment_id)
        .values(next_send_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db_session.commit()

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))
    assert result["status"] == "channel_task_waiting"

    current_step, _, next_send_at = await _reload(db_session, enrollment_id)
    assert current_step == 1
    # Re-armed, so the sweep backs off again instead of spinning on it.
    assert _aware(next_send_at) > datetime.now(timezone.utc)
    assert await _events(db_session, enrollment_id) == [("task_created", 1)]


async def test_a_dismissed_task_resumes_immediately_rather_than_waiting_it_out(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """An explicit "not doing this" is an answer — don't hold for the deadline."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(
        db_session, sample_contact, timeout_hours=336
    )
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up.status = "dismissed"
    follow_up.completed_at = datetime.now(timezone.utc)
    await db_session.flush()

    advanced = await manual_touch.complete_task(db_session, follow_up)
    await db_session.commit()

    assert advanced is not None and advanced["status"] == "dismissed"
    current_step, _, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2
    # Recorded as a touch that did not happen, not one that did.
    assert ("task_expired", 1) in await _events(db_session, enrollment_id)


async def test_a_missing_task_advances_rather_than_stranding_the_enrollment(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Holding on a task that no longer exists would strand the contact silently."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, step1, _ = await _call_then_email(db_session, sample_contact)
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    await db_session.delete(follow_up)
    await db_session.commit()

    enrollment = (
        await db_session.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id)
        )
    ).scalar_one()
    outcome = await manual_touch.resolve_task(db_session, enrollment, step1)
    await db_session.commit()

    assert outcome["status"] == "missing"
    current_step, status, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2
    assert status == "active"


# --- the hold must not double-advance ---------------------------------------


async def test_a_late_completion_does_not_advance_a_cadence_that_moved_on(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Completing an expired task must not skip the step the cadence reached."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    enrollment, _, _ = await _call_then_email(
        db_session, sample_contact, timeout_hours=24
    )
    enrollment_id = enrollment.id
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up.created_at = datetime.now(timezone.utc) - timedelta(hours=48)
    await db_session.flush()
    await db_session.commit()

    # Expire it — the cadence is now on step 2.
    await outreach_tasks.send_sequence_step.run(str(enrollment_id))
    current_step, _, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2

    # The rep marks the (auto-dismissed) task done a day later.
    db_session.expire_all()
    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up.status = "completed"
    follow_up.completed_at = datetime.now(timezone.utc)
    await db_session.flush()

    advanced = await manual_touch.complete_task(db_session, follow_up)
    await db_session.commit()

    assert advanced is None
    current_step, _, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2  # step 2 was not skipped


async def test_completing_an_unlinked_follow_up_touches_no_cadence(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Most follow-ups are hand-made. They must stay inert."""
    follow_up = FollowUp(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        due_date=datetime.now(timezone.utc),
        reason="Call Bob back about the quote",
        status="completed",
        completed_at=datetime.now(timezone.utc),
    )
    db_session.add(follow_up)
    await db_session.flush()

    assert await manual_touch.complete_task(db_session, follow_up) is None


# --- ending the sequence ----------------------------------------------------


async def test_a_manual_last_step_completes_the_enrollment(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """No step after the touch → the enrollment finishes, not stalls."""
    import app.tasks.outreach as outreach_tasks

    _stub_celery(monkeypatch, db_session)
    seq = OutreachSequence(id=_uuid.uuid4(), name="Single call", is_active=True)
    db_session.add(seq)
    await db_session.flush()
    db_session.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            channel="linkedin", task_timeout_hours=24,
            subject_template="(InMail)", body_template="Connect note",
        )
    )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db_session.add(enrollment)
    await db_session.flush()
    await db_session.commit()
    enrollment_id = enrollment.id

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    follow_up = (
        await db_session.execute(
            select(FollowUp).where(FollowUp.enrollment_id == enrollment_id)
        )
    ).scalar_one()
    follow_up.status = "completed"
    follow_up.completed_at = datetime.now(timezone.utc)
    await db_session.flush()
    await manual_touch.complete_task(db_session, follow_up)
    await db_session.commit()

    current_step, status, next_send_at = await _reload(db_session, enrollment_id)
    assert status == "completed"
    assert next_send_at is None


# --- the timeout resolver ---------------------------------------------------


class _Step:
    def __init__(self, hours):
        self.task_timeout_hours = hours
        self.id = "step"


class _Settings:
    manual_step_timeout_hours = 72
    manual_step_blocks_cadence = True


def test_step_timeout_overrides_the_org_default():
    assert manual_touch.timeout_for(_Step(6), _Settings()) == timedelta(hours=6)


def test_unset_step_timeout_falls_back_to_the_org_default():
    assert manual_touch.timeout_for(_Step(None), _Settings()) == timedelta(hours=72)


def test_timeout_is_clamped_at_both_ends():
    # 0 would expire the hold before the sweep that filed the task returned,
    # which looks exactly like the hold doing nothing.
    assert manual_touch.timeout_for(_Step(0), _Settings()) == timedelta(
        hours=manual_touch.MIN_TIMEOUT_HOURS
    )
    # A month-long hold outlives the relevance of the copy that follows it.
    assert manual_touch.timeout_for(_Step(10_000), _Settings()) == timedelta(
        hours=manual_touch.MAX_TIMEOUT_HOURS
    )


def test_an_unparseable_timeout_degrades_to_the_default_rather_than_raising():
    """A bad column value must not take down a send path."""
    assert manual_touch.timeout_for(_Step("soon"), _Settings()) == timedelta(hours=72)


async def test_hold_can_be_switched_off(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The escape hatch back to file-and-advance still records the touch."""
    import app.tasks.outreach as outreach_tasks
    from app.config import get_settings

    _stub_celery(monkeypatch, db_session)
    monkeypatch.setattr(
        get_settings(), "manual_step_blocks_cadence", False, raising=False
    )
    enrollment, _, _ = await _call_then_email(db_session, sample_contact)
    enrollment_id = enrollment.id

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    current_step, _, _ = await _reload(db_session, enrollment_id)
    assert current_step == 2
    # The recording is not the part you opt out of.
    assert await _events(db_session, enrollment_id) == [("task_created", 1)]


# --- analytics --------------------------------------------------------------


async def test_manual_touches_are_reported_separately_from_sends(
    db_session: AsyncSession, client, sample_company: Company,
    sample_contact: Contact,
):
    """A call step scores 0 on every email rate — its numbers live elsewhere."""
    enrollment, step1, _ = await _call_then_email(db_session, sample_contact)

    now = datetime.now(timezone.utc)
    for event_type in ("task_created", "task_created", "task_completed", "task_expired"):
        db_session.add(
            OutreachEvent(
                id=_uuid.uuid4(),
                enrollment_id=enrollment.id,
                step_number=1,
                event_type=event_type,
                occurred_at=now,
                event_metadata={"channel": "call"},
            )
        )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/dialtone/sequences/{enrollment.sequence_id}/analytics"
    )
    assert resp.status_code == 200
    step = next(s for s in resp.json()["steps"] if s["step_number"] == 1)

    assert step["tasks_created"] == 2
    assert step["tasks_completed"] == 1
    assert step["tasks_expired"] == 1
    # Resolved tasks, not filed ones: an open task is not a failure to work it.
    assert step["task_completion_rate"] == 0.5
    # And none of it leaked into the send funnel.
    assert step["sent"] == 0
    assert step["reply_rate"] == 0.0
