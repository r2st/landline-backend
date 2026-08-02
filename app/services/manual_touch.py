"""Non-email sequence steps: hold the cadence until the human does the touch.

A sequence step can be a ``call``, a ``linkedin`` message, a ``video`` or
``direct_mail``. None of those are things this system does — it files a
``FollowUp`` for a person and the cadence continues. That much is the right
design: LinkedIn automation risks a real person's account, and nothing here can
place a phone call.

What was wrong is *when* the cadence continued. The step advanced the moment the
reminder was filed, which produced two failures at once:

**The spacing was measured from the wrong event.** A "call, then email two days
later" cadence sent the email two days after the reminder was *created*. If the
rep called on Thursday, the email that was meant to follow the call had already
gone out on Tuesday, referring to a conversation that had not happened.

**The funnel could not tell a made touch from an unmade one.** No event was
recorded at all, so a five-step cadence where nobody ever worked the call queue
reported the same shape as one where every call was made. The step was marked
done because a reminder was filed — the cadence was reporting on its own
paperwork.

So a manual step now *holds*. The enrollment stays on that step, and the touch
resolves one of three ways, each recorded as an ``OutreachEvent``:

* ``task_completed`` — somebody marked the task done. The cadence resumes,
  spaced from the completion, so the next email follows the call by the interval
  the sequence asked for.
* ``task_expired`` — nobody did it before the deadline. The cadence resumes
  anyway and the touch is recorded as **not made**.
* ``task_created`` — filed, still open. The enrollment waits.

Why a deadline at all
---------------------
Holding without one trades a wrong-but-visible failure for a silent one. A rep
who ignores a task would strand the enrollment forever: the contact goes quiet
mid-sequence, the enrollment sits ``active`` with a due date in the past, and
nothing reports it — indistinguishable from a healthy sequence between touches.
A skipped touch that says so in the funnel is the better failure, so the hold
expires (``settings.manual_step_timeout_hours``, overridable per step via
``SequenceStep.task_timeout_hours``).

Why the events are not ``sent``
-------------------------------
``sent`` is the denominator of every reply rate in the product and half of the
bounce rail's send count (``services.danger_monitor``). A filed reminder is not a
delivered message; counting one as a send would dilute both with touches that
may never have been made. The ``task_*`` types are deliberately outside that
vocabulary, and the analytics surface them as their own column.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import SequenceStep

logger = logging.getLogger(__name__)

# Floor and ceiling on the hold, whatever a step configures.
#
# The floor stops a misconfigured 0 from meaning "expire before the queue sweep
# that filed the task has even returned", which would make the hold look like it
# does nothing. The ceiling stops a step from parking an enrollment for a month —
# past a couple of weeks the copy is stale and resuming the cadence is worse than
# ending it.
MIN_TIMEOUT_HOURS = 1
MAX_TIMEOUT_HOURS = 336  # 14 days

# Event types this module writes. Exported so tests and the analytics layer name
# them from one place rather than three string literals.
EVENT_CREATED = "task_created"
EVENT_COMPLETED = "task_completed"
EVENT_EXPIRED = "task_expired"
MANUAL_EVENT_TYPES = (EVENT_CREATED, EVENT_COMPLETED, EVENT_EXPIRED)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes (SQLite, and any tz-naive column) read as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def timeout_for(step: Any, settings: Any = None) -> timedelta:
    """How long to hold the cadence for this step's manual touch.

    ``SequenceStep.task_timeout_hours`` when set, else
    ``settings.manual_step_timeout_hours``, clamped to
    [:data:`MIN_TIMEOUT_HOURS`, :data:`MAX_TIMEOUT_HOURS`].

    ``getattr`` throughout because this is called with ORM rows, with detached
    objects, and (in tests) with stubs — and a step from before this column
    existed must resolve to the default rather than raise on a send path.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    hours = getattr(step, "task_timeout_hours", None)
    if hours is None:
        hours = getattr(settings, "manual_step_timeout_hours", 72)
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        logger.warning(
            "Unparseable task timeout %r on step %s — using 72h",
            hours, getattr(step, "id", "?"),
        )
        hours = 72
    return timedelta(hours=max(MIN_TIMEOUT_HOURS, min(MAX_TIMEOUT_HOURS, hours)))


