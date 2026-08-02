"""A step goes on the wire once — whatever happens to the transaction after it.

The defect these pin
--------------------
``send_sequence_step`` hands the message to Gmail and *then* writes down that it
did: the ``OutreachEvent``, the advanced ``current_step``, the thread ids, the
cleared failure counter, all in a transaction that commits afterwards. Anything
that fails in that window takes the record and leaves the mail — a dropped
``COMMIT``, a statement timeout, bookkeeping that raises, a worker killed. The
handler rolls back and re-raises; Celery retries in sixty seconds; the retry
loads an enrollment reading ``step 1, active, due`` — exactly what the rollback
restored — and sends the same cold email to the same person again.

Every existing gate is blind to this by construction. ``can_send_to_contact``
counts sends and finds none. ``can_send_to_company`` reads both send tables and
finds nothing. The due check sees a step still due. They are all asking the
database about a send that is only real on Gmail's side.

The second way in needs no fault at all: the queue sweep marked nothing when it
dispatched, so an enrollment whose task was still in the broker was due on the
next sweep too, and the next. A worker half an hour behind accumulated six
copies of every task in its backlog — six researches, six LLM compositions, and
without the claim, six messages.

Mutation-tested. Neutering the ``acquire`` call in ``send_sequence_step`` fails
six of these, ``test_retry_after_lost_commit_does_not_send_twice`` first;
releasing the claim on a transport exception fails
``test_a_transport_exception_keeps_its_claim``; *not* releasing it on a soft skip
fails ``test_soft_skip_releases_the_claim_so_the_retry_can_send``; deferring
unconditionally on a refusal fails
``test_a_duplicate_does_not_drag_an_advanced_cadence_forward``.
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
from app.services import send_claim

pytestmark = pytest.mark.asyncio


async def _two_step_enrollment(
    db: AsyncSession, contact: Contact, *, step: int = 1
) -> OutreachEnrollment:
    """An active enrollment sitting on ``step`` of a two-step sequence, due now.

    Two steps rather than one so "the enrollment advanced" and "the enrollment
    completed" stay tellable apart — a single-step sequence completes on its
    first send and hides which of the two happened.
    """
    seq = OutreachSequence(id=_uuid.uuid4(), name="Tooling teardown", is_active=True)
    db.add(seq)
    await db.flush()
    for number in (1, 2):
        db.add(
            SequenceStep(
                id=_uuid.uuid4(),
                sequence_id=seq.id,
                step_number=number,
                delay_days=0 if number == 1 else 3,
                subject_template="Step {}".format(number),
                body_template="<p>Hi {{first_name}},</p>",
            )
        )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        current_step=step,
        status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


def _stub_pipeline(monkeypatch, db_session, send_result: dict):
    """Neutralise everything between "enrollment is due" and the dispatcher.

    Same shape as ``tests/test_outreach_soft_skip.py`` — the claim itself, and
    every decision made around it, is left untouched. ``wire`` collects one
    entry per call that actually reached ``send_outreach_email``, which is the
    only number these tests are really about.
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

    wire = []

    async def _send(db, **kwargs):
        wire.append(kwargs)
        return dict(send_result)

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return wire


