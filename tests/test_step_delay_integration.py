"""The schedulers actually go through ``services.step_delay``.

``test_step_delay.py`` pins what the resolver computes. These pin that the code
which schedules real sends *asks* it — which is the part that was wrong before.
The due date used to be computed inline in five places, three applying the
adaptive factor and two not, so the queue sweep and the send task it dispatched
could disagree about whether a step was due.

So each test here drives a real scheduling path against a step configured in
hours (something no inline ``timedelta(days=step.delay_days)`` can express) and
checks where the enrollment was actually scheduled.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import adaptive_delay, step_delay
from app.tasks.outreach import _advance_step


async def _sequence_with_steps(db, step_specs):
    """``step_specs`` is a list of kwargs, one per step, numbered from 1."""
    seq = OutreachSequence(id=uuid.uuid4(), name="Cadence")
    db.add(seq)
    await db.flush()
    steps = []
    for n, spec in enumerate(step_specs, start=1):
        step = SequenceStep(
            id=uuid.uuid4(), sequence_id=seq.id, step_number=n,
            subject_template="S", body_template="B", **spec,
        )
        db.add(step)
        steps.append(step)
    await db.flush()
    return seq, steps


async def _enrollment(db, seq, contact, *, current_step=1, factor=1.0):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=current_step, status="active", adaptive_delay_factor=factor,
        enrolled_at=datetime.now(timezone.utc),
    )
    db.add(enr)
    await db.flush()
    return enr


# ── _advance_step ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_advance_step_schedules_an_hours_only_step_in_hours(
    db_session: AsyncSession, sample_contact
):
    """The inline ``timedelta(days=step.delay_days)`` this replaced would have
    scheduled a 6-hour follow-up for *right now* — ``delay_days`` is 0."""
    seq, _ = await _sequence_with_steps(
        db_session, [{"delay_days": 0}, {"delay_days": 0, "delay_hours": 6}]
    )
    enr = await _enrollment(db_session, seq, sample_contact)

    before = datetime.now(timezone.utc)
    await _advance_step(db_session, enr)

    assert enr.current_step == 2
    gap = enr.next_send_at - before
    assert timedelta(hours=5, minutes=55) < gap < timedelta(hours=6, minutes=5)


@pytest.mark.asyncio
async def test_advance_step_still_applies_the_adaptive_factor(
    db_session: AsyncSession, sample_contact
):
    """A day-granular step keeps the exact behaviour it had: factor 0.5 on a
    4-day step is a 2-day wait."""
    seq, _ = await _sequence_with_steps(
        db_session, [{"delay_days": 0}, {"delay_days": 4}]
    )
    enr = await _enrollment(db_session, seq, sample_contact, factor=0.5)

    before = datetime.now(timezone.utc)
    await _advance_step(db_session, enr)

    gap = enr.next_send_at - before
    assert timedelta(days=1, hours=23) < gap < timedelta(days=2, hours=1)


@pytest.mark.asyncio
async def test_advance_step_applies_the_factor_to_a_sub_day_step_too(
    db_session: AsyncSession, sample_contact
):
    seq, _ = await _sequence_with_steps(
        db_session, [{"delay_days": 0}, {"delay_days": 0, "delay_hours": 8}]
    )
    enr = await _enrollment(db_session, seq, sample_contact, factor=0.5)

    before = datetime.now(timezone.utc)
    await _advance_step(db_session, enr)

    gap = enr.next_send_at - before
    assert timedelta(hours=3, minutes=55) < gap < timedelta(hours=4, minutes=5)


@pytest.mark.asyncio
async def test_advance_step_completes_the_enrollment_at_the_last_step(
    db_session: AsyncSession, sample_contact
):
    seq, _ = await _sequence_with_steps(db_session, [{"delay_days": 0}])
    enr = await _enrollment(db_session, seq, sample_contact)

    await _advance_step(db_session, enr)

    assert enr.status == "completed"
    assert enr.completed_at is not None


@pytest.mark.asyncio
async def test_advance_step_jitter_is_reproducible_for_one_enrollment(
    db_session: AsyncSession, sample_contact
):
    """What ``_advance_step`` wrote must be what a later recomputation gets, or
    the send task would defer a step the sweep just dispatched."""
    seq, steps = await _sequence_with_steps(
        db_session,
        [{"delay_days": 0}, {"delay_days": 1, "delay_jitter_minutes": 120}],
    )
    enr = await _enrollment(db_session, seq, sample_contact)

    await _advance_step(db_session, enr)
    written = enr.next_send_at

    # Recompute from the same anchor the advance used.
    anchor = written - step_delay.effective_delay(steps[1], enr.adaptive_delay_factor) \
        - step_delay.jitter_for(steps[1], enr.id)
    assert step_delay.due_at(
        steps[1], anchor, factor=enr.adaptive_delay_factor, enrollment_id=enr.id
    ) == written


# ── adaptive_delay.reschedule_pending_step ────────────────────────────────────


@pytest.mark.asyncio
async def test_reschedule_pending_step_honours_hours(
    db_session: AsyncSession, sample_contact
):
    """It selected only ``delay_days`` before, so an hours-only step looked like
    an immediate one and the reschedule bailed out."""
    seq, _ = await _sequence_with_steps(
        db_session, [{"delay_days": 0}, {"delay_days": 0, "delay_hours": 10}]
    )
    enr = await _enrollment(db_session, seq, sample_contact, current_step=2)

    sent_at = datetime.now(timezone.utc) - timedelta(hours=2)
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1,
        event_type="sent", occurred_at=sent_at,
    ))
    await db_session.flush()

    moved = await adaptive_delay.reschedule_pending_step(db_session, enr)

    assert moved is True
    assert abs((enr.next_send_at - (sent_at + timedelta(hours=10)))) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_reschedule_pending_step_shortens_a_sub_day_wait_on_engagement(
    db_session: AsyncSession, sample_contact
):
    seq, _ = await _sequence_with_steps(
        db_session, [{"delay_days": 0}, {"delay_days": 0, "delay_hours": 12}]
    )
    enr = await _enrollment(db_session, seq, sample_contact, current_step=2)

    sent_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1,
        event_type="sent", occurred_at=sent_at,
    ))
    await db_session.flush()

    await adaptive_delay.register_engagement(db_session, enr)

    # Engagement halves the factor, so a 12-hour wait becomes six.
    assert enr.adaptive_delay_factor == 0.5
    assert abs((enr.next_send_at - (sent_at + timedelta(hours=6)))) < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_reschedule_pending_step_declines_an_immediate_step(
    db_session: AsyncSession, sample_contact
):
    """Nothing to reshape, and pulling it "in" would schedule a moment already
    past."""
    seq, _ = await _sequence_with_steps(db_session, [{"delay_days": 0}, {"delay_days": 0}])
    enr = await _enrollment(db_session, seq, sample_contact, current_step=2)

    assert await adaptive_delay.reschedule_pending_step(db_session, enr) is False


@pytest.mark.asyncio
async def test_reschedule_pending_step_declines_a_missing_step(
    db_session: AsyncSession, sample_contact
):
    seq, _ = await _sequence_with_steps(db_session, [{"delay_days": 1}])
    enr = await _enrollment(db_session, seq, sample_contact, current_step=9)

    assert await adaptive_delay.reschedule_pending_step(db_session, enr) is False


# ── The API surface ───────────────────────────────────────────────────────────

SEQUENCES = "/api/v1/outreach/sequences"


@pytest.mark.asyncio
async def test_cadence_fields_round_trip_through_the_sequence_api(client):
    resp = await client.post(SEQUENCES, json={
        "name": "Sub-day cadence",
        "steps": [
            {"step_number": 1, "delay_days": 0,
             "subject_template": "S1", "body_template": "B1"},
            {"step_number": 2, "delay_days": 0, "delay_hours": 6,
             "subject_template": "S2", "body_template": "B2"},
            {"step_number": 3, "delay_days": 3, "delay_business_days": True,
             "delay_jitter_minutes": 90,
             "subject_template": "S3", "body_template": "B3"},
        ],
    })
    assert resp.status_code in (200, 201), resp.text

    listing = await client.get(SEQUENCES)
    created = next(s for s in listing.json() if s["name"] == "Sub-day cadence")
    steps = sorted(created["steps"], key=lambda s: s["step_number"])

    assert steps[1]["delay_hours"] == 6
    assert steps[2]["delay_business_days"] is True
    assert steps[2]["delay_jitter_minutes"] == 90


@pytest.mark.asyncio
async def test_cadence_defaults_keep_an_unchanged_payload_unchanged(client):
    """A caller that has never heard of the new fields gets exactly the step it
    used to get."""
    resp = await client.post(SEQUENCES, json={
        "name": "Legacy payload",
        "steps": [{"step_number": 1, "delay_days": 2,
                   "subject_template": "S", "body_template": "B"}],
    })
    assert resp.status_code in (200, 201), resp.text

    listing = await client.get(SEQUENCES)
    step = next(s for s in listing.json() if s["name"] == "Legacy payload")["steps"][0]
    assert (step["delay_days"], step["delay_hours"]) == (2, 0)
    assert step["delay_business_days"] is False
    assert step["delay_jitter_minutes"] == 0


@pytest.mark.asyncio
async def test_the_api_rejects_a_day_sized_hours_value(client):
    """24 hours belongs in ``delay_days``, where business-day counting can see
    it — an hours-only "1 day" would silently skip the weekend hop."""
    resp = await client.post(SEQUENCES, json={
        "name": "Bad hours",
        "steps": [{"step_number": 1, "delay_hours": 24,
                   "subject_template": "S", "body_template": "B"}],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_the_api_rejects_jitter_beyond_the_cap(client):
    resp = await client.post(SEQUENCES, json={
        "name": "Bad jitter",
        "steps": [{"step_number": 1, "delay_jitter_minutes": 5000,
                   "subject_template": "S", "body_template": "B"}],
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_sequence_analytics_describes_the_cadence(
    client, db_session: AsyncSession
):
    """``delay_days`` alone stopped describing the wait once steps could be
    configured in hours and business days."""
    seq, _ = await _sequence_with_steps(
        db_session,
        [{"delay_days": 0}, {"delay_days": 2, "delay_hours": 4, "delay_business_days": True}],
    )
    await db_session.commit()

    resp = await client.get(f"/api/v1/dialtone/sequences/{seq.id}/analytics")
    assert resp.status_code == 200, resp.text
    steps = {s["step_number"]: s for s in resp.json()["steps"]}

    assert steps[1]["cadence"] == "immediately"
    assert steps[2]["cadence"] == "2 business days and 4 hours"
    assert steps[2]["delay_hours"] == 4
    assert steps[2]["delay_business_days"] is True


# ── Business days end to end ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_business_day_step_scheduled_over_a_weekend(
    db_session: AsyncSession, sample_contact
):
    seq, steps = await _sequence_with_steps(
        db_session,
        [{"delay_days": 0}, {"delay_days": 2, "delay_business_days": True}],
    )
    enr = await _enrollment(db_session, seq, sample_contact, current_step=2)

    friday = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
    due = step_delay.due_at(steps[1], friday, enrollment_id=enr.id)

    assert due.weekday() == 1  # Tuesday, not Sunday
