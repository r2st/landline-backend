"""The queue sweep must not hand the worker the same task over and over.

``process_outreach_queue`` runs every five minutes, selects every active
enrollment whose step is due, and calls ``send_sequence_step.delay(...)``. It
recorded nothing when it did. Dispatch is asynchronous by design — the task goes
to the broker and a worker picks it up when it gets there — so an enrollment
whose task is still queued is, on the next sweep, exactly what it was on the
last one: active, due, and dispatched again.

Nothing about that requires a fault. It is the normal state of a worker that is
behind. Each send does contact research, an LLM composition, a spam scan and an
SMTP round trip; a few hundred enrollments and a sweep's worth of work takes
longer than the interval between sweeps. A worker half an hour behind is holding
six copies of every task in its backlog, and each copy pays the full research and
composition bill before reaching the gate that stops it.

``send_claim`` is what stops the duplicate *message*. This lease is what stops
the duplicate *task* — cheaper, earlier, and it is the difference between a
queue that drains and one that grows.

Mutation-tested: removing the ``dispatched_at`` stamp fails
``test_second_sweep_does_not_requeue_an_in_flight_send``; removing the lease
check fails it too; making the lease permanent (never expiring) fails
``test_an_expired_lease_is_re_dispatched``.
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

pytestmark = pytest.mark.asyncio


async def _due_enrollment(db: AsyncSession, contact: Contact) -> OutreachEnrollment:
    seq = OutreachSequence(id=_uuid.uuid4(), name="Second-op capacity", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(),
            sequence_id=seq.id,
            step_number=1,
            delay_days=0,
            subject_template="Who eats the scrap?",
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


def _stub_sweep(monkeypatch, db_session):
    """Let the sweep run for real, but collect dispatches instead of queuing them.

    Everything the sweep consults — the kill switch, the send window, per-contact
    limits, the send-time optimizer — is left alone. Only the broker call is
    replaced, because that is the thing being counted.
    """
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _for_each_org(body, label=None):
        return await body(db_session)

    monkeypatch.setattr(outreach_tasks, "for_each_org", _for_each_org)

    # The window and the optimizer are about *when* to send, not how many times.
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

    class _Delay:
        @staticmethod
        def delay(enrollment_id):
            dispatched.append(enrollment_id)

    monkeypatch.setattr(outreach_tasks, "send_sequence_step", _Delay)
    return dispatched


async def test_second_sweep_does_not_requeue_an_in_flight_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The regression: two sweeps, one queued task, one dispatch.

    Before the lease the second sweep found the same enrollment in exactly the
    state the first one had left it — nothing marks a dispatch — and queued a
    second copy of the same send.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _due_enrollment(db_session, sample_contact)
    dispatched = _stub_sweep(monkeypatch, db_session)

    await outreach_tasks.process_outreach_queue.run()
    await outreach_tasks.process_outreach_queue.run()

    assert dispatched == [str(enrollment.id)], (
        "the sweep queued a second send for an enrollment whose first is still "
        "in the broker"
    )


async def test_the_first_sweep_still_dispatches(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The obvious counterweight: a lease that blocks everything is not a fix."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _due_enrollment(db_session, sample_contact)
    dispatched = _stub_sweep(monkeypatch, db_session)

    result = await outreach_tasks.process_outreach_queue.run()

    assert dispatched == [str(enrollment.id)]
    assert result["dispatched"] == 1


async def test_a_sweep_that_dies_mid_list_keeps_the_leases_it_took(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The stamp is durable at the moment the broker learns about the task.

    Stamped in the loop and committed at the *end* of the sweep, it would hold a
    row lock for the whole loop against a worker whose first act is to UPDATE
    that same row to claim its step — and it would be lost outright if the sweep
    died mid-list, which is precisely the run whose already-queued tasks are
    most likely to be dispatched a second time.

    So: dispatch one, blow up on the next, and the first one's lease must still
    be there.
    """
    import app.tasks.outreach as outreach_tasks

    first = await _due_enrollment(db_session, sample_contact)
    second_contact = Contact(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        first_name="Dana",
        email="dana@second-op.example",
        source="test",
    )
    db_session.add(second_contact)
    await db_session.flush()
    await _due_enrollment(db_session, second_contact)
    _stub_sweep(monkeypatch, db_session)

    dispatched = []

    class _DiesOnTheSecond:
        @staticmethod
        def delay(eid):
            if dispatched:
                raise RuntimeError("broker connection lost")
            dispatched.append(eid)

    monkeypatch.setattr(outreach_tasks, "send_sequence_step", _DiesOnTheSecond)

    # The sweep dies where the broker did. What matters is what survived it.
    with pytest.raises(RuntimeError):
        await outreach_tasks.process_outreach_queue.run()

    assert len(dispatched) == 1
    # Read past the identity map: the sweep's own commits are what this is
    # about, not the instances the test session is still holding.
    db_session.expire_all()
    stamped = (
        await db_session.execute(
            select(OutreachEnrollment.dispatched_at).where(
                OutreachEnrollment.id == _uuid.UUID(dispatched[0])
            )
        )
    ).scalar_one()
    assert stamped is not None, (
        "the lease for a task that is already in the broker was lost with the "
        "sweep that queued it"
    )


