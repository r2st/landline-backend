"""The daily send cap must hold when workers send concurrently.

``can_send_today`` counts rows that already exist, so every worker in a burst
reads the same pre-send number and each concludes it is the one still under the
cap. These tests pin the atomic reservation that replaces it: N concurrent
claims against a limit of L yield exactly L grants, unused claims are returned,
and a Redis outage degrades to the old row-count behaviour instead of removing
the limit.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from app.services import send_limiter as sl
from app.services.send_limiter import get_send_limiter


class FakeRedis:
    """Minimal async Redis with the atomicity that matters here.

    Real Redis executes commands on one thread, so INCR is indivisible. asyncio
    gives the same guarantee for a synchronous critical section between awaits,
    which is what these methods are — so a fake that mutates a dict without
    awaiting reproduces the ordering the production client provides.
    """

    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttl_store: dict[str, int] = {}
        self.incr_calls = 0

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = int(value)
        if ex is not None:
            self.ttl_store[key] = ex
        return True

    async def incr(self, key):
        self.incr_calls += 1
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def ttl(self, key):
        return self.ttl_store.get(key, -1)

    async def expire(self, key, seconds):
        self.ttl_store[key] = seconds
        return True


class ExplodingRedis(FakeRedis):
    async def incr(self, key):
        raise RuntimeError("redis connection reset")


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()

    async def _get():
        return client

    monkeypatch.setattr(sl, "_get_redis", _get)
    return client


async def _limiter_with_limit(monkeypatch, limit: int):
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "_get_daily_limit_db", lambda db: _async(limit))
    monkeypatch.setattr(limiter, "_db_sent_today", lambda db, now=None: _async(0))
    return limiter


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_the_limit(monkeypatch, fake_redis):
    """20 workers, cap of 5 → exactly 5 grants.

    This is the bug in one line: with a row-count check all 20 would see "0 sent
    today, under the cap of 5" and all 20 would send.
    """
    limiter = await _limiter_with_limit(monkeypatch, 5)

    results = await asyncio.gather(
        *(limiter.reserve_daily_send(db=None) for _ in range(20))
    )

    granted = [r for r in results if r[0]]
    denied = [r for r in results if not r[0]]
    assert len(granted) == 5
    assert len(denied) == 15
    assert all("Daily limit reached" in reason for _, reason in denied)


@pytest.mark.asyncio
async def test_counter_seeds_from_the_database_on_first_use(monkeypatch, fake_redis):
    """A restart mid-day must resume from sends already recorded, not from zero."""
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "_get_daily_limit_db", lambda db: _async(10))
    monkeypatch.setattr(limiter, "_db_sent_today", lambda db, now=None: _async(9))

    allowed, reason = await limiter.reserve_daily_send(db=None)
    assert allowed is True
    assert "10/10" in reason

    # The 10th send exhausted the cap; the 11th is refused even though the Redis
    # key started life this second.
    allowed, _ = await limiter.reserve_daily_send(db=None)
    assert allowed is False


@pytest.mark.asyncio
async def test_seed_happens_once_not_per_reservation(monkeypatch, fake_redis):
    """SET NX must not re-seed on every claim, or the count would never rise."""
    limiter = await _limiter_with_limit(monkeypatch, 100)

    for _ in range(3):
        await limiter.reserve_daily_send(db=None)

    key = sl._daily_key()
    assert fake_redis.store[key] == 3


@pytest.mark.asyncio
async def test_release_returns_an_unused_slot(monkeypatch, fake_redis):
    """A send that never went out must not spend the day's allowance."""
    limiter = await _limiter_with_limit(monkeypatch, 2)

    assert (await limiter.reserve_daily_send(db=None))[0] is True
    assert (await limiter.reserve_daily_send(db=None))[0] is True
    assert (await limiter.reserve_daily_send(db=None))[0] is False

    await limiter.release_daily_send()

    # The returned slot is usable again.
    assert (await limiter.reserve_daily_send(db=None))[0] is True


