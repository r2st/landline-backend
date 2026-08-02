"""When a follow-up is due.

One function — :func:`due_at` — answers "given the last touch, when does this
step go out?", and every scheduler in the system goes through it.

That centralisation is the point. The due date used to be computed inline as
``anchor + timedelta(days=step.delay_days)`` in five places: the post-send
advance, the optimizer's advance, the send path's double-dispatch guard, the
queue sweep's legacy path, and the sequences router's preview. Three of them
applied the adaptive-delay factor and two did not, so an enrollment could be
"due" according to the sweep and "not yet due" according to the send task it
dispatched — a step that oscillates instead of sending. Adding sub-day delays to
five copies of the arithmetic would have made that worse, not better.

The cadence a step can express:

* ``delay_days`` + ``delay_hours`` — the wait, to the hour.
* ``delay_business_days`` — count the day portion in business days.
* ``delay_jitter_minutes`` — spread a batch that was enrolled together.

and on top of it ``OutreachEnrollment.adaptive_delay_factor``, which shortens
the wait for an engaged contact and extends it for a silent one
(``services.adaptive_delay``).

**This decides the schedule, not the send.** Business hours, weekends, per-contact
timezone, warm-up ceilings, the daily cap and per-mailbox pacing are all enforced
at dispatch by ``send_limiter`` and ``send_pacing``. A step due at 3 AM on a
Sunday is not sent at 3 AM on a Sunday; it waits for the window. What this module
owns is the *spacing between touches*, which nothing downstream can reconstruct.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Floor for a step that asked for a sub-day wait. The whole-day floor in
# ``adaptive_delay`` exists to stop the factor collapsing a cadence into a burst;
# a step that deliberately asked for four hours needs the same protection at a
# proportionate scale, not to be rounded up to a day and silently ignored.
MIN_SUBDAY_DELAY = timedelta(minutes=15)

# Ceiling on jitter. Past a few hours it stops being a spread and starts being
# an unpredictable cadence, and the delay itself is the field for that.
MAX_JITTER_MINUTES = 720


def _utc(value: datetime) -> datetime:
    """Naive datetimes (SQLite, and any tz-naive column) read as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def base_delay(step: Any) -> timedelta:
    """The step's configured wait, before the adaptive factor and jitter.

    ``getattr`` with defaults throughout: this is called with ORM rows, with
    detached objects loaded before migration 104, and with lightweight stubs in
    tests. A step that predates the sub-day columns is a day-granular step.
    """
    days = max(0, int(getattr(step, "delay_days", 0) or 0))
    hours = max(0, int(getattr(step, "delay_hours", 0) or 0))
    return timedelta(days=days, hours=hours)


def effective_delay(step: Any, factor: Optional[float] = None) -> timedelta:
    """The step's wait with the enrollment's adaptive factor applied, clamped.

    Day-granular steps (``delay_hours == 0``) delegate to
    ``adaptive_delay.effective_delay_days`` untouched, so every sequence that
    exists today keeps its exact current schedule — the whole-day rounding and
    the ``[1, 14]``-day clamp included. Only a step that opts into hours takes
    the sub-day path, where rounding to whole days would erase the thing it
    asked for.
    """
    from app.services.adaptive_delay import MAX_DELAY_DAYS, effective_delay_days

    base = base_delay(step)
    if base <= timedelta(0):
        # An immediate step. The factor reshapes real waits; it does not invent
        # one, or a "send now" step would drift by whatever the contact did last.
        return timedelta(0)

    if not int(getattr(step, "delay_hours", 0) or 0):
        return timedelta(days=effective_delay_days(int(step.delay_days or 0), factor or 1.0))

    scaled = base * (factor if factor and factor > 0 else 1.0)
    return max(MIN_SUBDAY_DELAY, min(timedelta(days=MAX_DELAY_DAYS), scaled))


def _schedule_tz():
    """Timezone the business-day count is reckoned in.

    The send window's timezone, because that is the calendar the recipient's
    weekend actually falls on and the one dispatch will judge the send against.
    An unresolvable name degrades to UTC: a weekend boundary a few hours off is
    a far smaller error than a scheduler that raises.
    """
    from zoneinfo import ZoneInfo

    name = get_settings().send_window_timezone
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — bad tz name must not break scheduling
        logger.warning("Unknown send_window_timezone %r — scheduling in UTC", name)
        return timezone.utc


