"""Tests for per-account warm-up + throttle enforcement in the send path.

NOTE ON ORDERING: async DB tests are defined before the plain (non-DB) tests in
this file. The suite's session-scoped event loop makes intra-file ordering
matter — see reference_landline-test-suite-ordering — so DB-touching tests come
first.
"""

import uuid
from datetime import datetime, timezone, timedelta

from app.models.gmail_account import GmailAccount
from app.models.sender_health import SenderHealth
from app.services.send_limiter import get_send_limiter
from app.services.email_dispatch import (
    _filter_by_sender_health,
    get_next_rotation_account,
)


def _account(email="s@apprend.co", days_old=100, daily_sent=0, sending=True):
    """A connected mailbox that has sent ``daily_sent`` messages **today**.

    ``daily_count_reset_at`` is stamped whenever the count is non-zero, because
    that is the only state production can produce: ``record_account_send``
    writes both in one statement, and the count is meaningless without the
    stamp saying which day it belongs to. Leaving it NULL described a mailbox
    with 20 sends on no particular day, which the cap readers now correctly
    treat as 0 — they consult ``warmup.used_today`` rather than the raw column,
    so that yesterday's spent ramp stops freezing a mailbox out of rotation
    today.
    """
    now = datetime.now(timezone.utc)
    return GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=str(uuid.uuid4()),
        refresh_token_encrypted="x", is_active=False, sending_enabled=sending,
        status="connected", daily_send_count=daily_sent,
        daily_count_reset_at=now if daily_sent else None,
        connected_at=now - timedelta(days=days_old),
    )


async def _persist(db, acct):
    db.add(acct)
    await db.flush()
    return acct


# --- account_capacity combines throttle + warm-up (async DB) ---

async def test_capacity_takes_lower_of_throttle_and_warmup(db_session):
    limiter = get_send_limiter()
    # New account (week 1 → cap 10), no health row → throttle 1.0.
    acct = await _persist(db_session, _account(days_old=2, daily_sent=0))
    cap = await limiter.account_capacity(db_session, acct)
    assert cap["warmup_daily_limit"] == 20
    assert cap["effective_daily_limit"] == 20  # min(100*1.0, 20)
    assert cap["can_send"] is True


async def test_capacity_throttle_beats_warmup_when_lower(db_session):
    limiter = get_send_limiter()
    acct = await _persist(db_session, _account(days_old=200, daily_sent=0))  # warmed
    db_session.add(SenderHealth(
        id=uuid.uuid4(), account_id=acct.id, account_type="gmail",
        account_email=acct.email, reputation_score=65, throttle_factor=0.4,
        status="throttled",
    ))
    await db_session.flush()
    cap = await limiter.account_capacity(db_session, acct)
    assert cap["warmup_daily_limit"] is None
    assert cap["effective_daily_limit"] == 40  # 100 * 0.4
    assert cap["throttle_factor"] == 0.4


async def test_capacity_exhausted_when_warmup_hit(db_session):
    limiter = get_send_limiter()
    acct = await _persist(db_session, _account(days_old=2, daily_sent=20))  # hit week-1 cap
    cap = await limiter.account_capacity(db_session, acct)
    assert cap["remaining_today"] == 0
    assert cap["can_send"] is False


async def test_filter_excludes_warmup_capped_and_critical(db_session):
    fresh_capped = await _persist(db_session, _account(email="new@a.co", days_old=1, daily_sent=20))
    healthy = await _persist(db_session, _account(email="ok@a.co", days_old=200, daily_sent=0))
    crit = await _persist(db_session, _account(email="bad@a.co", days_old=200, daily_sent=0))
    db_session.add(SenderHealth(
        id=uuid.uuid4(), account_id=crit.id, account_type="gmail",
        account_email=crit.email, reputation_score=10, throttle_factor=0.1,
        status="critical",
    ))
    await db_session.flush()

    kept = await _filter_by_sender_health(db_session, [fresh_capped, healthy, crit])
    kept_ids = {a.id for a in kept}
    assert healthy.id in kept_ids
    assert fresh_capped.id not in kept_ids  # over its week-1 warm-up cap
    assert crit.id not in kept_ids          # critical health


async def test_rotation_prefers_account_with_capacity(db_session):
    # capped fresh account vs a warmed healthy one → rotation must pick the healthy one.
    await _persist(db_session, _account(email="capped@a.co", days_old=1, daily_sent=20))
    healthy = await _persist(db_session, _account(email="healthy@a.co", days_old=200, daily_sent=0))
    chosen = await get_next_rotation_account(db_session)
    assert chosen is not None
    assert chosen.id == healthy.id


async def test_rotation_falls_back_when_all_capped(db_session):
    # Every account is over its warm-up cap → rotation still returns one (never
    # halts sending entirely; the queue's global gate handles a full stop).
    a = await _persist(db_session, _account(email="a@a.co", days_old=1, daily_sent=20))
    b = await _persist(db_session, _account(email="b@a.co", days_old=1, daily_sent=20))
    chosen = await get_next_rotation_account(db_session)
    assert chosen is not None
    assert chosen.id in {a.id, b.id}


# --- warm-up ladder (pure, non-DB) ---

def test_account_warmup_limit_ramps_by_age():
    limiter = get_send_limiter()
    assert limiter.account_warmup_limit(_account(days_old=1)) == 20    # week 1
    assert limiter.account_warmup_limit(_account(days_old=9)) == 50    # week 2
    assert limiter.account_warmup_limit(_account(days_old=16)) == 100  # week 3
    assert limiter.account_warmup_limit(_account(days_old=30)) is None  # warmed


def test_account_warmup_disabled(monkeypatch):
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter.settings, "account_warmup_enabled", False)
    assert limiter.account_warmup_limit(_account(days_old=1)) is None