@pytest.mark.asyncio
async def test_denied_reservation_does_not_consume_a_slot(monkeypatch, fake_redis):
    """An over-limit claim decrements itself back, so the counter can't run away.

    Without the give-back, a queue that keeps retrying past the cap would drive
    the counter to thousands and the next day's seed logic would be the only
    thing standing between that and a permanently jammed sender.
    """
    limiter = await _limiter_with_limit(monkeypatch, 1)
    await limiter.reserve_daily_send(db=None)

    for _ in range(5):
        assert (await limiter.reserve_daily_send(db=None))[0] is False

    assert fake_redis.store[sl._daily_key()] == 1


@pytest.mark.asyncio
async def test_double_release_cannot_manufacture_allowance(monkeypatch, fake_redis):
    """Releasing more than was claimed floors at zero rather than going negative."""
    limiter = await _limiter_with_limit(monkeypatch, 3)

    await limiter.release_daily_send()
    await limiter.release_daily_send()

    assert fake_redis.store[sl._daily_key()] == 0


@pytest.mark.asyncio
async def test_redis_outage_degrades_to_row_counts_not_to_no_limit(monkeypatch):
    """No Redis → the old per-worker check, and the reason says it's degraded."""
    async def _no_redis():
        return None

    monkeypatch.setattr(sl, "_get_redis", _no_redis)

    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "can_send_today", lambda db: _async((False, "Daily limit reached (50/50).")))

    allowed, reason = await limiter.reserve_daily_send(db=None)
    assert allowed is False
    assert "degraded" in reason


@pytest.mark.asyncio
async def test_redis_error_mid_call_falls_back(monkeypatch):
    """A connection dropped between SET and INCR must not grant an unchecked send."""
    client = ExplodingRedis()

    async def _get():
        return client

    monkeypatch.setattr(sl, "_get_redis", _get)

    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "_get_daily_limit_db", lambda db: _async(5))
    monkeypatch.setattr(limiter, "_db_sent_today", lambda db, now=None: _async(0))
    monkeypatch.setattr(limiter, "can_send_today", lambda db: _async((True, "OK (0/5 sent today)")))

    allowed, reason = await limiter.reserve_daily_send(db=None)
    assert allowed is True
    assert "degraded" in reason


def test_daily_key_rolls_over_at_utc_midnight():
    """Each UTC day gets its own counter, so yesterday's volume never leaks in."""
    a = sl._daily_key(datetime(2026, 7, 25, 23, 59, tzinfo=timezone.utc))
    b = sl._daily_key(datetime(2026, 7, 26, 0, 1, tzinfo=timezone.utc))
    assert a != b
    assert "2026-07-25" in a and "2026-07-26" in b


def test_key_ttl_outlives_the_day_it_counts():
    """The TTL must not expire the counter before the day it is counting ends."""
    late = datetime(2026, 7, 25, 23, 30, tzinfo=timezone.utc)
    early = datetime(2026, 7, 25, 0, 30, tzinfo=timezone.utc)
    assert sl._seconds_until_utc_midnight(late) >= 30 * 60
    assert sl._seconds_until_utc_midnight(early) > 23 * 3600


# --- The arithmetic behind the verdict ---------------------------------------


@pytest.mark.asyncio
async def test_daily_capacity_reports_the_numbers_can_send_today_decides_on(
    monkeypatch,
):
    """``daily_capacity`` and ``can_send_today`` must never disagree.

    Reporting surfaces need the limit and the spend, not the boolean — and the
    only thing worse than parsing them back out of the sentence is computing
    them a second, subtly different way.
    """
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "_get_daily_limit_db", lambda db: _async(70))
    monkeypatch.setattr(limiter, "_db_sent_today", lambda db, now=None: _async(45))

    assert await limiter.daily_capacity(db=None) == (70, 45)

    allowed, reason = await limiter.can_send_today(db=None)
    assert allowed is True
    assert "45/70" in reason


@pytest.mark.asyncio
async def test_daily_capacity_reports_a_spent_day_without_clamping(monkeypatch):
    """A day already at (or over) its limit reports what actually happened. A
    caller subtracting these is entitled to see the overshoot, not a floor."""
    limiter = get_send_limiter()
    monkeypatch.setattr(limiter, "_get_daily_limit_db", lambda db: _async(20))
    monkeypatch.setattr(limiter, "_db_sent_today", lambda db, now=None: _async(23))

    assert await limiter.daily_capacity(db=None) == (20, 23)
    allowed, _ = await limiter.can_send_today(db=None)
    assert allowed is False
