"""A send that keeps throwing must stop, back off, and become visible.

The defect these pin: ``send_sequence_step`` retried three times on an exception
and then let the task die, leaving the enrollment *exactly as it was* —
``status='active'``, ``next_send_at`` still in the past. Five minutes later the
queue sweep found it due and dispatched it again. For a transient fault that is
correct. For a permanent one it is an infinite loop: four attempts every five
minutes, a thousand a day, each doing real work before reaching the thing that
fails, and nothing anywhere reporting it. The enrollment looked healthy from
every angle — active, with a due date — and the only symptom was a contact who
silently stopped receiving mail.

So a failure is now recorded on the enrollment, backs off exponentially, and at
a threshold pauses with ``pause_reason='send_failures'``. The tests below cover
each of those, plus the three things the mechanism must never do: count Celery's
own retries as separate incidents, let a quarantine be undone by a sweep that
did not create it, or forget a failure because a *later* send happened to skip.
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
from app.services import send_failures

pytestmark = pytest.mark.asyncio


# --- fixtures ---------------------------------------------------------------


async def _enrolled(db: AsyncSession, contact: Contact, *, failures: int = 0):
    """An active enrollment on step 1, due now, with ``failures`` already on it."""
    seq = OutreachSequence(id=_uuid.uuid4(), name="CNC Q3", is_active=True)
    db.add(seq)
    await db.flush()

    db.add(SequenceStep(
        id=_uuid.uuid4(),
        sequence_id=seq.id,
        step_number=1,
        delay_days=0,
        channel="email",
        subject_template="Quick question about your quote turnaround",
        body_template="<p>Hello {{first_name}}.</p>",
    ))

    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        current_step=1,
        status="active",
        consecutive_failures=failures,
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


async def _reload(db: AsyncSession, enrollment_id):
    db.expire_all()
    return (
        await db.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id)
        )
    ).scalar_one()


def _aware(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _stub_celery(monkeypatch, db_session):
    """Run the task on the test's session and event loop."""
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)


# --- recording a failure ----------------------------------------------------


