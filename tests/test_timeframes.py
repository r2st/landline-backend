"""Unit tests for the reporting-timeframe resolver behind the stats endpoints.

These are pure date math — no DB, no app — so they pin the boundaries a console
number is only as trustworthy as: week starts, month rollovers, the reporting
timezone offset, and the half-open windows that keep consecutive periods from
double-counting the same event.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services import timeframes as tf_mod
from app.services.timeframes import (
    TIMEFRAMES,
    in_range,
    normalize_timeframe,
    pct_change,
    resolve_timeframe,
)


@pytest.fixture
def utc_reporting(monkeypatch):
    """Pin the reporting timezone to UTC so window bounds are exact."""
    monkeypatch.setattr(tf_mod, "reporting_timezone", lambda: timezone.utc)


# A Wednesday, mid-month, mid-afternoon UTC.
NOW = datetime(2026, 7, 15, 14, 30, tzinfo=timezone.utc)


def _utc(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc)


# ─── normalize / validation ──────────────────────────────────────────────────


def test_all_documented_timeframes_resolve(utc_reporting):
    for key in TIMEFRAMES:
        tf = resolve_timeframe(key, now=NOW)
        assert tf.key == key
        assert tf.label


def test_normalize_accepts_case_and_whitespace():
    assert normalize_timeframe("  This_Week ") == "this_week"


def test_normalize_defaults_to_today():
    assert normalize_timeframe(None) == "today"
    assert normalize_timeframe("") == "today"


def test_normalize_rejects_unknown_key():
    with pytest.raises(ValueError) as exc:
        normalize_timeframe("last_fortnight")
    # The message must name the valid options — the router turns it into a 400.
    assert "this_week" in str(exc.value)


# ─── day windows ─────────────────────────────────────────────────────────────


def test_today_window_is_the_calendar_day(utc_reporting):
    tf = resolve_timeframe("today", now=NOW)
    assert tf.start == _utc(2026, 7, 15)
    assert tf.end == _utc(2026, 7, 16)
    # Previous period is yesterday.
    assert tf.previous_start == _utc(2026, 7, 14)
    assert tf.previous_end == _utc(2026, 7, 15)
    assert tf.start_date.isoformat() == "2026-07-15"
    assert tf.end_date.isoformat() == "2026-07-15"


def test_yesterday_window_and_its_baseline(utc_reporting):
    tf = resolve_timeframe("yesterday", now=NOW)
    assert (tf.start, tf.end) == (_utc(2026, 7, 14), _utc(2026, 7, 15))
    assert (tf.previous_start, tf.previous_end) == (_utc(2026, 7, 13), _utc(2026, 7, 14))


# ─── week windows ────────────────────────────────────────────────────────────


def test_this_week_starts_monday(utc_reporting):
    # 2026-07-15 is a Wednesday; the ISO week opened Monday the 13th.
    tf = resolve_timeframe("this_week", now=NOW)
    assert tf.start == _utc(2026, 7, 13)
    assert tf.end == _utc(2026, 7, 20)
    assert tf.start.weekday() == 0
    # Inclusive local dates for the drill-down link.
    assert tf.start_date.isoformat() == "2026-07-13"
    assert tf.end_date.isoformat() == "2026-07-19"


def test_this_week_on_a_monday_does_not_reach_backwards(utc_reporting):
    monday = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
    tf = resolve_timeframe("this_week", now=monday)
    assert tf.start == _utc(2026, 7, 13)


def test_this_week_baseline_is_last_week(utc_reporting):
    this_week = resolve_timeframe("this_week", now=NOW)
    last_week = resolve_timeframe("last_week", now=NOW)
    assert (this_week.previous_start, this_week.previous_end) == (last_week.start, last_week.end)
    assert last_week.start == _utc(2026, 7, 6)
    assert last_week.end == _utc(2026, 7, 13)


# ─── month windows ───────────────────────────────────────────────────────────


def test_this_month_window(utc_reporting):
    tf = resolve_timeframe("this_month", now=NOW)
    assert tf.start == _utc(2026, 7, 1)
    assert tf.end == _utc(2026, 8, 1)
    assert tf.end_date.isoformat() == "2026-07-31"


def test_last_month_window(utc_reporting):
    tf = resolve_timeframe("last_month", now=NOW)
    assert tf.start == _utc(2026, 6, 1)
    assert tf.end == _utc(2026, 7, 1)
    # Compared against May — a real calendar month, not a fixed 30 days.
    assert (tf.previous_start, tf.previous_end) == (_utc(2026, 5, 1), _utc(2026, 6, 1))


def test_month_windows_roll_over_the_year(utc_reporting):
    january = datetime(2026, 1, 20, 12, 0, tzinfo=timezone.utc)
    this_month = resolve_timeframe("this_month", now=january)
    assert this_month.start == _utc(2026, 1, 1)
    # December of the prior year is the baseline.
    assert this_month.previous_start == _utc(2025, 12, 1)
    assert this_month.previous_end == _utc(2026, 1, 1)

    last_month = resolve_timeframe("last_month", now=january)
    assert (last_month.start, last_month.end) == (_utc(2025, 12, 1), _utc(2026, 1, 1))
    assert last_month.previous_start == _utc(2025, 11, 1)


def test_february_length_is_respected(utc_reporting):
    march = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
    tf = resolve_timeframe("last_month", now=march)
    assert (tf.start, tf.end) == (_utc(2026, 2, 1), _utc(2026, 3, 1))
    assert (tf.end - tf.start).days == 28


# ─── all_time ────────────────────────────────────────────────────────────────


def test_all_time_is_unbounded_and_has_no_baseline(utc_reporting):
    tf = resolve_timeframe("all_time", now=NOW)
    assert tf.start is None and tf.end is None
    assert tf.previous_start is None and tf.previous_end is None
    assert tf.has_previous is False
    assert tf.start_date is None and tf.end_date is None
    # Still reports what day it is, for the console header.
    assert tf.today.isoformat() == "2026-07-15"


# ─── reporting timezone ──────────────────────────────────────────────────────


def test_windows_are_anchored_to_the_reporting_timezone(monkeypatch):
    """A day boundary is local midnight, not UTC midnight.

    Landline reports in Asia/Kolkata (UTC+5:30) by default, so "today" starts at
    18:30 UTC the previous evening. Getting this wrong would file the first 5½
    hours of every local day under yesterday.
    """
    monkeypatch.setattr(tf_mod, "reporting_timezone", lambda: ZoneInfo("Asia/Kolkata"))
    tf = resolve_timeframe("today", now=NOW)
    assert tf.start == datetime(2026, 7, 14, 18, 30, tzinfo=timezone.utc)
    assert tf.end == datetime(2026, 7, 15, 18, 30, tzinfo=timezone.utc)
    assert tf.start_date.isoformat() == "2026-07-15"


def test_reporting_day_uses_local_clock(monkeypatch):
    """Late-evening UTC is already tomorrow in the reporting timezone."""
    monkeypatch.setattr(tf_mod, "reporting_timezone", lambda: ZoneInfo("Asia/Kolkata"))
    tf = resolve_timeframe("today", now=datetime(2026, 7, 15, 20, 0, tzinfo=timezone.utc))
    assert tf.today.isoformat() == "2026-07-16"


def test_naive_now_is_treated_as_utc(utc_reporting):
    tf = resolve_timeframe("today", now=datetime(2026, 7, 15, 14, 30))
    assert tf.start == _utc(2026, 7, 15)


# ─── windows tile without overlap ────────────────────────────────────────────


@pytest.mark.parametrize("key", ["today", "yesterday", "this_week", "last_week",
                                "this_month", "last_month"])
def test_previous_period_abuts_the_current_one(utc_reporting, key):
    tf = resolve_timeframe(key, now=NOW)
    assert tf.previous_end == tf.start, "a gap or overlap would mis-attribute events"


def test_midnight_event_belongs_to_exactly_one_period(utc_reporting):
    tf = resolve_timeframe("today", now=NOW)
    boundary = tf.start
    assert in_range(boundary, tf.start, tf.end) is True
    assert in_range(boundary, tf.previous_start, tf.previous_end) is False


# ─── in_range ────────────────────────────────────────────────────────────────


def test_in_range_is_half_open():
    start, end = _utc(2026, 7, 15), _utc(2026, 7, 16)
    assert in_range(start, start, end) is True
    assert in_range(end, start, end) is False
    assert in_range(end - timedelta(microseconds=1), start, end) is True


def test_in_range_unbounded_sides():
    ts = _utc(2026, 7, 15)
    assert in_range(ts, None, None) is True
    assert in_range(ts, None, _utc(2026, 7, 16)) is True
    assert in_range(ts, _utc(2026, 7, 16), None) is False


def test_in_range_rejects_missing_timestamp():
    # An un-stamped row didn't happen — it must never land in a window.
    assert in_range(None, None, None) is False


def test_in_range_normalizes_naive_timestamps():
    """SQLite hands back naive datetimes; they're UTC and must still compare."""
    naive = datetime(2026, 7, 15, 12, 0)
    assert in_range(naive, _utc(2026, 7, 15), _utc(2026, 7, 16)) is True


# ─── pct_change ──────────────────────────────────────────────────────────────


def test_pct_change_growth_and_decline():
    assert pct_change(120, 100) == 20.0
    assert pct_change(80, 100) == -20.0
    assert pct_change(100, 100) == 0.0


def test_pct_change_rounds_to_one_decimal():
    assert pct_change(7, 3) == 133.3


def test_pct_change_without_a_baseline_is_none():
    # 0 → 5 is new activity, not "+500%"; the UI renders "new" instead.
    assert pct_change(5, 0) is None
    assert pct_change(0, 0) is None
    assert pct_change(5, None) is None
    assert pct_change(None, 5) is None
