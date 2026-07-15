"""Tests for the launch sender configuration.

Covers the apprend.tech primary-sender wiring, the conservative 20/50/100
warm-up ramp, the Gmail-OAuth send scope, and the production kill-switch
default (paused until explicitly enabled).

ORDERING NOTE: async DB tests are defined before the plain (non-DB) tests in
this file — the suite's session-scoped event loop makes intra-file ordering
matter (see reference_landline-test-suite-ordering).
"""

from datetime import datetime, timezone, timedelta

import pytest

from app.config import get_settings
from app.services import system_settings
from app.services.email_dispatch import resolve_sender_info
from app.services.send_limiter import get_send_limiter


def _iso_days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


# ─── async / DB-touching tests first ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fallback_sender_reports_apprend_tech(db_session):
    """With no Gmail/SMTP account connected, the deliverability/test-send path
    reports the apprend.tech primary sender so domain auth checks the right
    domain before OAuth is wired up."""
    info = await resolve_sender_info(db_session)
    assert info.connected is False
    assert info.from_email == "suman.das@apprend.tech"
    # The domain the deliverability endpoint will authenticate.
    assert info.from_email.split("@", 1)[1] == "apprend.tech"


@pytest.mark.asyncio
async def test_kill_switch_default_off_when_config_flag_set(db_session, monkeypatch):
    """When sending_enabled_default is False (production) and the switch has
    never been set, sending is treated as PAUSED."""
    monkeypatch.setattr(get_settings(), "sending_enabled_default", False)

    assert await system_settings.is_sending_enabled(db_session) is False
    status = await system_settings.get_sending_status(db_session)
    assert status["enabled"] is False


@pytest.mark.asyncio
async def test_kill_switch_default_on_by_default(db_session):
    """Ships True (dev default) so existing behaviour/tests are unaffected."""
    assert get_settings().sending_enabled_default is True
    assert await system_settings.is_sending_enabled(db_session) is True


@pytest.mark.asyncio
async def test_explicit_setting_overrides_config_default(db_session, monkeypatch):
    """An explicit stored value always wins over the config default."""
    monkeypatch.setattr(get_settings(), "sending_enabled_default", False)
    # Operator turns sending ON explicitly → stored value beats the paused default.
    await system_settings.set_sending_enabled(db_session, True, updated_by="op")
    assert await system_settings.is_sending_enabled(db_session) is True


@pytest.mark.asyncio
async def test_warmup_ramp_uses_persisted_start(db_session):
    """The DB-backed daily limit follows the 20/50/100 ramp off the persisted
    warm-up start date."""
    limiter = get_send_limiter()

    await system_settings.set_value(
        db_session, system_settings.WARMUP_START_KEY, {"date": _iso_days_ago(0)}
    )
    assert await limiter._get_daily_limit_db(db_session) == 20  # week 1

    await system_settings.set_value(
        db_session, system_settings.WARMUP_START_KEY, {"date": _iso_days_ago(9)}
    )
    assert await limiter._get_daily_limit_db(db_session) == 50  # week 2

    await system_settings.set_value(
        db_session, system_settings.WARMUP_START_KEY, {"date": _iso_days_ago(16)}
    )
    assert await limiter._get_daily_limit_db(db_session) == 100  # week 3


# ─── pure / non-DB tests ────────────────────────────────────────────────────


def test_warmup_config_is_conservative_20_50_100():
    s = get_settings()
    assert s.warmup_daily_limit_week1 == 20
    assert s.warmup_daily_limit_week2 == 50
    assert s.warmup_daily_limit_week3 == 100
    # Per-account ramp matches the global ramp.
    assert s.account_warmup_daily_limit_week1 == 20
    assert s.account_warmup_daily_limit_week2 == 50
    assert s.account_warmup_daily_limit_week3 == 100
    # Steady-state cap equals the fully-warmed week-3 volume (no drop at day 21).
    assert s.max_emails_per_day == 100


def test_warmup_starts_at_launch_date():
    assert get_settings().warmup_start_date == "2026-07-15"


def test_daily_limit_ramp_config_only():
    """The config-only ramp (no DB) also follows 20 → 50 → 100 → steady 100."""
    limiter = get_send_limiter()
    assert limiter._daily_limit_for_start(_iso_days_ago(0)) == 20    # week 1
    assert limiter._daily_limit_for_start(_iso_days_ago(9)) == 50    # week 2
    assert limiter._daily_limit_for_start(_iso_days_ago(16)) == 100  # week 3
    assert limiter._daily_limit_for_start(_iso_days_ago(40)) == 100  # steady state


def test_gmail_send_scope_present():
    """Gmail OAuth must request gmail.send, and the send path must use it."""
    from app.services import google_oauth
    from app.services.gmail_sender import _SEND_SCOPES

    send_scope = "https://www.googleapis.com/auth/gmail.send"
    assert send_scope in google_oauth.SCOPES
    assert send_scope in _SEND_SCOPES


def test_primary_sender_is_apprend_tech():
    assert get_settings().primary_sender_email == "suman.das@apprend.tech"