async def test_a_failure_is_recorded_and_the_enrollment_backs_off(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The defect in one test: a failure used to leave next_send_at in the past."""
    enrollment = await _enrolled(db_session, sample_contact)
    before = _aware(enrollment.next_send_at)
    assert before < datetime.now(timezone.utc)  # due, i.e. the sweep would fire it

    outcome = await send_failures.record_failure(
        db_session, enrollment.id, RuntimeError("SMTP 421 service unavailable")
    )

    assert outcome["consecutive_failures"] == 1
    assert outcome["quarantined"] is False

    row = await _reload(db_session, enrollment.id)
    assert row.consecutive_failures == 1
    assert "421" in row.last_error
    assert row.last_error_at is not None
    assert row.status == "active"  # one failure is not a quarantine
    # The whole point: no longer due, so the sweep cannot re-dispatch it on top
    # of the retry chain that just ran.
    assert _aware(row.next_send_at) > datetime.now(timezone.utc)


async def test_the_first_backoff_outlasts_celerys_own_retry_chain(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Otherwise the sweep dispatches a second, concurrent attempt at the same row.

    Celery retries at 60s + 120s + 180s = 6 minutes, and the queue sweeps every
    5. A backoff shorter than either is decorative.
    """
    assert send_failures.backoff_for(1) >= timedelta(minutes=10)


async def test_backoff_grows_with_each_failure_and_is_clamped(
    db_session: AsyncSession,
):
    """Exponential, but never below the sweep interval nor beyond half a day."""
    first = send_failures.backoff_for(1)
    second = send_failures.backoff_for(2)
    third = send_failures.backoff_for(3)
    assert first < second < third

    # Clamped at both ends, including against a counter that ran away.
    assert send_failures.backoff_for(0) >= timedelta(
        minutes=send_failures.MIN_BACKOFF_MINUTES
    )
    assert send_failures.backoff_for(9999) == timedelta(
        minutes=send_failures.MAX_BACKOFF_MINUTES
    )


async def test_a_missing_enrollment_records_nothing_rather_than_raising(
    db_session: AsyncSession,
):
    """Recording a failure must never become a second failure."""
    assert await send_failures.record_failure(
        db_session, _uuid.uuid4(), RuntimeError("gone")
    ) is None
    assert await send_failures.record_failure(
        db_session, "not-a-uuid", RuntimeError("gone")
    ) is None


async def test_a_long_error_is_truncated_rather_than_stored_whole(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A driver traceback repr runs to kilobytes; the useful part is the front."""
    enrollment = await _enrolled(db_session, sample_contact)

    await send_failures.record_failure(
        db_session, enrollment.id, RuntimeError("x" * 5000)
    )

    row = await _reload(db_session, enrollment.id)
    assert len(row.last_error) == send_failures.MAX_ERROR_CHARS


# --- quarantine -------------------------------------------------------------


async def test_repeated_failures_quarantine_the_enrollment(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """At the threshold it stops trying and starts being findable."""
    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)

    outcome = await send_failures.record_failure(
        db_session, enrollment.id, RuntimeError("550 relay denied")
    )

    assert outcome["quarantined"] is True
    row = await _reload(db_session, enrollment.id)
    assert row.status == "paused"
    assert row.pause_reason == send_failures.QUARANTINE_REASON
    assert row.consecutive_failures == threshold


async def test_a_quarantine_is_outside_every_automatic_resume(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Nothing un-pauses it on a timer — the fault behind it does not self-clear.

    The two sweeps that resume enrollments both match on what they themselves
    did: out-of-office on its own ``pause_reason``, the kill switch on
    ``kill_switch_paused``. A quarantine matches neither.
    """
    from app.services import out_of_office

    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)
    await send_failures.record_failure(db_session, enrollment.id, RuntimeError("boom"))

    row = await _reload(db_session, enrollment.id)
    assert row.pause_reason != out_of_office.OOO_PAUSE_REASON
    assert row.kill_switch_paused is False

    # And the OOO sweep genuinely leaves it alone.
    await out_of_office.resume_due(db_session)
    row = await _reload(db_session, enrollment.id)
    assert row.status == "paused"
    assert row.pause_reason == send_failures.QUARANTINE_REASON


async def test_quarantine_can_be_disabled_while_backoff_still_applies(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """The escape hatch bounds the cost without pausing anything."""
    class _NoQuarantine:
        send_failure_quarantine_threshold = 0
        send_failure_backoff_base_minutes = 15

    enrollment = await _enrolled(db_session, sample_contact, failures=20)

    outcome = await send_failures.record_failure(
        db_session, enrollment.id, RuntimeError("boom"), settings=_NoQuarantine()
    )

    assert outcome["quarantined"] is False
    row = await _reload(db_session, enrollment.id)
    assert row.status == "active"
    assert _aware(row.next_send_at) > datetime.now(timezone.utc)


async def test_a_failure_after_a_reply_does_not_clobber_the_outcome(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A send already in flight when the contact replied must not undo that.

    The task was dispatched, the contact replied, the send then failed. Marking
    the enrollment 'paused' here would erase the single outcome the whole system
    exists to produce and hand the contact back to a sweep.
    """
    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)
    enrollment.status = "replied"
    await db_session.commit()

    outcome = await send_failures.record_failure(
        db_session, enrollment.id, RuntimeError("SMTP 550")
    )

    assert outcome["quarantined"] is False
    row = await _reload(db_session, enrollment.id)
    assert row.status == "replied"
    assert row.pause_reason is None
    # Still recorded — the failure happened, it just does not move the state.
    assert row.consecutive_failures == threshold
    assert "550" in row.last_error


# --- release ----------------------------------------------------------------


async def test_releasing_a_quarantine_resets_the_count_and_resumes(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A stale count would re-quarantine on the next single blip."""
    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)
    await send_failures.record_failure(db_session, enrollment.id, RuntimeError("boom"))

    row = await _reload(db_session, enrollment.id)
    result = send_failures.clear_quarantine(row)
    await db_session.commit()

    assert result["released"] is True
    row = await _reload(db_session, enrollment.id)
    assert row.status == "active"
    assert row.consecutive_failures == 0
    assert row.last_error is None
    assert row.pause_reason is None
    assert _aware(row.next_send_at) <= datetime.now(timezone.utc) + timedelta(minutes=1)


async def test_releasing_without_resuming_leaves_it_paused(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """"I have seen this, it is not coming back" is a different answer."""
    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)
    await send_failures.record_failure(db_session, enrollment.id, RuntimeError("boom"))

    row = await _reload(db_session, enrollment.id)
    result = send_failures.clear_quarantine(row, resume=False)
    await db_session.commit()

    assert result["released"] is True
    row = await _reload(db_session, enrollment.id)
    assert row.status == "paused"
    assert row.consecutive_failures == 0
    assert row.pause_reason is None


async def test_a_pause_we_did_not_cause_is_refused(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """A dead domain or an operator's decision is not this module's to undo."""
    enrollment = await _enrolled(db_session, sample_contact)
    enrollment.status = "paused"
    enrollment.pause_reason = None  # the "leave it alone" marker
    await db_session.commit()

    row = await _reload(db_session, enrollment.id)
    result = send_failures.clear_quarantine(row)

    assert result["released"] is False
    assert result["reason"] == "not_quarantined"
    assert row.status == "paused"


# --- the send path ----------------------------------------------------------


def _fail_first_send(monkeypatch, db_session, error: Exception):
    """Make the send throw, and let the failure path have a working session.

    Injected at ``celery_session`` — the first thing ``send_sequence_step``
    opens — so the fault surfaces the way a real one does (out of the whole
    attempt) without dragging the test through recipient validation, an AI
    composition and an SMTP round-trip to reach it. The second call is the
    recording, which needs a live session precisely *because* the send's died.
    """
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    calls = {"n": 0}

    @asynccontextmanager
    async def _session():
        calls["n"] += 1
        if calls["n"] == 1:
            raise error
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)
    return calls


async def test_a_failure_with_retries_left_records_nothing(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Celery's three retries of one send are ONE incident, not four.

    Otherwise ``consecutive_failures`` counts attempts and a threshold of 5
    quarantines on the second bad afternoon rather than the fifth.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrolled(db_session, sample_contact)
    _fail_first_send(monkeypatch, db_session, RuntimeError("SMTP 421"))

    task = outreach_tasks.send_sequence_step
    task.push_request(retries=0)
    try:
        with pytest.raises(RuntimeError, match="421"):
            await task.run(str(enrollment.id))
    finally:
        task.pop_request()

    row = await _reload(db_session, enrollment.id)
    assert row.consecutive_failures == 0
    assert row.last_error is None


async def test_the_last_retry_records_the_incident_instead_of_dying(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The fix: the exhausted attempt writes the failure down and backs off.

    Before, this branch re-raised and the enrollment was left untouched —
    active, due, and about to be re-dispatched by the very next sweep.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrolled(db_session, sample_contact)
    _fail_first_send(monkeypatch, db_session, RuntimeError("SMTP 550 relay denied"))

    task = outreach_tasks.send_sequence_step
    task.push_request(retries=task.max_retries)
    try:
        result = await task.run(str(enrollment.id))
    finally:
        task.pop_request()

    assert result["status"] == "failed"
    assert result["consecutive_failures"] == 1
    assert result["quarantined"] is False

    row = await _reload(db_session, enrollment.id)
    assert row.consecutive_failures == 1
    assert "550" in row.last_error
    # The defect, pinned: no longer sitting due in the past.
    assert _aware(row.next_send_at) > datetime.now(timezone.utc)


async def test_the_last_retry_quarantines_once_the_threshold_is_reached(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """End to end: a fault that keeps happening eventually stops the enrollment."""
    import app.tasks.outreach as outreach_tasks

    threshold = send_failures.quarantine_threshold()
    enrollment = await _enrolled(db_session, sample_contact, failures=threshold - 1)
    _fail_first_send(monkeypatch, db_session, RuntimeError("SMTP 550 relay denied"))

    task = outreach_tasks.send_sequence_step
    task.push_request(retries=task.max_retries)
    try:
        result = await task.run(str(enrollment.id))
    finally:
        task.pop_request()

    assert result["quarantined"] is True
    row = await _reload(db_session, enrollment.id)
    assert row.status == "paused"
    assert row.pause_reason == send_failures.QUARANTINE_REASON


async def test_a_successful_send_clears_the_failure_state(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    """Consecutive is the point — a recovered enrollment is not a failing one."""
    enrollment = await _enrolled(db_session, sample_contact, failures=3)
    enrollment.last_error = "SMTP 421"
    enrollment.last_error_at = datetime.now(timezone.utc)
    await db_session.commit()

    row = await _reload(db_session, enrollment.id)
    send_failures.clear_failures(row)
    await db_session.commit()

    row = await _reload(db_session, enrollment.id)
    assert row.consecutive_failures == 0
    assert row.last_error is None
    assert row.last_error_at is None


# --- the API surface --------------------------------------------------------


async def test_failing_enrollments_are_listed_worst_first(
    client, db_session: AsyncSession, sample_company: Company,
    sample_contact: Contact,
):
    """A failing enrollment used to be indistinguishable from a healthy one."""
    mild = await _enrolled(db_session, sample_contact, failures=0)
    await send_failures.record_failure(db_session, mild.id, RuntimeError("blip"))

    bad = await _enrolled(db_session, sample_contact, failures=0)
    for _ in range(send_failures.quarantine_threshold()):
        await send_failures.record_failure(db_session, bad.id, RuntimeError("550 denied"))

    response = await client.get("/api/v1/dialtone/failures")
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert body["quarantined"] == 1
    assert body["retrying"] == 1
    # Worst first.
    assert body["enrollments"][0]["enrollment_id"] == str(bad.id)
    assert body["enrollments"][0]["quarantined"] is True
    assert body["enrollments"][1]["quarantined"] is False
    # One fault usually explains most of the list, so the errors are tallied.
    # Counted per enrollment (each stores only its latest), so both appear once.
    assert {e["error"] for e in body["top_errors"]} == {"blip", "550 denied"}
    assert all(e["count"] == 1 for e in body["top_errors"])


async def test_the_failures_list_can_be_narrowed_to_quarantined(
    client, db_session: AsyncSession, sample_company: Company,
    sample_contact: Contact,
):
    """Quarantined rows need a human; retrying ones need watching."""
    mild = await _enrolled(db_session, sample_contact)
    await send_failures.record_failure(db_session, mild.id, RuntimeError("blip"))

    response = await client.get(
        "/api/v1/dialtone/failures?quarantined_only=true"
    )
    assert response.status_code == 200
    assert response.json()["enrollments"] == []


async def test_releasing_through_the_api_puts_it_back_to_work(
    client, db_session: AsyncSession, sample_company: Company,
    sample_contact: Contact,
):
    enrollment = await _enrolled(
        db_session, sample_contact, failures=send_failures.quarantine_threshold() - 1
    )
    await send_failures.record_failure(db_session, enrollment.id, RuntimeError("boom"))

    response = await client.post(
        f"/api/v1/dialtone/failures/{enrollment.id}/release"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["released"] is True
    assert body["status"] == "active"

    row = await _reload(db_session, enrollment.id)
    assert row.status == "active"
    assert row.consecutive_failures == 0


async def test_releasing_an_unknown_enrollment_is_a_404(client):
    response = await client.post(
        f"/api/v1/dialtone/failures/{_uuid.uuid4()}/release"
    )
    assert response.status_code == 404
