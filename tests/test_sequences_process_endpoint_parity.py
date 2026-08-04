"""The manual dispatcher must not undo the interlocks the beat task holds.

``POST /sequences/process`` is the HTTP twin of ``process_outreach_queue``: same
job, reachable without Celery Beat, and the thing an operator presses when the
queue looks stuck. It was written alongside the sweep and then stopped tracking
it, keeping the kill switch and drifting on the two interlocks added afterwards.

Both drifts matter *because* of when the endpoint gets used:

* a paused campaign is one an operator just stopped, and this is the next button
  they press;
* the dispatch lease exists for a backlogged worker, which is exactly the state
  that makes someone trigger a manual run.

These pin behaviour, not implementation — the endpoint is deliberately *not* a
second copy of the sweep (no ranking, no send window, not per-org, because those
all run again inside ``send_sequence_step``). What it may not do is defeat a
control, which is a different thing from not implementing a heuristic.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep


@pytest.fixture
def captured_dispatches(monkeypatch):
    """Record what reaches the broker instead of sending it there."""
    sent: list[str] = []

    from app.tasks import outreach as outreach_tasks

    monkeypatch.setattr(
        outreach_tasks.send_sequence_step, "delay", lambda eid: sent.append(eid)
    )
    return sent


async def _seed(db, *, is_active=True, dispatched_at=None, due=True):
    company = Company(id=uuid.uuid4(), name="Kettle Works", country="US", source="t")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Ana",
        email=f"ana-{uuid.uuid4().hex[:8]}@kettle.example", source="t",
    )
    db.add(contact)
    seq = OutreachSequence(id=uuid.uuid4(), name="kettle", is_active=is_active)
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=1,
        subject_template="Hi {{first_name}}", body_template="<p>hello</p>",
        delay_days=0,
    ))
    now = datetime.now(timezone.utc)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status="active", current_step=1, enrolled_at=now - timedelta(days=1),
        next_send_at=now - timedelta(hours=1) if due else now + timedelta(days=1),
        dispatched_at=dispatched_at,
    )
    db.add(enrollment)
    await db.commit()
    return enrollment, seq


@pytest.mark.asyncio
async def test_paused_sequence_is_not_dispatched(client, db_session, captured_dispatches):
    """The operator stopped this campaign. The manual run must agree."""
    enrollment, _ = await _seed(db_session, is_active=False)

    resp = await client.post("/api/v1/sequences/process")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dispatched"] == 0
    # Filtered in SQL, so it is not even counted as an active enrollment that
    # was then skipped — a paused campaign should cost this endpoint nothing.
    assert body["active_enrollments"] == 0
    assert captured_dispatches == []


@pytest.mark.asyncio
async def test_live_sequence_is_dispatched(client, db_session, captured_dispatches):
    """The control above is a filter on paused campaigns, not on everything."""
    enrollment, _ = await _seed(db_session, is_active=True)

    resp = await client.post("/api/v1/sequences/process")

    assert resp.status_code == 200, resp.text
    assert resp.json()["dispatched"] == 1
    assert captured_dispatches == [str(enrollment.id)]


@pytest.mark.asyncio
async def test_a_leased_enrollment_is_not_queued_again(
    client, db_session, captured_dispatches
):
    """Its task is still in the broker; a second copy pays for research twice."""
    fresh_lease = datetime.now(timezone.utc) - timedelta(minutes=2)
    await _seed(db_session, dispatched_at=fresh_lease)

    resp = await client.post("/api/v1/sequences/process")

    body = resp.json()
    assert body["dispatched"] == 0
    assert body["skipped_in_flight"] == 1
    assert captured_dispatches == []


@pytest.mark.asyncio
async def test_an_expired_lease_is_redispatched(
    client, db_session, captured_dispatches
):
    """The lease is time-based and never cleared, so it has to expire.

    A task lost with its broker must not strand its enrollment forever — which
    is why the sweep floors the lease above the sweep interval rather than
    holding it until something explicitly releases it.
    """
    stale = datetime.now(timezone.utc) - timedelta(hours=4)
    enrollment, _ = await _seed(db_session, dispatched_at=stale)

    resp = await client.post("/api/v1/sequences/process")

    assert resp.json()["dispatched"] == 1
    assert captured_dispatches == [str(enrollment.id)]


@pytest.mark.asyncio
async def test_dispatching_takes_the_lease(client, db_session, captured_dispatches):
    """Otherwise the *next* call re-queues everything this one just queued.

    The lease is only an interlock if the dispatcher writes it; reading it
    without taking it makes the endpoint honour the sweep's leases and none of
    its own.
    """
    enrollment, _ = await _seed(db_session)
    enrollment_id = enrollment.id

    first = await client.post("/api/v1/sequences/process")
    assert first.json()["dispatched"] == 1

    db_session.expire_all()
    reloaded = (
        await db_session.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id)
        )
    ).scalars().first()
    assert reloaded.dispatched_at is not None

    second = await client.post("/api/v1/sequences/process")
    assert second.json()["dispatched"] == 0
    assert second.json()["skipped_in_flight"] == 1
    # One task in the broker for one due step, not two.
    assert captured_dispatches == [str(enrollment_id)]


@pytest.mark.asyncio
async def test_kill_switch_still_halts_everything(
    client, db_session, captured_dispatches
):
    """The one control this endpoint never lost. Pinned so it cannot be lost."""
    from app.services import system_settings

    await _seed(db_session)
    await system_settings.set_sending_enabled(db_session, False)
    await db_session.commit()

    resp = await client.post("/api/v1/sequences/process")

    assert resp.json()["status"] == "halted"
    assert captured_dispatches == []
