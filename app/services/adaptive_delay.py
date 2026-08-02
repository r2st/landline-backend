"""Engagement-aware step delays (Feature 3).

Replaces a sequence's static ``delay_days`` with a per-enrollment cadence that
reacts to the contact:

* **Engagement** (an open / click / reply) → *shorten* the next delay: strike
  while interest is warm. The factor halves, floored.
* **Silence** after a step's window has elapsed → *extend* the next delay: back
  off rather than pile on. The factor grows ×1.5, capped.

The factor lives on ``OutreachEnrollment.adaptive_delay_factor``. It is *applied*
by ``services.step_delay``, which owns every due-date calculation in the system;
this module owns only the factor itself and the rules that move it.

:func:`effective_delay_days` is the day-granular arm of that calculation, and
clamps to ``[MIN_DELAY_DAYS, MAX_DELAY_DAYS]`` so the factor can neither stall a
sequence nor collapse it into a burst. A step that opts into sub-day delays takes
``step_delay``'s proportional clamp instead — rounding it to whole days here
would erase the thing it asked for.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import SequenceStep

logger = logging.getLogger(__name__)

# How hard each signal moves the factor.
SHORTEN_MULTIPLIER = 0.5   # engagement halves the wait
EXTEND_MULTIPLIER = 1.5    # silence grows it by 50%

# Bounds on the factor itself, so repeated signals can't run away.
MIN_FACTOR = 0.25
MAX_FACTOR = 3.0

# Bounds on the *effective* delay in days. A shortened delay never drops below a
# day (min 1 day); an extended one never exceeds two weeks (max 14 days).
MIN_DELAY_DAYS = 1
MAX_DELAY_DAYS = 14


def effective_delay_days(base_delay_days: int, factor: float) -> int:
    """Apply the adaptive ``factor`` to a step's base delay, clamped.

    A zero/negative base delay (an immediate step) is left at 0 — the factor only
    reshapes real waits. Otherwise the result is clamped to
    ``[MIN_DELAY_DAYS, MAX_DELAY_DAYS]``.
    """
    if base_delay_days is None or base_delay_days <= 0:
        return 0
    scaled = round(base_delay_days * (factor if factor and factor > 0 else 1.0))
    return max(MIN_DELAY_DAYS, min(MAX_DELAY_DAYS, scaled))


def _clamp_factor(factor: float) -> float:
    return max(MIN_FACTOR, min(MAX_FACTOR, factor))


def shorten(enrollment: OutreachEnrollment) -> float:
    """Engagement: halve the delay factor (floored). Returns the new factor."""
    current = enrollment.adaptive_delay_factor or 1.0
    enrollment.adaptive_delay_factor = _clamp_factor(current * SHORTEN_MULTIPLIER)
    return enrollment.adaptive_delay_factor


def extend(enrollment: OutreachEnrollment) -> float:
    """Silence: grow the delay factor ×1.5 (capped). Returns the new factor."""
    current = enrollment.adaptive_delay_factor or 1.0
    enrollment.adaptive_delay_factor = _clamp_factor(current * EXTEND_MULTIPLIER)
    return enrollment.adaptive_delay_factor


async def _current_step(db: AsyncSession, enrollment: OutreachEnrollment) -> SequenceStep | None:
    """The enrollment's current (pending) step.

    The whole row, not just ``delay_days``: the cadence is now days *plus*
    hours, business-day counting and jitter, and ``services.step_delay`` needs
    all of it to place the send.
    """
    return (
        await db.execute(
            select(SequenceStep).where(
                and_(
                    SequenceStep.sequence_id == enrollment.sequence_id,
                    SequenceStep.step_number == enrollment.current_step,
                )
            )
        )
    ).scalar_one_or_none()


async def _last_sent_at(db: AsyncSession, enrollment: OutreachEnrollment) -> datetime:
    """When the most recent step was sent (anchor for the next delay).

    Falls back to the enrollment date when nothing has been sent yet.
    """
    occurred = (
        await db.execute(
            select(OutreachEvent.occurred_at)
            .where(
                and_(
                    OutreachEvent.enrollment_id == enrollment.id,
                    OutreachEvent.event_type == "sent",
                )
            )
            .order_by(OutreachEvent.occurred_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    anchor = occurred or enrollment.enrolled_at
    if anchor is not None and anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    return anchor or datetime.now(timezone.utc)


async def reschedule_pending_step(db: AsyncSession, enrollment: OutreachEnrollment) -> bool:
    """Recompute ``next_send_at`` for the pending step using the current factor.

    Called after an engagement signal so a freshly-shortened cadence takes effect
    immediately — the already-scheduled next step is pulled in (or pushed out)
    from the last send by the new effective delay. No-op for enrollments that
    aren't actively waiting on a step. Returns True when it moved the schedule.
    """
    if enrollment.status != "active":
        return False
    step = await _current_step(db, enrollment)
    if step is None:
        return False

    from app.services import step_delay

    if step_delay.base_delay(step) <= timedelta(0):
        # An immediate step has no wait to reshape, and pulling one "in" would
        # only mean scheduling it for a moment that has already passed.
        return False

    anchor = await _last_sent_at(db, enrollment)
    enrollment.next_send_at = step_delay.due_at(
        step,
        anchor,
        factor=enrollment.adaptive_delay_factor,
        enrollment_id=enrollment.id,
    )
    return True


async def register_engagement(db: AsyncSession, enrollment: OutreachEnrollment) -> None:
    """An open/click/reply landed: shorten the factor and pull in the next step."""
    before = enrollment.adaptive_delay_factor or 1.0
    shorten(enrollment)
    await reschedule_pending_step(db, enrollment)
    logger.info(
        "Adaptive delay for enrollment %s: engagement shortened factor %.3f → %.3f",
        enrollment.id, before, enrollment.adaptive_delay_factor,
    )


def register_silence(enrollment: OutreachEnrollment) -> None:
    """A step's window elapsed with no engagement: extend the factor.

    Called at step-start (the step is due, so its predecessor's window is over).
    Only mutates the factor — the next send is scheduled right after this step
    ships, so it will pick the extended delay up then.
    """
    before = enrollment.adaptive_delay_factor or 1.0
    extend(enrollment)
    logger.info(
        "Adaptive delay for enrollment %s: silence extended factor %.3f → %.3f",
        enrollment.id, before, enrollment.adaptive_delay_factor,
    )
