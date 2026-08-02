"""At-most-once: a step goes on the wire once, whatever happens afterwards.

The problem this exists for
---------------------------
``send_sequence_step`` sends the message and then writes down that it did. The
``OutreachEvent``, the advanced ``current_step``, the Gmail thread ids, the
cleared failure counter — all of that lands in a transaction that commits
*after* the dispatcher has already handed the message to Gmail. Anything that
fails in between takes the record and leaves the mail:

* ``COMMIT`` itself fails — connection reset, statement timeout, deadlock.
  Ordinary in production and by definition unrecoverable in that session.
* the bookkeeping between send and commit raises — a malformed
  ``sender_account_id``, a lazy load on an expired instance, a draft row that
  vanished.
* the worker is killed after the send and before the commit.

The handler rolls back and re-raises. Celery retries in sixty seconds. The retry
loads an enrollment that says ``current_step=1``, ``status='active'``,
``next_send_at`` in the past — because that is precisely the state the rollback
restored — walks the same gates, and sends the same cold email to the same
person again. The one thing a cold-email system cannot do to a prospect it is
trying to build credibility with, done by the safety mechanism.

Every other gate in this system reasons about *state in the database*, which is
exactly the thing the failing window has not written yet. ``can_send_to_contact``
counts sends and finds none. ``can_send_to_company`` reads both send tables and
finds nothing. The due check sees a step still due. They are all correct and all
blind, because the send they need to know about is only real on Gmail's side.

The mechanism
-------------
Claim ``(enrollment, step)`` in a transaction of its own, committed *before* the
dispatcher call. The claim is the only fact that survives the rollback of the
send it describes, so it is the only thing a retry can ask.

The claim is a conditional UPDATE:

    UPDATE outreach_enrollments SET send_claim_step = :n, send_claimed_at = now()
    WHERE id = :e AND (send_claim_step IS NULL OR send_claim_step <> :n)

The row lock makes it atomic against every other worker, so this also covers the
case no fault is involved in: two tasks for one enrollment running concurrently,
both past the due check, both about to send. One gets rowcount 1 and sends; the
other gets 0 and does not. No advisory locks, no new table, no broker
coordination — the guarantee is the row.

It is not cleared on success. Claiming step 2 is a different value, so leaving
it means the column reads as "the last step that reached the wire", which is
worth being able to ask. It *is* cleared when the attempt is known not to have
reached the transport: a soft skip (the dispatcher declined — warm-up cap,
suppression, kill switch) or an exception raised on the way to the dispatcher.

Why at-most-once and not at-least-once
--------------------------------------
A claim that exists is ambiguous: it says an attempt reached the dispatcher, not
that Gmail accepted it. Resolving that ambiguity by re-sending would reintroduce
the duplicate for the sake of a send that may already have arrived. Resolving it
by never sending costs, at worst, one missing touch in a cadence — invisible to
the recipient, who cannot miss a message they were never promised. For cold
email those are not close, so an ambiguous claim is never re-sent.

That leaves the enrollment to deal with. A refused claim on a step the
enrollment is *still sitting on* means one of two things, told apart by age:

* fresh (inside :func:`in_flight_grace`) — another worker is mid-send right now.
  Back off and let it finish; it will advance the enrollment itself.
* stale — the attempt that took this claim never came back to write anything.
  The message went out and its bookkeeping is lost. :func:`reconcile` records
  the send that is missing and advances the cadence, so the enrollment moves on
  instead of being re-dispatched and refused every five minutes forever.

The reconciled event is recorded as ``sent`` and flagged in its metadata. Not as
a distinct event type: a send whose outcome we could not read still went out, so
it belongs in the denominator of every rate the deliverability rails compute —
which must count attempts, not confirmations, or a bad afternoon flatters itself
by losing its own records.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)

# Floor on the in-flight grace. A send does research, an LLM composition, a spam
# scan and an SMTP round trip; a grace shorter than that would call a healthy
# concurrent send "stale" and reconcile a step out from under a worker that is
# about to deliver it.
MIN_GRACE_MINUTES = 10


def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


def in_flight_grace(settings: Any = None) -> timedelta:
    """How long a claim is presumed to belong to a worker that is still running.

    Read from config so an operator whose composition step is unusually slow can
    raise it, floored at :data:`MIN_GRACE_MINUTES` so it can never be lowered to
    the point where it reconciles live sends.
    """
    s = settings or _settings()
    minutes = getattr(s, "send_claim_grace_minutes", 15)
    return timedelta(minutes=max(int(minutes), MIN_GRACE_MINUTES))


class ClaimResult(NamedTuple):
    """Outcome of :func:`acquire`.

    ``acquired`` is the only thing the caller needs to decide whether to send.
    ``held_since`` is set only on a refusal, and is what tells an in-flight
    sibling apart from an abandoned one.
    """

    acquired: bool
    held_since: Optional[datetime] = None

    @property
    def stale(self) -> bool:
        """A refused claim old enough that nothing is plausibly still running."""
        if self.acquired or self.held_since is None:
            return False
        held = self.held_since
        if held.tzinfo is None:
            held = held.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - held >= in_flight_grace()


async def acquire(db: AsyncSession, enrollment_id: Any, step_number: int) -> ClaimResult:
    """Claim ``(enrollment, step)`` for the wire. Commits before returning.

    The commit is the point, and it is the caller's own session that must do it.
    A claim left pending dies with the rollback that loses the send record —
    the exact fault this module exists to survive. Committing here closes that
    transaction, so the rollback that comes later can only reach work started
    after this line, and the claim stands.

    On the *send's* session rather than a second one, deliberately. By this
    point the send task has dirtied ``optimization_context`` on this same
    enrollment row, and the next autoflush turns that into an uncommitted UPDATE
    holding a row lock. A claim issued from another connection would wait on
    that lock while the send waited on the claim — a worker wedged until the
    statement timeout, on every send.

    ``skip_org_filter`` throughout: this is a safety interlock keyed by primary
    key, and a claim that silently no-ops because the org context was not bound
    would fail *open* — it would report the step unclaimed and send again.
    """
    eid = enrollment_id if isinstance(enrollment_id, UUID) else UUID(str(enrollment_id))
    result = await db.execute(
        update(OutreachEnrollment)
        .where(
            OutreachEnrollment.id == eid,
            # IS DISTINCT FROM, spelled out: a NULL claim is unclaimed, and a
            # claim on a *different* step belongs to a step already finished.
            (OutreachEnrollment.send_claim_step.is_(None))
            | (OutreachEnrollment.send_claim_step != step_number),
        )
        .values(send_claim_step=step_number, send_claimed_at=datetime.now(timezone.utc))
        .execution_options(skip_org_filter=True, synchronize_session=False)
    )
    if result.rowcount:
        await db.commit()
        return ClaimResult(acquired=True)

    # Rowcount 0 is two different things: this step is already claimed, or the
    # enrollment does not exist. Read back to tell them apart — a missing
    # enrollment must not be reported as "held by somebody", because the caller
    # would then wait for a worker that is not there.
    await db.rollback()
    row = (
        await db.execute(
            select(OutreachEnrollment.send_claim_step, OutreachEnrollment.send_claimed_at)
            .where(OutreachEnrollment.id == eid)
            .execution_options(skip_org_filter=True)
        )
    ).one_or_none()
    if row is None or row[0] != step_number:
        # No such enrollment, or it moved on between the UPDATE and this read.
        # Neither is a claim we are behind; let the caller proceed and hit its
        # own not-found / status checks.
        return ClaimResult(acquired=True)
    return ClaimResult(acquired=False, held_since=row[1])


async def release(db: AsyncSession, enrollment_id: Any, step_number: int) -> bool:
    """Give back a claim for a step that never reached the transport.

    Only for outcomes where that is *known*: the dispatcher returned a soft skip
    (it declined before the wire), or it raised on the way in. An exception out
    of the transport itself is ambiguous and must keep its claim — releasing it
    would turn one uncertain send into two certain ones.

    Guarded on the step number so a release can never eat a claim that a later
    step has since taken.

    Best-effort. A failure here costs a step that will be reconciled later
    instead of retried now; raising would turn a benign soft skip into a task
    failure, which is the shape of bug this file's neighbour was written for.
    """
    eid = enrollment_id if isinstance(enrollment_id, UUID) else UUID(str(enrollment_id))
    try:
        result = await db.execute(
            update(OutreachEnrollment)
            .where(
                OutreachEnrollment.id == eid,
                OutreachEnrollment.send_claim_step == step_number,
            )
            .values(send_claim_step=None, send_claimed_at=None)
            .execution_options(skip_org_filter=True, synchronize_session=False)
        )
        await db.commit()
        return bool(result.rowcount)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Could not release send claim for enrollment %s step %s: %s",
            enrollment_id, step_number, exc,
        )
        await db.rollback()
        return False


async def reconcile(db: AsyncSession, enrollment_id: Any, step_number: int) -> bool:
    """Close out a step whose send reached the wire and lost its bookkeeping.

    Called when a claim is refused, stale, and the enrollment is *still* on the
    claimed step — the signature of an attempt that got the message out and then
    died before recording it. Without this the enrollment is re-dispatched every
    five minutes and refused every time: no duplicate reaches the recipient, but
    the cadence stops and the enrollment quietly costs a composition per sweep.

    Records the missing ``sent`` event and advances exactly as the send would
    have, so the next step's delay is measured from the moment the message
    actually went out — ``send_claimed_at`` — rather than from whenever a sweep
    happened to notice.

    Returns False if the enrollment moved on in the meantime, which means the
    original attempt did come back and there is nothing to fix.
    """
    from app.services import step_delay

    eid = enrollment_id if isinstance(enrollment_id, UUID) else UUID(str(enrollment_id))
    enrollment = (
        await db.execute(
            select(OutreachEnrollment)
            .where(OutreachEnrollment.id == eid)
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one_or_none()
    if enrollment is None or enrollment.current_step != step_number:
        return False
    if enrollment.status != "active":
        return False

    sent_at = enrollment.send_claimed_at or datetime.now(timezone.utc)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)

    db.add(
        OutreachEvent(
            enrollment_id=enrollment.id,
            step_number=step_number,
            event_type="sent",
            occurred_at=sent_at,
            event_metadata={
                "status": "sent",
                # The honest part. This send is in the denominator of every
                # deliverability rate because it did go out; it carries no
                # subject or body because the attempt that composed them never
                # got to write them down, and inventing either would put copy in
                # the audit trail that nobody can prove was sent.
                "reconciled": True,
                "wire_outcome": "unconfirmed",
                "reason": (
                    "claimed for the wire and never recorded — recovered by "
                    "app.services.send_claim.reconcile"
                ),
                "tracked": False,
            },
        )
    )

    enrollment.current_step = step_number + 1
    next_step = await _next_step(db, enrollment)
    if next_step is not None:
        enrollment.next_send_at = step_delay.due_at(
            next_step,
            sent_at,
            factor=enrollment.adaptive_delay_factor,
            enrollment_id=enrollment.id,
        )
    else:
        enrollment.status = "completed"
        enrollment.completed_at = datetime.now(timezone.utc)
        enrollment.next_send_at = None
    await db.commit()
    logger.warning(
        "Reconciled enrollment %s step %s: claimed at %s, never recorded",
        enrollment_id, step_number, sent_at.isoformat(),
    )
    return True


async def _next_step(db: AsyncSession, enrollment: OutreachEnrollment):
    from app.models.sequence import SequenceStep

    return (
        await db.execute(
            select(SequenceStep)
            .where(
                SequenceStep.sequence_id == enrollment.sequence_id,
                SequenceStep.step_number == enrollment.current_step,
            )
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one_or_none()
