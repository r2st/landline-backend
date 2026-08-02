"""What happens to an enrollment whose send keeps throwing.

Until now: nothing. ``send_sequence_step`` retried three times on an exception
and then let the task die, and *the enrollment was left exactly as it was* —
``status='active'``, ``next_send_at`` still in the past. Five minutes later the
queue sweep found it due and dispatched it again.

For a transient fault that is fine; the next attempt succeeds. For a permanent
one — a template that will not render, a contact row that trips a bug, a
dispatcher rejecting this particular message forever — it is an infinite loop.
Four attempts every five minutes, a bit over a thousand a day, each one doing
real work (research loads, an LLM composition) before it reaches the thing that
fails. Nothing reports it. The enrollment looks healthy from every angle: it is
``active``, it has a due date, and the funnel just shows a contact who stopped
receiving mail.

So a failure is now *recorded on the enrollment*, and repeated failures
eventually stop it:

* ``consecutive_failures`` counts **incidents**, not attempts — Celery's own
  retries of one send are one failure, because "5 consecutive failures" should
  mean five times we tried and gave up, not one bad afternoon.
* Each failure pushes ``next_send_at`` out by an exponential backoff, which is
  also what stops the sweep from dispatching a *duplicate* attempt on top of a
  Celery retry chain that is still running.
* At :data:`quarantine threshold <app.config.Settings.send_failure_quarantine_threshold>`
  the enrollment is paused with ``pause_reason='send_failures'``. It stops
  costing anything and starts being visible.
* A successful send clears the counter. *Consecutive* is the point: an
  enrollment that fails, recovers, and fails again months later is not the same
  animal as one that has never once gone through.

Why pause rather than keep retrying forever
-------------------------------------------
Backoff alone caps the cost but never surfaces the problem, and an enrollment
retrying every six hours for a year is not "still trying", it is abandoned with
extra steps. A pause is a state a human can find, which is the only thing that
gets a broken sequence fixed.

Why ``pause_reason`` matters here
---------------------------------
It is what keeps the pause from being silently undone. The OOO sweep resumes
only ``pause_reason='out_of_office'``, and the kill switch resumes only what it
paused (``kill_switch_paused``). Marking these with their own reason puts them
outside both — nothing auto-resumes a quarantine, because the fault that caused
it does not fix itself on a timer. Resuming is
:func:`clear_quarantine`, called from the API by a person who has dealt with it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment

logger = logging.getLogger(__name__)

# The marker this module owns on ``OutreachEnrollment.pause_reason``.
QUARANTINE_REASON = "send_failures"

# Error text is stored for a human to read, not parsed. Truncated because a
# driver traceback repr can run to kilobytes and the useful part is the front.
MAX_ERROR_CHARS = 1000

# Backoff is clamped at both ends. The floor must exceed the queue sweep's
# interval or a failing enrollment is re-dispatched before the backoff has any
# effect; the ceiling stops the last retry before a quarantine from landing a
# week out, which would make the quarantine itself unreachable in practice.
MIN_BACKOFF_MINUTES = 10
MAX_BACKOFF_MINUTES = 12 * 60


def backoff_for(failures: int, settings: Any = None) -> timedelta:
    """How long to wait after ``failures`` consecutive incidents.

    Exponential off the configured base, clamped to
    [:data:`MIN_BACKOFF_MINUTES`, :data:`MAX_BACKOFF_MINUTES`].

    The first failure already backs off further than Celery's own retry chain
    (60s + 120s + 180s), so the sweep cannot dispatch a second, concurrent
    attempt at an enrollment that is still being retried — which is how one
    fault became two live send paths racing to write the same event.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    base = getattr(settings, "send_failure_backoff_base_minutes", 15)
    try:
        base = int(base)
    except (TypeError, ValueError):
        base = 15

    # Guard the exponent as well as the result: 2**failures on a counter that
    # somehow ran away is an OverflowError, not a long wait.
    exponent = max(0, min(int(failures) - 1, 16))
    minutes = base * (2 ** exponent)
    return timedelta(
        minutes=max(MIN_BACKOFF_MINUTES, min(MAX_BACKOFF_MINUTES, minutes))
    )