async def test_an_expired_lease_is_re_dispatched(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A task lost with its broker must not strand the enrollment forever.

    This is why the lease is a lease and not a flag: RabbitMQ restarts, workers
    are killed, queues are purged. An enrollment whose task evaporated has to
    become due again, and a permanent marker would mean it never does.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _due_enrollment(db_session, sample_contact)
    dispatched = _stub_sweep(monkeypatch, db_session)

    await outreach_tasks.process_outreach_queue.run()
    assert len(dispatched) == 1

    from app.config import get_settings

    settings = get_settings()

    stale = datetime.now(timezone.utc) - timedelta(
        minutes=getattr(settings, "dispatch_lease_minutes", 15) + 5
    )
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment.id)
        .values(dispatched_at=stale)
    )
    await db_session.commit()
    # A Core UPDATE does not touch the instance the sweep's SELECT will return
    # from the identity map (``expire_on_commit=False``), so the sweep would
    # keep reading the fresh stamp it wrote itself.
    db_session.expire_all()

    await outreach_tasks.process_outreach_queue.run()
    assert len(dispatched) == 2


async def test_the_lease_cannot_be_configured_below_the_sweep_interval(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A lease shorter than the five-minute sweep does nothing at all.

    It would expire between sweeps, so every enrollment would be re-dispatched
    exactly as before — the guard present, configured, and inert.
    """
    import app.tasks.outreach as outreach_tasks
    from app.config import get_settings

    settings = get_settings()

    monkeypatch.setattr(settings, "dispatch_lease_minutes", 1, raising=False)
    enrollment = await _due_enrollment(db_session, sample_contact)
    dispatched = _stub_sweep(monkeypatch, db_session)

    await outreach_tasks.process_outreach_queue.run()
    # Four minutes on: inside the 5-minute sweep interval, so a 1-minute lease
    # would have expired and a floored one has not.
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment.id)
        .values(dispatched_at=datetime.now(timezone.utc) - timedelta(minutes=4))
    )
    await db_session.commit()
    db_session.expire_all()
    await outreach_tasks.process_outreach_queue.run()

    assert len(dispatched) == 1


async def test_the_sweep_reports_what_it_held_back(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A count that climbs sweep after sweep is "the worker is not keeping up".

    Without it that condition surfaced only as an LLM bill nobody could explain.
    """
    import app.tasks.outreach as outreach_tasks

    await _due_enrollment(db_session, sample_contact)
    _stub_sweep(monkeypatch, db_session)

    await outreach_tasks.process_outreach_queue.run()
    second = await outreach_tasks.process_outreach_queue.run()

    assert second["skipped_in_flight"] == 1
    assert second["dispatched"] == 0