def add_business_days(anchor: datetime, days: int) -> datetime:
    """``anchor`` advanced by ``days`` business days, keeping the local wall time.

    Walks dates rather than adding a ``timedelta``, and rebuilds the result at
    the same local clock time, so a delay that spans a DST boundary still lands
    at the hour it was scheduled for instead of sliding by one.
    """
    if days <= 0:
        return anchor

    tz = _schedule_tz()
    local = _utc(anchor).astimezone(tz)
    day = local.date()
    remaining = days
    while remaining > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:  # Mon–Fri
            remaining -= 1

    rebuilt = datetime.combine(day, local.time()).replace(tzinfo=tz)
    return rebuilt.astimezone(timezone.utc)


def jitter_for(step: Any, enrollment_id: Any) -> timedelta:
    """A stable per-enrollment offset within the step's jitter window.

    Derived from ``(enrollment_id, step_number)``, never drawn fresh. A random
    draw would be a correctness bug, not a style choice: the queue sweep
    recomputes the due date on every pass for enrollments with no
    ``next_send_at``, so fresh randomness would move the deadline each time it
    was checked — a step could be due at 10:00, not due at 10:05, and due again
    at 10:10. Hashing the identity gives the same spread across the batch and
    the same answer every time for any one enrollment.
    """
    cap = int(getattr(step, "delay_jitter_minutes", 0) or 0)
    if cap <= 0 or enrollment_id is None:
        return timedelta(0)
    cap = min(cap, MAX_JITTER_MINUTES)

    seed = f"{enrollment_id}:{getattr(step, 'step_number', 0)}".encode()
    digest = int(hashlib.sha256(seed).hexdigest()[:12], 16)
    return timedelta(seconds=digest % (cap * 60))


def due_at(
    step: Any,
    anchor: datetime,
    *,
    factor: Optional[float] = None,
    enrollment_id: Any = None,
) -> datetime:
    """When ``step`` is due, measured from ``anchor`` (the previous touch).

    ``anchor`` is the last ``sent`` event for the enrollment, or the enrollment
    time for the first step. Returns an aware UTC datetime.

    Business-day counting applies to whole days only; any hours component is
    added afterwards as real elapsed time. A step configured as "2 business days
    + 4 hours" from Friday 16:00 is due Tuesday 20:00 — the days skip the
    weekend, the hours do not, because four hours means four hours.

    A delay the adaptive factor has shrunk below a day takes the plain path even
    when the step asked for business days: there is no whole day left to hop, and
    an anchor is always a *send*, which the send window already keeps off the
    weekend. So a sub-day wait cannot land on a Saturday to begin with.
    """
    anchor = _utc(anchor)
    delay = effective_delay(step, factor)

    if getattr(step, "delay_business_days", False) and delay >= timedelta(days=1):
        whole_days = delay.days
        due = add_business_days(anchor, whole_days) + (delay - timedelta(days=whole_days))
    else:
        due = anchor + delay

    return due + jitter_for(step, enrollment_id)


def is_due(
    step: Any,
    anchor: datetime,
    *,
    now: Optional[datetime] = None,
    factor: Optional[float] = None,
    enrollment_id: Any = None,
) -> tuple[bool, datetime]:
    """``(due_now, due_at)`` for ``step``. The read-side companion to :func:`due_at`."""
    now = _utc(now or datetime.now(timezone.utc))
    due = due_at(step, anchor, factor=factor, enrollment_id=enrollment_id)
    return now >= due, due


def describe(step: Any) -> str:
    """The step's cadence in words, for logs and API responses."""
    days = int(getattr(step, "delay_days", 0) or 0)
    hours = int(getattr(step, "delay_hours", 0) or 0)
    if not days and not hours:
        return "immediately"

    parts = []
    if days:
        unit = "business day" if getattr(step, "delay_business_days", False) else "day"
        parts.append(f"{days} {unit}{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    out = " and ".join(parts)

    jitter = int(getattr(step, "delay_jitter_minutes", 0) or 0)
    if jitter > 0:
        out += f" (±{min(jitter, MAX_JITTER_MINUTES)}m spread)"
    return out