def quarantine_threshold(settings: Any = None) -> int:
    """How many consecutive incidents before the enrollment is paused.

    ``0`` or less disables quarantine — the backoff still applies, so the cost
    is bounded either way, but the enrollment keeps trying. Kept as an escape
    hatch for an operator who would rather have a stuck sequence than a paused
    one, though the default is not that.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()
    try:
        return int(getattr(settings, "send_failure_quarantine_threshold", 5))
    except (TypeError, ValueError):
        return 5


def clear_failures(enrollment: OutreachEnrollment) -> None:
    """Reset the failure state after a send that actually went out.

    Mutates in place for the caller to commit — this runs inside the send path's
    own transaction, right before the commit that records the send, so the
    reset and the send land together or not at all.
    """
    enrollment.consecutive_failures = 0
    enrollment.last_error = None
    enrollment.last_error_at = None


async def record_failure(
    db: AsyncSession,
    enrollment_id: str | UUID,
    error: BaseException | str,
    settings: Any = None,
) -> Optional[dict]:
    """Record one failed send incident and back the enrollment off.

    Returns a summary dict, or None when the enrollment could not be loaded
    (deleted mid-flight) or the update itself failed.

    Issued against a *fresh* read and committed here, because the caller reaches
    this after a ``rollback()`` that expired every object in its session: the
    loaded enrollment cannot be trusted to be readable, and the whole point is
    that this record survives the transaction that failed.

    Never raises. A failure to record a failure must not become a second
    failure — the caller is already on its error path, and the worst case of
    swallowing this is the behaviour that existed before this module.
    """
    if isinstance(enrollment_id, str):
        try:
            enrollment_id = UUID(enrollment_id)
        except ValueError:
            logger.warning("Cannot record failure for malformed id %r", enrollment_id)
            return None

    text = str(error) or error.__class__.__name__
    text = text[:MAX_ERROR_CHARS]
    now = datetime.now(timezone.utc)

    try:
        enrollment = (
            await db.execute(
                select(OutreachEnrollment)
                .where(OutreachEnrollment.id == enrollment_id)
                .execution_options(skip_org_filter=True)
            )
        ).scalar_one_or_none()
        if enrollment is None:
            logger.info(
                "Enrollment %s failed but is no longer present — nothing to record",
                enrollment_id,
            )
            return None

        failures = (enrollment.consecutive_failures or 0) + 1
        threshold = quarantine_threshold(settings)

        values: dict[str, Any] = {
            "consecutive_failures": failures,
            "last_error": text,
            "last_error_at": now,
        }

        if enrollment.status != "active":
            # Record what happened, change nothing else. A send can fail *after*
            # the contact replied or unsubscribed — the task was already in
            # flight when the state changed — and overwriting 'replied' with
            # 'paused' would erase the one outcome the system exists to produce
            # and hand the contact back to a sweep. Rescheduling is pointless
            # here too: the queue only dispatches 'active'.
            logger.info(
                "Enrollment %s failed but is %s — recording without rescheduling",
                enrollment_id, enrollment.status,
            )
            await db.execute(
                update(OutreachEnrollment)
                .where(OutreachEnrollment.id == enrollment_id)
                .values(**values)
                .execution_options(skip_org_filter=True)
            )
            await db.commit()
            return {
                "enrollment_id": enrollment_id,
                "consecutive_failures": failures,
                "quarantined": False,
                "next_send_at": enrollment.next_send_at,
                "last_error": text,
            }

        # Past here the enrollment is active, so a threshold breach is a real
        # quarantine rather than a terminal state being overwritten.
        quarantined = 0 < threshold <= failures

        if quarantined:
            # Stop. ``next_send_at`` is left where the backoff put it rather than
            # nulled, so the record of when it would next have tried survives for
            # whoever picks this up — and the queue only dispatches ``active``,
            # so a due date on a paused row sends nothing.
            values["status"] = "paused"
            values["pause_reason"] = QUARANTINE_REASON
            values["next_send_at"] = now + backoff_for(failures, settings)
            logger.error(
                "Enrollment %s quarantined after %d consecutive send failures — "
                "last error: %s",
                enrollment_id, failures, text,
            )
        else:
            values["next_send_at"] = now + backoff_for(failures, settings)
            logger.warning(
                "Enrollment %s send failed (%d/%d) — retrying after %s: %s",
                enrollment_id, failures, threshold,
                values["next_send_at"].isoformat(), text,
            )

        await db.execute(
            update(OutreachEnrollment)
            .where(OutreachEnrollment.id == enrollment_id)
            .values(**values)
            .execution_options(skip_org_filter=True)
        )
        await db.commit()

        return {
            "enrollment_id": enrollment_id,
            "consecutive_failures": failures,
            "quarantined": quarantined,
            "next_send_at": values["next_send_at"],
            "last_error": text,
        }
    except Exception as exc:  # noqa: BLE001 — see docstring
        logger.exception(
            "Could not record send failure for enrollment %s: %s", enrollment_id, exc
        )
        try:
            await db.rollback()
        except Exception:  # pragma: no cover - defensive
            pass
        return None


def clear_quarantine(enrollment: OutreachEnrollment, resume: bool = True) -> dict:
    """Release a quarantined enrollment, for a human who has fixed the cause.

    ``resume=True`` puts it back to ``active`` and due now; ``False`` clears the
    failure counter but leaves it paused, for "I have seen this, it is not
    coming back". Either way the counter resets — keeping a stale count would
    quarantine it again on the next single blip.

    Refuses to touch an enrollment paused for any *other* reason. A pause for a
    dead domain or an operator's decision is not this module's to undo, and the
    reason field is the only thing that distinguishes them.
    """
    if enrollment.pause_reason != QUARANTINE_REASON:
        return {
            "released": False,
            "reason": "not_quarantined",
            "pause_reason": enrollment.pause_reason,
        }

    enrollment.consecutive_failures = 0
    enrollment.last_error = None
    enrollment.last_error_at = None
    enrollment.pause_reason = None

    if resume:
        enrollment.status = "active"
        enrollment.next_send_at = datetime.now(timezone.utc)

    logger.info(
        "Quarantine cleared on enrollment %s (%s)",
        enrollment.id, "resumed" if resume else "left paused",
    )
    return {
        "released": True,
        "resumed": resume,
        "status": enrollment.status,
        "next_send_at": enrollment.next_send_at,
    }
