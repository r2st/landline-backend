"""Tests for per-contact timezone-aware send window.

Time is made deterministic by patching ``send_limiter.datetime`` with a fake
whose ``now(tz)`` returns a controlled local time keyed off the timezone, so we
can prove the window is evaluated in the CONTACT's timezone.
"""

from datetime import datetime as real_datetime

from app.services import send_limiter
from app.services.send_limiter import get_send_limiter

# 2026-07-15 is a Wednesday; 2026-07-18 is a Saturday.
_WEEKDAY = (2026, 7, 15)
_WEEKEND = (2026, 7, 18)

# Map a timezone key to the local hour the fake clock reports for it.
_TZ_HOURS = {
    "America/Toronto": 12,       # midday → in window
    "America/Los_Angeles": 5,    # 5 AM → outside window
    "Europe/London": 15,         # afternoon → in window
}


def _install_fake_clock(monkeypatch, date=_WEEKDAY, tz_hours=None):
    tz_hours = tz_hours or _TZ_HOURS

    # This file is about the window, so opt back in: conftest holds it open for
    # the rest of the suite so the send path isn't wall-clock dependent.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "send_window_enabled", True)

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            key = getattr(tz, "key", None)
            hour = tz_hours.get(key, 12)
            return real_datetime(date[0], date[1], date[2], hour, 0, tzinfo=tz)

    monkeypatch.setattr(send_limiter, "datetime", _FakeDateTime)


def test_uses_contact_timezone_when_in_window(monkeypatch):
    _install_fake_clock(monkeypatch)
    limiter = get_send_limiter()
    ok, reason = limiter.within_send_window("America/Toronto")
    assert ok, reason
    assert "America/Toronto" in reason


def test_contact_timezone_outside_window_blocks(monkeypatch):
    _install_fake_clock(monkeypatch)
    limiter = get_send_limiter()
    # 5 AM in LA is outside the 8-18 window even though Toronto (default) is open.
    ok, reason = limiter.within_send_window("America/Los_Angeles")
    assert not ok
    assert "America/Los_Angeles" in reason


def test_none_timezone_uses_global_default(monkeypatch):
    _install_fake_clock(monkeypatch)
    limiter = get_send_limiter()
    # Global default is America/Toronto (midday) → open.
    ok, reason = limiter.within_send_window(None)
    assert ok
    assert limiter.settings.send_window_timezone in reason


def test_invalid_contact_timezone_falls_back_to_global(monkeypatch):
    _install_fake_clock(monkeypatch)
    limiter = get_send_limiter()
    ok, reason = limiter.within_send_window("Not/AZone")
    # Falls back to the (open) global default rather than erroring.
    assert ok
    assert limiter.settings.send_window_timezone in reason


def test_weekend_blocks_when_weekdays_only(monkeypatch):
    _install_fake_clock(monkeypatch, date=_WEEKEND)
    limiter = get_send_limiter()
    ok, reason = limiter.within_send_window("America/Toronto")
    assert not ok
    assert "weekend" in reason.lower()


def test_disabled_window_always_open(monkeypatch):
    _install_fake_clock(monkeypatch, date=_WEEKEND)  # even a weekend
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter.settings, "send_window_enabled", False)
    ok, _ = limiter.within_send_window("America/Los_Angeles")
    assert ok
