"""The daily cap rolls over outside the sending day, not at midnight UTC.

Three counters answer "has this mailbox sent enough today?" — the org daily
cap's Redis key, the row count that seeds it, and the per-account warm-up
counter — and all three keyed themselves on the UTC date.

That is only the right boundary if the sender's day sits inside a UTC day, and
this system deliberately does not send inside one: ``within_send_window`` takes
the **contact's** timezone (``tasks/outreach.py`` passes ``contact.timezone`` at
both dispatch sites). With the shipped defaults — 08:00–18:00, America/Toronto —
a contact in Los Angeles is inside their window from 15:00 UTC to 01:00 UTC the
next day. 00:00 UTC is 17:00 for them, one hour before it closes.

So every counter reset while the mailbox was still sending, and that last hour
got a fresh full allowance. On a day-1 warm-up plan of 5/day the mailbox put ten
on the wire and every warm-up check afterwards read 5/5, because the counter it
reads is the one that rolled. Nothing errors and the ramp graph looks textbook;
what notices is the receiving side, weeks later.

The fix anchors all three to the sender's local midnight. That instant is
guaranteed to fall outside the sending window, because the window is
``start_hour``–``end_hour`` on a 24h clock in that same zone and so cannot span
local midnight.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.config import get_settings
from app.services import send_day, warmup
from app.services.send_limiter import _daily_key, _seconds_until_day_rollover

TORONTO = ZoneInfo("America/Toronto")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _restore_tz():
    s = get_settings()
    before = s.send_window_timezone
    yield
    s.send_window_timezone = before


def _set_tz(name: str):
    get_settings().send_window_timezone = name


# --- The boundary itself ---------------------------------------------------------


def test_day_start_is_local_midnight_expressed_in_utc():
    _set_tz("America/Toronto")
    # 2026-07-15 is EDT (UTC-4), so local midnight is 04:00 UTC.
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    assert send_day.day_start(now) == datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc)


def test_day_start_tracks_the_dst_offset():
    """January is EST (UTC-5); the boundary moves with the offset, not the date."""
    _set_tz("America/Toronto")
    now = datetime(2026, 1, 15, 18, 0, tzinfo=timezone.utc)
    assert send_day.day_start(now) == datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)


def test_the_boundary_falls_outside_the_configured_send_window():
    """The property the whole fix rests on, asserted rather than assumed.

    The window cannot span local midnight (it is two hours on a 24h clock in the
    same zone), so local midnight is the one instant the sender is never sending
    at — whatever hours an operator configures.
    """
    _set_tz("America/Toronto")
    s = get_settings()
    boundary_local_hour = (
        send_day.day_start(datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc))
        .astimezone(TORONTO)
        .hour
    )
    assert boundary_local_hour == 0
    assert not (s.send_window_start_hour <= 0 < s.send_window_end_hour)


def test_utc_midnight_was_inside_a_west_coast_contacts_window():
    """The failing case, stated as a fact about the clock.

    This is what made the UTC boundary wrong: it is not merely a different
    instant, it is one the system sends at.
    """
    s = get_settings()
    utc_midnight_in_la = datetime(2026, 7, 16, 0, 0, tzinfo=timezone.utc).astimezone(
        LOS_ANGELES
    )
    assert s.send_window_start_hour <= utc_midnight_in_la.hour < s.send_window_end_hour


def test_utc_boundary_and_send_day_boundary_are_different_days():
    """Between local midnight and 00:00 UTC the two answers disagree by a day."""
    _set_tz("America/Toronto")
    # 22:00 UTC on the 15th = 18:00 EDT on the 15th. Same local day, same UTC day.
    assert send_day.day_key(datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)) == "2026-07-15"
    # 02:00 UTC on the 16th = 22:00 EDT on the *15th*. New UTC day, same local day.
    assert send_day.day_key(datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)) == "2026-07-15"


def test_day_key_and_day_start_agree():
    """The Redis key and the seed it is filled from must roll together."""
    _set_tz("America/Toronto")
    for hour in range(24):
        now = datetime(2026, 7, 15, hour, 30, tzinfo=timezone.utc)
        assert send_day.day_key(now) == send_day.day_start(now).astimezone(
            TORONTO
        ).strftime("%Y-%m-%d")


def test_unknown_timezone_degrades_to_utc():
    """A bad config value must not take down every path that needs the date."""
    _set_tz("Mars/Olympus_Mons")
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    assert send_day.day_start(now) == datetime(2026, 7, 15, 0, 0, tzinfo=timezone.utc)


def test_naive_now_is_read_as_utc():
    _set_tz("America/Toronto")
    aware = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    assert send_day.day_start(datetime(2026, 7, 15, 18, 0)) == send_day.day_start(aware)


# --- The TTL ---------------------------------------------------------------------


def test_ttl_outlives_the_day_it_counts():
    _set_tz("America/Toronto")
    now = datetime(2026, 7, 15, 22, 0, tzinfo=timezone.utc)  # 18:00 EDT
    ttl = _seconds_until_day_rollover(now)
    # Six hours to local midnight, plus the grace margin.
    assert 6 * 3600 < ttl < 7 * 3600
    assert now + timedelta(seconds=ttl) > send_day.day_start(now) + timedelta(days=1)


def test_ttl_never_degenerates_to_zero_at_the_boundary():
    _set_tz("America/Toronto")
    a_second_before = send_day.day_start(
        datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    ) - timedelta(seconds=1)
    assert _seconds_until_day_rollover(a_second_before) >= 60


# --- The Redis key ----------------------------------------------------------------


def test_daily_key_does_not_roll_during_the_west_coast_afternoon():
    """The bug, at the surface that carried it.

    17:00 and 17:30 Pacific are one hour apart inside one business day and used
    to sit either side of a counter reset.
    """
    _set_tz("America/Toronto")
    before = datetime(2026, 7, 15, 23, 59, tzinfo=timezone.utc)  # 16:59 PDT
    after = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)   # 17:30 PDT
    assert _daily_key(before) == _daily_key(after)
    # The control: it *does* roll, once, at the sender's local midnight.
    next_day = datetime(2026, 7, 16, 5, 0, tzinfo=timezone.utc)  # 01:00 EDT
    assert _daily_key(next_day) != _daily_key(before)


# --- The warm-up counter ------------------------------------------------------------


class _Account:
    def __init__(self, count, reset_at):
        self.daily_send_count = count
        self.daily_count_reset_at = reset_at


def test_warmup_counter_survives_utc_midnight():
    """A stamp from this afternoon is still today's, even past 00:00 UTC.

    Mutation control for the ramp half: against the old ``reset_at.date() <
    today`` in UTC this returns 0, which is what handed a day-1 mailbox a second
    full allowance.
    """
    _set_tz("America/Toronto")
    # Local midnight today is 04:00 UTC, so anything after that is today's.
    stamped = send_day.day_start() + timedelta(hours=1)
    assert warmup.used_today(_Account(5, stamped)) == 5


def test_warmup_counter_zeroes_on_a_stamp_from_before_local_midnight():
    _set_tz("America/Toronto")
    stale = send_day.day_start() - timedelta(minutes=1)
    assert warmup.used_today(_Account(5, stale)) == 0


def test_warmup_counter_treats_a_naive_stamp_as_utc():
    """SQLite drops the offset; the comparison must not raise on it."""
    _set_tz("America/Toronto")
    stamped = (send_day.day_start() + timedelta(hours=1)).replace(tzinfo=None)
    assert warmup.used_today(_Account(3, stamped)) == 3


def test_warmup_counter_zero_without_a_stamp():
    assert warmup.used_today(_Account(9, None)) == 0


# --- The row-count seed --------------------------------------------------------------


@pytest.mark.asyncio
async def test_row_seed_counts_from_the_same_boundary(db_session, monkeypatch):
    """``_db_sent_today`` must ask the window ``_daily_key`` names.

    An eviction re-seeds the counter from this number; a seed taken over a
    different window hands back allowance the mailbox already spent.
    """
    _set_tz("America/Toronto")
    from app.services.send_limiter import SendLimiter

    seen = {}

    async def _capture(self, db, since):
        seen["since"] = since
        return 0

    monkeypatch.setattr(SendLimiter, "_db_sent_since", _capture)

    now = datetime(2026, 7, 16, 0, 30, tzinfo=timezone.utc)  # 20:30 EDT on the 15th
    await SendLimiter()._db_sent_today(db_session, now)

    assert seen["since"] == send_day.day_start(now)
    assert seen["since"].astimezone(TORONTO).strftime("%Y-%m-%d") == "2026-07-15"
