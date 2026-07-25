"""Reporting timeframes for stats endpoints.

The Dialtone Console used to show one number per metric — "today", implicitly, in
the reporting timezone — which left operators unable to tell a quiet Tuesday from
a broken sender. This module resolves a named timeframe (``today``,
``this_week``, ``last_month``, …) into the concrete UTC window the stats queries
filter on, **plus the equal-granularity window immediately before it** so a
response can carry a "vs previous period" delta.

Every window is half-open — ``[start, end)`` — so consecutive periods tile
without double-counting an event that lands exactly on midnight. ``all_time`` has
no bounds (and therefore no previous period to compare against).

Windows are anchored to ``settings.daily_summary_timezone``, the same reporting
day the daily summary and the console's warm-up counters already use, so "today"
means the same thing everywhere in the product. Weeks start Monday (ISO).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls, datetime, time, timedelta, timezone
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# The timeframe keys stats endpoints accept, in menu order.
TIMEFRAMES: tuple[str, ...] = (
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "all_time",
)

DEFAULT_TIMEFRAME = "today"

TIMEFRAME_LABELS: dict[str, str] = {
    "today": "Today",
    "yesterday": "Yesterday",
    "this_week": "This week",
    "last_week": "Last week",
    "this_month": "This month",
    "last_month": "Last month",
    "all_time": "All time",
}

# Each bounded timeframe is "one period of granularity G, N periods back". The
# previous period for a delta is the same granularity one further back, which is
# why the calendar-month timeframes compare against a real calendar month rather
# than a fixed 30 days.
_SPEC: dict[str, tuple[str, int]] = {
    "today": ("day", 0),
    "yesterday": ("day", 1),
    "this_week": ("week", 0),
    "last_week": ("week", 1),
    "this_month": ("month", 0),
    "last_month": ("month", 1),
}


def ensure_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to aware UTC.

    SQLite (tests) returns naive datetimes, Postgres returns aware ones; callers
    compare across sources so they must be comparable.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def in_range(
    ts: Optional[datetime], start: Optional[datetime], end: Optional[datetime]
) -> bool:
    """Half-open membership test: ``start <= ts < end``.

    A ``None`` bound is unbounded on that side; a ``None`` timestamp is never in
    range (an un-stamped row didn't happen).
    """
    ts = ensure_aware(ts)
    if ts is None:
        return False
    if start is not None and ts < start:
        return False
    if end is not None and ts >= end:
        return False
    return True


def reporting_timezone():
    """The tzinfo stats windows are anchored to, falling back to UTC."""
    tz_name = get_settings().daily_summary_timezone
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:  # pragma: no cover - bad/unavailable tz name
        logger.info("unknown reporting timezone %r; falling back to UTC", tz_name)
        return timezone.utc


@dataclass(frozen=True)
class Timeframe:
    """A resolved reporting window plus the period before it.

    ``start``/``end`` are aware UTC and half-open. ``*_date`` fields are the
    *inclusive* local calendar dates the window covers — what a date picker or a
    drill-down link wants. Everything but ``key``/``label``/``today`` is ``None``
    for ``all_time``.
    """

    key: str
    label: str
    start: Optional[datetime]
    end: Optional[datetime]
    previous_start: Optional[datetime]
    previous_end: Optional[datetime]
    # Local reporting date at resolution time ("what day is it for us").
    today: date_cls
    start_date: Optional[date_cls]
    end_date: Optional[date_cls]
    previous_start_date: Optional[date_cls] = None
    previous_end_date: Optional[date_cls] = None

    @property
    def has_previous(self) -> bool:
        return self.previous_start is not None

    @property
    def window(self) -> tuple[Optional[datetime], Optional[datetime]]:
        return self.start, self.end

    @property
    def previous_window(self) -> tuple[Optional[datetime], Optional[datetime]]:
        return self.previous_start, self.previous_end


def _period(granularity: str, offset: int, today: date_cls) -> tuple[date_cls, date_cls]:
    """Local half-open date bounds for one period, ``offset`` periods back."""
    if granularity == "day":
        start = today - timedelta(days=offset)
        return start, start + timedelta(days=1)
    if granularity == "week":
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=offset)
        return start, start + timedelta(days=7)
    # month
    year, month = today.year, today.month - offset
    while month <= 0:
        month += 12
        year -= 1
    start = date_cls(year, month, 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return start, date_cls(next_year, next_month, 1)


def normalize_timeframe(value: Optional[str]) -> str:
    """Coerce a query value to a known timeframe key.

    Raises ``ValueError`` on an unknown key so the router can turn it into a 400
    that names the valid options, rather than silently reporting the wrong period.
    """
    key = (value or DEFAULT_TIMEFRAME).strip().lower()
    if key not in TIMEFRAMES:
        raise ValueError(
            f"Unknown timeframe {value!r}. Valid timeframes: {list(TIMEFRAMES)}"
        )
    return key


def resolve_timeframe(value: Optional[str], *, now: Optional[datetime] = None) -> Timeframe:
    """Resolve a timeframe key into concrete UTC windows.

    ``now`` is injectable for tests; it defaults to the current instant and is
    interpreted in the reporting timezone.
    """
    key = normalize_timeframe(value)
    label = TIMEFRAME_LABELS[key]
    tz = reporting_timezone()
    now_utc = ensure_aware(now) or datetime.now(timezone.utc)
    today = now_utc.astimezone(tz).date()

    if key == "all_time":
        return Timeframe(
            key=key, label=label,
            start=None, end=None, previous_start=None, previous_end=None,
            today=today, start_date=None, end_date=None,
        )

    granularity, offset = _SPEC[key]
    start_d, end_d = _period(granularity, offset, today)
    prev_start_d, prev_end_d = _period(granularity, offset + 1, today)

    def utc(d: date_cls) -> datetime:
        return datetime.combine(d, time.min, tzinfo=tz).astimezone(timezone.utc)

    return Timeframe(
        key=key,
        label=label,
        start=utc(start_d),
        end=utc(end_d),
        previous_start=utc(prev_start_d),
        previous_end=utc(prev_end_d),
        today=today,
        start_date=start_d,
        end_date=end_d - timedelta(days=1),
        previous_start_date=prev_start_d,
        previous_end_date=prev_end_d - timedelta(days=1),
    )


def pct_change(current: Optional[int], previous: Optional[int]) -> Optional[float]:
    """Percent change from ``previous`` to ``current``, or ``None``.

    ``None`` means "no comparable baseline": either there is no previous period
    (``all_time``) or the previous period was zero, where a percentage is
    undefined — 0 → 5 is not "+500%", it's new activity. Callers render that as
    "new"/"—" instead of a misleading number.
    """
    if previous is None or current is None or previous == 0:
        return None
    return round(((current - previous) / previous) * 100, 1)