async def _claim_of(db: AsyncSession, enrollment_id):
    row = (
        await db.execute(
            select(OutreachEnrollment.send_claim_step, OutreachEnrollment.send_claimed_at)
            .where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()
    return row[0], row[1]


# --------------------------------------------------------------------------
# The claim primitive
# --------------------------------------------------------------------------


async def test_a_second_claim_on_the_same_step_is_refused(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """The whole guarantee, at its narrowest: one owner per (enrollment, step)."""
    enrollment = await _two_step_enrollment(db_session, sample_contact)

    first = await send_claim.acquire(db_session, enrollment.id, 1)
    second = await send_claim.acquire(db_session, enrollment.id, 1)

    assert first.acquired is True
    assert second.acquired is False
    assert second.held_since is not None


async def test_the_next_step_claims_freely(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A held claim blocks its own step, not the cadence.

    The claim is not cleared on success, so this is what stops it from becoming
    a permanent block on the enrollment it was protecting.
    """
    enrollment = await _two_step_enrollment(db_session, sample_contact)

    assert (await send_claim.acquire(db_session, enrollment.id, 1)).acquired is True
    assert (await send_claim.acquire(db_session, enrollment.id, 2)).acquired is True

    step, _ = await _claim_of(db_session, enrollment.id)
    assert step == 2


async def test_release_lets_the_step_be_claimed_again(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A step the dispatcher declined is unsent, so it must be re-claimable."""
    enrollment = await _two_step_enrollment(db_session, sample_contact)

    await send_claim.acquire(db_session, enrollment.id, 1)
    assert await send_claim.release(db_session, enrollment.id, 1) is True
    assert (await send_claim.acquire(db_session, enrollment.id, 1)).acquired is True


async def test_release_cannot_eat_a_later_steps_claim(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A stale release for step 1 must not unlock a step 2 that is in flight.

    Without the step guard, a late soft-skip handler from a previous step would
    hand back the claim protecting the send happening right now.
    """
    enrollment = await _two_step_enrollment(db_session, sample_contact)
    await send_claim.acquire(db_session, enrollment.id, 2)

    assert await send_claim.release(db_session, enrollment.id, 1) is False

    step, _ = await _claim_of(db_session, enrollment.id)
    assert step == 2


async def test_a_missing_enrollment_is_not_reported_as_held(db_session: AsyncSession):
    """Rowcount 0 has two causes and only one of them is a claim.

    Reporting a nonexistent enrollment as "held by somebody" would make the
    caller back off waiting for a worker that does not exist. The caller is
    allowed through to its own not-found check instead.
    """
    result = await send_claim.acquire(db_session, _uuid.uuid4(), 1)
    assert result.acquired is True


async def test_a_fresh_claim_is_not_stale(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    enrollment = await _two_step_enrollment(db_session, sample_contact)
    await send_claim.acquire(db_session, enrollment.id, 1)

    refused = await send_claim.acquire(db_session, enrollment.id, 1)
    assert refused.stale is False


async def test_the_grace_window_has_a_floor(monkeypatch):
    """Config can raise the grace, never lower it into reconciling live sends.

    A send does research, an LLM composition, a spam scan and an SMTP round
    trip. A grace of one minute would call a healthy concurrent worker "stale"
    and reconcile the step out from under it — writing a send event for a
    message that is still being composed.
    """
    from app.config import get_settings

    settings = get_settings()

    monkeypatch.setattr(settings, "send_claim_grace_minutes", 1, raising=False)
    assert send_claim.in_flight_grace() == timedelta(
        minutes=send_claim.MIN_GRACE_MINUTES
    )

    monkeypatch.setattr(settings, "send_claim_grace_minutes", 45, raising=False)
    assert send_claim.in_flight_grace() == timedelta(minutes=45)


# --------------------------------------------------------------------------
# The send task
# --------------------------------------------------------------------------


async def test_retry_after_lost_commit_does_not_send_twice(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The headline regression.

    The message reaches Gmail; the transaction recording it then dies. Celery
    retries. Before the claim, the retry found ``step 1, active, due`` — because
    that is what the rollback restored — and sent the same cold email again.
    """
    import app.tasks.outreach as outreach_tasks
    from app.services import step_delay

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    wire = _stub_pipeline(
        monkeypatch, db_session, {"status": "sent", "message_id": "m1"}
    )

    # Fail the bookkeeping *after* the wire, once. ``due_at`` is called only on
    # the post-send advance, which is squarely inside the window this is about.
    real_due_at = step_delay.due_at
    boom = {"fired": False}

    def _due_at(*a, **k):
        if not boom["fired"]:
            boom["fired"] = True
            raise RuntimeError("connection reset by peer")
        return real_due_at(*a, **k)

    monkeypatch.setattr(step_delay, "due_at", _due_at)

    with pytest.raises(RuntimeError):
        await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert len(wire) == 1, "the first attempt must reach the wire"

    # The Celery retry, against the state the rollback left behind.
    second = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert len(wire) == 1, (
        "the retry re-sent a message that had already gone out to the prospect"
    )
    assert second["status"] in ("skipped", "reconciled")


async def test_the_claim_is_held_before_the_dispatcher_is_called(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """Ordering is the property that makes the narrow race safe.

    Two tasks for one enrollment need no fault to both reach the wire: both pass
    the due check, and every gate that counts rows clears them, because neither
    has written a row yet. What decides is the claim's conditional UPDATE — but
    only if it is taken *before* the message goes out. Claimed after, it records
    history and prevents nothing; claimed earlier than the last gate, it has to
    be released on every path that declines.

    So this pins the position: at the instant the dispatcher is entered, the
    claim is already committed and any sibling asking for it is refused.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    observed = {}

    async def _send(db, **kwargs):
        observed["claim_at_wire"] = (
            await db.execute(
                select(OutreachEnrollment.send_claim_step).where(
                    OutreachEnrollment.id == enrollment_id
                )
            )
        ).scalar_one()
        return {"status": "sent", "message_id": "m1"}

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert observed["claim_at_wire"] == 1, (
        "the step reached the dispatcher without being claimed — a concurrent "
        "worker would have been cleared to send it too"
    )


async def test_a_successful_send_records_the_claim_it_used(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The claim survives the send, and reads as the step that shipped."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "sent"
    step, claimed_at = await _claim_of(db_session, enrollment_id)
    assert step == 1
    assert claimed_at is not None


async def test_soft_skip_releases_the_claim_so_the_retry_can_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A declined send is unsent, and must not be blocked forever by its claim.

    Warm-up caps, suppression and the kill switch all come back as a *returned*
    status — the dispatcher decided, it did not fail. Keeping the claim on those
    would turn every capped mailbox into a permanently skipped step.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    wire = _stub_pipeline(
        monkeypatch, db_session,
        {"status": "skipped", "reason": "warm-up daily limit reached (day 1: 5/5)"},
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert len(wire) == 1
    step, _ = await _claim_of(db_session, enrollment_id)
    assert step is None, "a step the dispatcher declined is still owed a send"

    # And the retry, once the cap clears, actually sends it.
    monkeypatch.setattr(
        outreach_tasks, "send_outreach_email",
        lambda db, **kw: _appended(wire, kw),
    )
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment_id)
        .values(next_send_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    )
    await db_session.commit()

    await outreach_tasks.send_sequence_step.run(str(enrollment_id))
    assert len(wire) == 2


async def _appended(wire, kwargs):
    wire.append(kwargs)
    return {"status": "sent", "message_id": "m2"}


async def test_a_transport_exception_keeps_its_claim(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """An exception out of the dispatcher is ambiguous, and treated as such.

    ``send_outreach_email`` does real work after the transport returns — the
    per-account daily counter, the warm-up start — so a raise from it does not
    prove the message stayed home. The daily slot and the pacing window are
    handed back because being wrong about those is cheap; the claim is not.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(monkeypatch, db_session, {"status": "sent"})

    async def _explode(db, **kwargs):
        raise RuntimeError("smtp connection reset after DATA")

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _explode)

    with pytest.raises(RuntimeError):
        await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    step, _ = await _claim_of(db_session, enrollment_id)
    assert step == 1, "an ambiguous send was handed back for a second attempt"


async def test_a_fresh_duplicate_defers_rather_than_reconciling(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A sibling that is still running gets to finish and record its own send.

    Reconciling here would write a send event for a message the other worker is
    about to write itself — a double count, and a step advanced twice.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    wire = _stub_pipeline(monkeypatch, db_session, {"status": "sent"})

    # Somebody else took the claim a moment ago.
    await send_claim.acquire(db_session, enrollment_id, 1)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert result["reason"] == "duplicate_send_suppressed"
    assert wire == []

    current_step, events = await _step_and_events(db_session, enrollment_id)
    assert current_step == 1, "the sibling's step was advanced out from under it"
    assert events == 0

    # Deferred, so the sweep stops re-dispatching on top of a live send.
    next_send_at = (
        await db_session.execute(
            select(OutreachEnrollment.next_send_at).where(
                OutreachEnrollment.id == enrollment_id
            )
        )
    ).scalar_one()
    if next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)
    assert next_send_at > datetime.now(timezone.utc)


async def test_a_duplicate_does_not_drag_an_advanced_cadence_forward(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The deferral must not apply once the sibling has already advanced.

    Step 2 is scheduled days out. A blanket ``_defer_enrollment`` on the refusal
    path would pull it back to an hour from now — the duplicate guard shortening
    the cadence it exists to protect.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    _stub_pipeline(monkeypatch, db_session, {"status": "sent"})

    # The sibling finished: step 1 claimed and sent, now on step 2, days out.
    await send_claim.acquire(db_session, enrollment_id, 1)
    days_out = datetime.now(timezone.utc) + timedelta(days=3)
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment_id)
        .values(current_step=2, next_send_at=days_out)
    )
    await db_session.commit()

    await outreach_tasks._handle_duplicate_claim(
        db_session,
        str(enrollment_id),
        1,
        send_claim.ClaimResult(False, datetime.now(timezone.utc)),
    )

    next_send_at = (
        await db_session.execute(
            select(OutreachEnrollment.next_send_at).where(
                OutreachEnrollment.id == enrollment_id
            )
        )
    ).scalar_one()
    if next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)
    assert next_send_at > datetime.now(timezone.utc) + timedelta(days=2)


# --------------------------------------------------------------------------
# Reconciliation — the stale claim
# --------------------------------------------------------------------------


async def test_a_stale_claim_is_reconciled_not_re_sent(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The attempt that took this claim never came back. The mail still went.

    Without this the enrollment is re-dispatched every five minutes and refused
    every time: no duplicate reaches the recipient, but the cadence stops dead
    and each sweep pays for a composition to be told no.
    """
    import app.tasks.outreach as outreach_tasks

    enrollment = await _two_step_enrollment(db_session, sample_contact)
    enrollment_id = enrollment.id
    wire = _stub_pipeline(monkeypatch, db_session, {"status": "sent"})

    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment_id)
        .values(send_claim_step=1, send_claimed_at=long_ago)
    )
    await db_session.commit()

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "reconciled"
    assert wire == [], "a lost send was re-sent instead of recovered"

    current_step, events = await _step_and_events(db_session, enrollment_id)
    assert current_step == 2, "the cadence stayed wedged on the lost step"
    assert events == 1


async def test_the_reconciled_send_counts_and_says_it_is_unconfirmed(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """It is a ``sent`` event, flagged — both halves matter.

    ``sent`` because the message did reach the wire, and every deliverability
    rate in this system divides by attempts; a bad afternoon that loses its own
    records must not flatter itself by shrinking its denominator. Flagged
    because nobody can prove what arrived, and the event carries no subject or
    body — inventing either would put copy in the audit trail that was never
    demonstrably sent.
    """
    enrollment = await _two_step_enrollment(db_session, sample_contact)
    claimed_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment.id)
        .values(send_claim_step=1, send_claimed_at=claimed_at)
    )
    await db_session.commit()

    assert await send_claim.reconcile(db_session, enrollment.id, 1) is True

    event = (
        await db_session.execute(
            select(OutreachEvent).where(OutreachEvent.enrollment_id == enrollment.id)
        )
    ).scalar_one()
    assert event.event_type == "sent"
    assert event.event_metadata["reconciled"] is True
    assert event.event_metadata["wire_outcome"] == "unconfirmed"
    assert "body_html" not in event.event_metadata
    assert "subject" not in event.event_metadata
    # Anchored on when the message actually went out, not on when a sweep
    # noticed — otherwise every recovered enrollment silently gains the delay.
    occurred = event.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=timezone.utc)
    assert abs((occurred - claimed_at).total_seconds()) < 5


async def test_reconcile_declines_when_the_enrollment_already_moved_on(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """The original attempt came back late. There is nothing to recover."""
    enrollment = await _two_step_enrollment(db_session, sample_contact, step=2)
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment.id)
        .values(
            send_claim_step=1,
            send_claimed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )
    await db_session.commit()

    assert await send_claim.reconcile(db_session, enrollment.id, 1) is False

    _, events = await _step_and_events(db_session, enrollment.id)
    assert events == 0


async def test_reconcile_completes_an_enrollment_with_no_next_step(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A lost last send finishes the enrollment rather than leaving it active.

    An ``active`` enrollment past its final step is dispatched forever by a
    sweep that can never find a step to send.
    """
    enrollment = await _two_step_enrollment(db_session, sample_contact, step=2)
    await db_session.execute(
        OutreachEnrollment.__table__.update()
        .where(OutreachEnrollment.id == enrollment.id)
        .values(
            send_claim_step=2,
            send_claimed_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )
    await db_session.commit()

    assert await send_claim.reconcile(db_session, enrollment.id, 2) is True

    status, next_send_at = (
        await db_session.execute(
            select(OutreachEnrollment.status, OutreachEnrollment.next_send_at)
            .where(OutreachEnrollment.id == enrollment.id)
        )
    ).one()
    assert status == "completed"
    assert next_send_at is None


async def _step_and_events(db: AsyncSession, enrollment_id):
    step = (
        await db.execute(
            select(OutreachEnrollment.current_step).where(
                OutreachEnrollment.id == enrollment_id
            )
        )
    ).scalar_one()
    events = len(
        (
            await db.execute(
                select(OutreachEvent.id).where(
                    OutreachEvent.enrollment_id == enrollment_id
                )
            )
        )
        .scalars()
        .all()
    )
    return step, events