def blocks_cadence(settings: Any = None) -> bool:
    """Whether a manual step holds the cadence at all.

    The escape hatch back to the old behaviour (file and advance). Kept because
    this changes the timing of live sequences: an org that wants its cadence to
    run on a clock regardless of whether the calls happen can say so, and get the
    ``task_*`` events either way — the recording is not the part worth opting out
    of.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    return bool(getattr(settings, "manual_step_blocks_cadence", True))


async def _record(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    step_number: int,
    event_type: str,
    metadata: dict,
) -> OutreachEvent:
    event = OutreachEvent(
        enrollment_id=enrollment.id,
        step_number=step_number,
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        event_metadata=metadata,
    )
    db.add(event)
    return event


async def open_task_for(
    db: AsyncSession, enrollment: OutreachEnrollment, step_number: int
) -> Optional[FollowUp]:
    """The task filed for this enrollment's step, whatever state it is in.

    Newest first: re-filing is possible if a step is re-entered, and the current
    hold is governed by the most recent task, not the first one ever filed.
    """
    return (
        await db.execute(
            select(FollowUp)
            .where(
                FollowUp.enrollment_id == enrollment.id,
                FollowUp.step_number == step_number,
            )
            .order_by(FollowUp.created_at.desc())
        )
    ).scalars().first()


async def create_task(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    step: SequenceStep,
    contact_id: Optional[uuid.UUID],
    company_id: Optional[uuid.UUID],
    settings: Any = None,
) -> tuple[FollowUp, datetime]:
    """File the human task for a manual step and hold the enrollment on it.

    Returns ``(follow_up, deadline)``. The caller commits.

    ``next_send_at`` is set to the deadline rather than to a poll interval: a
    completed task advances the enrollment immediately through
    :func:`complete_task`, so the only thing the queue needs to wake up for is
    the expiry. Polling hourly for a task that will not have changed costs a
    dispatch, a session and an enrollment load per hour per held enrollment, and
    buys nothing.
    """
    now = datetime.now(timezone.utc)
    deadline = now + timeout_for(step, settings)
    channel = step.channel or "call"

    follow_up = FollowUp(
        contact_id=contact_id,
        company_id=company_id,
        enrollment_id=enrollment.id,
        step_number=step.step_number,
        channel=channel,
        # Due now, not at the deadline: the deadline is when we give up on the
        # touch, and a task that only becomes "due" at the moment it stops
        # mattering would never appear in an overdue list in time to be worked.
        due_date=now,
        reason=f"Sequence step {step.step_number}: reach out via {channel}"[:500],
        status="pending",
    )
    db.add(follow_up)
    await db.flush()

    await _record(
        db, enrollment, step.step_number, EVENT_CREATED,
        {
            "channel": channel,
            "follow_up_id": str(follow_up.id),
            "expires_at": deadline.isoformat(),
            "blocking": blocks_cadence(settings),
        },
    )

    if blocks_cadence(settings):
        # Hold here. ``current_step`` does not move, so a re-dispatch lands on
        # this same step and resolves the task rather than sending anything.
        enrollment.next_send_at = deadline
    else:
        from app.tasks.outreach import _advance_step

        await _advance_step(db, enrollment)

    logger.info(
        "Manual step %d (%s) for enrollment %s → task %s, %s",
        step.step_number, channel, enrollment.id, follow_up.id,
        f"holding until {deadline.isoformat()}" if blocks_cadence(settings)
        else "cadence advanced (hold disabled)",
    )
    return follow_up, deadline


async def resolve_task(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    step: SequenceStep,
    settings: Any = None,
) -> dict:
    """Decide what a re-dispatch onto a held manual step should do.

    Returns a dict with ``status`` one of:

    * ``"waiting"`` — the task is open and the deadline has not passed. The
      caller does nothing; ``next_send_at`` still carries the deadline.
    * ``"completed"`` — somebody did it. The touch is recorded and the cadence
      advances, spaced from the completion.
    * ``"expired"`` — nobody did it in time. The touch is recorded as not made
      and the cadence advances so the enrollment is not stranded.
    * ``"missing"`` — no task is on file for this step (deleted, or the step was
      re-entered). Treated as expired: the enrollment must never be left holding
      on a task that does not exist.

    The caller commits.
    """
    now = datetime.now(timezone.utc)
    follow_up = await open_task_for(db, enrollment, step.step_number)

    if follow_up is None:
        logger.warning(
            "Enrollment %s is held on step %d with no task on file — advancing",
            enrollment.id, step.step_number,
        )
        await _finish(db, enrollment, step, EVENT_EXPIRED, now, {"reason": "task_missing"})
        return {"status": "missing", "step": step.step_number}

    if follow_up.status == "completed":
        # Space the next touch from when the call actually happened. ``or now``:
        # a task completed through a path that did not stamp ``completed_at``
        # still resolves, just anchored to this moment rather than to a time we
        # do not have.
        done_at = _utc(follow_up.completed_at) or now
        await _finish(
            db, enrollment, step, EVENT_COMPLETED, done_at,
            {
                "channel": follow_up.channel,
                "follow_up_id": str(follow_up.id),
                "completed_at": done_at.isoformat(),
            },
        )
        return {"status": "completed", "step": step.step_number, "completed_at": done_at}

    if follow_up.status == "dismissed":
        # An explicit human "not doing this". Resume immediately rather than
        # holding out the deadline — the answer is already in.
        await _finish(
            db, enrollment, step, EVENT_EXPIRED, now,
            {
                "channel": follow_up.channel,
                "follow_up_id": str(follow_up.id),
                "reason": "dismissed",
            },
        )
        return {"status": "expired", "step": step.step_number, "reason": "dismissed"}

    deadline = _deadline_for(follow_up, step, settings)
    if now < deadline:
        # Re-arm rather than trust the stored value: a task rescheduled or a step
        # whose timeout was edited under an open hold should be governed by the
        # current answer, and leaving a stale past-due ``next_send_at`` would
        # spin the queue on this enrollment every sweep.
        enrollment.next_send_at = deadline
        return {
            "status": "waiting",
            "step": step.step_number,
            "expires_at": deadline,
            "follow_up_id": follow_up.id,
        }

    # Expired. Mark the task so it stops showing in a work queue as something
    # still worth doing — the cadence has already moved past it.
    follow_up.status = "dismissed"
    follow_up.completed_at = now
    follow_up.notes = (
        (follow_up.notes + "\n" if follow_up.notes else "")
        + "Auto-dismissed: the sequence moved on before this touch was made."
    )
    await _finish(
        db, enrollment, step, EVENT_EXPIRED, now,
        {
            "channel": follow_up.channel,
            "follow_up_id": str(follow_up.id),
            "reason": "timed_out",
            "expired_at": deadline.isoformat(),
        },
    )
    logger.info(
        "Manual step %d for enrollment %s expired unworked — cadence resumed, "
        "touch recorded as not made",
        step.step_number, enrollment.id,
    )
    return {"status": "expired", "step": step.step_number, "reason": "timed_out"}


def _deadline_for(
    follow_up: FollowUp, step: SequenceStep, settings: Any = None
) -> datetime:
    """When this hold gives up, from the task's creation plus the step's timeout.

    Derived rather than stored so editing ``task_timeout_hours`` takes effect on
    holds already open, and so a task and its deadline cannot drift apart.
    ``created_at`` is server-defaulted, so it can be None on an object that was
    added but never flushed — that resolves to "expire now", which is the safe
    direction: it advances the cadence rather than holding on an unknown.
    """
    created = _utc(follow_up.created_at) or datetime.now(timezone.utc)
    return created + timeout_for(step, settings)


async def _finish(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    step: SequenceStep,
    event_type: str,
    anchor: datetime,
    metadata: dict,
) -> None:
    """Record the touch's outcome and advance the cadence from ``anchor``.

    ``anchor`` is the completion time for a made touch and "now" for one that was
    not made, which is the whole point of holding: the next email is spaced from
    the event it is supposed to follow.
    """
    await _record(db, enrollment, step.step_number, event_type, metadata)
    await _advance_from(db, enrollment, anchor)


async def _advance_from(
    db: AsyncSession, enrollment: OutreachEnrollment, anchor: datetime
) -> None:
    """Advance to the next step, scheduling it relative to ``anchor``.

    Mirrors ``tasks.outreach._advance_step`` but takes the anchor explicitly
    instead of assuming "now". Imported lazily: ``tasks.outreach`` imports this
    module, so a top-level import would be a cycle.
    """
    from app.services import step_delay

    enrollment.current_step += 1
    next_step = (
        await db.execute(
            select(SequenceStep).where(
                SequenceStep.sequence_id == enrollment.sequence_id,
                SequenceStep.step_number == enrollment.current_step,
            )
        )
    ).scalar_one_or_none()

    if next_step is None:
        enrollment.status = "completed"
        enrollment.completed_at = datetime.now(timezone.utc)
        enrollment.next_send_at = None
        return

    enrollment.next_send_at = step_delay.due_at(
        next_step,
        anchor,
        factor=enrollment.adaptive_delay_factor,
        enrollment_id=enrollment.id,
    )


async def complete_task(db: AsyncSession, follow_up: FollowUp) -> Optional[dict]:
    """Advance the cadence when a sequence-linked task reaches a terminal state.

    Called from the follow-ups API the moment a human marks the task done, so the
    next touch is scheduled from the completion rather than waiting out the
    deadline to discover it. Returns None when the task is not sequence-linked
    (the common case — most follow-ups are hand-made) or when the enrollment has
    moved on without it.

    Never raises into the caller's request: a reminder being marked done must
    succeed even if the cadence cannot be advanced. The caller commits.
    """
    if follow_up.enrollment_id is None or follow_up.step_number is None:
        return None
    if follow_up.status not in ("completed", "dismissed"):
        return None

    try:
        enrollment = (
            await db.execute(
                select(OutreachEnrollment).where(
                    OutreachEnrollment.id == follow_up.enrollment_id
                )
            )
        ).scalar_one_or_none()
        if enrollment is None:
            return None
        # Only the step the enrollment is actually holding on may advance it.
        # A task completed late — after the hold expired and the cadence moved
        # on — must not push the enrollment forward a second time, which would
        # skip whatever step it had legitimately reached.
        if enrollment.status != "active" or enrollment.current_step != follow_up.step_number:
            logger.info(
                "Task %s completed but enrollment %s is on step %s (%s) — "
                "not advancing",
                follow_up.id, enrollment.id, enrollment.current_step, enrollment.status,
            )
            return None

        step = (
            await db.execute(
                select(SequenceStep).where(
                    SequenceStep.sequence_id == enrollment.sequence_id,
                    SequenceStep.step_number == follow_up.step_number,
                )
            )
        ).scalar_one_or_none()
        if step is None:
            return None

        now = datetime.now(timezone.utc)
        if follow_up.status == "completed":
            done_at = _utc(follow_up.completed_at) or now
            await _finish(
                db, enrollment, step, EVENT_COMPLETED, done_at,
                {
                    "channel": follow_up.channel,
                    "follow_up_id": str(follow_up.id),
                    "completed_at": done_at.isoformat(),
                    "via": "follow_up_api",
                },
            )
            outcome = "completed"
        else:
            await _finish(
                db, enrollment, step, EVENT_EXPIRED, now,
                {
                    "channel": follow_up.channel,
                    "follow_up_id": str(follow_up.id),
                    "reason": "dismissed",
                    "via": "follow_up_api",
                },
            )
            outcome = "dismissed"

        logger.info(
            "Manual touch %s for enrollment %s — cadence advanced to step %d",
            outcome, enrollment.id, enrollment.current_step,
        )
        return {
            "status": outcome,
            "enrollment_id": enrollment.id,
            "next_step": enrollment.current_step,
            "next_send_at": enrollment.next_send_at,
        }
    except Exception as exc:  # noqa: BLE001 — advancing must never fail the update
        logger.exception(
            "Could not advance enrollment %s from task %s: %s",
            follow_up.enrollment_id, follow_up.id, exc,
        )
        return None
