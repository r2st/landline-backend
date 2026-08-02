"""Per-mailbox send pacing — a mailbox never bursts.

The properties that matter:
  * one claim wins, the rest are told to wait (concurrency is the whole point);
  * a refusal reports how long to wait, so the caller can defer accurately;
  * the gap is jittered, because a fixed gap is its own fingerprint;
  * Redis being down degrades pacing, never blocks sending.
"""

import uuid

import pytest

from app.config import get_settings
from app.services import send_pacing


@pytest.fixture(autouse=True)
def _clean_pacing_state():
    """No Redis in the test env, so these exercise the degraded (per-process)
    path deliberately — state has to be reset between tests either way."""
    send_pacing._reset_for_tests()
    yield
    send_pacing._reset_for_tests()


@pytest.fixture
def pacing_on(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "send_pacing_enabled", True)
    monkeypatch.setattr(settings, "send_min_gap_seconds", 60)
    monkeypatch.setattr(settings, "send_gap_jitter_seconds", 0)
    # A cached client from another test's fake would answer these calls; force
    # the local path so this file tests one implementation, not whichever ran first.
    monkeypatch.setattr(send_pacing, "_redis_disabled", True)
    return settings


@pytest.mark.asyncio
async def test_first_send_is_allowed(pacing_on):
    decision = await send_pacing.claim_send_slot(uuid.uuid4())
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_second_send_from_the_same_mailbox_waits(pacing_on):
    account = uuid.uuid4()
    assert (await send_pacing.claim_send_slot(account)).allowed is True

    second = await send_pacing.claim_send_slot(account)
    assert second.allowed is False
    assert 0 < second.wait_seconds <= 60
    assert "paced" in second.reason


@pytest.mark.asyncio
async def test_a_different_mailbox_is_unaffected(pacing_on):
    """Pacing is per-mailbox: one busy inbox must not stall the whole fleet."""
    busy = uuid.uuid4()
    await send_pacing.claim_send_slot(busy)
    assert (await send_pacing.claim_send_slot(uuid.uuid4())).allowed is True


@pytest.mark.asyncio
async def test_the_window_expires(pacing_on):
    account = uuid.uuid4()
    await send_pacing.claim_send_slot(account, now=1_000_000.0)

    still_early = await send_pacing.claim_send_slot(account, now=1_000_030.0)
    assert still_early.allowed is False
    assert still_early.wait_seconds == pytest.approx(30, abs=1)

    later = await send_pacing.claim_send_slot(account, now=1_000_061.0)
    assert later.allowed is True


@pytest.mark.asyncio
async def test_releasing_frees_the_mailbox_immediately(pacing_on):
    """A soft skip didn't put anything on the wire, so the next enrollment
    behind it shouldn't serve the no-send's sentence."""
    account = uuid.uuid4()
    await send_pacing.claim_send_slot(account)
    assert (await send_pacing.claim_send_slot(account)).allowed is False

    await send_pacing.release_send_slot(account)
    assert (await send_pacing.claim_send_slot(account)).allowed is True


@pytest.mark.asyncio
async def test_disabled_never_paces(monkeypatch):
    monkeypatch.setattr(get_settings(), "send_pacing_enabled", False)
    account = uuid.uuid4()
    for _ in range(5):
        assert (await send_pacing.claim_send_slot(account)).allowed is True


@pytest.mark.asyncio
async def test_unknown_mailbox_is_allowed_through(pacing_on):
    """Pacing a mailbox we can't name would either block every such send or pace
    them all against one shared key. Both are worse than not pacing."""
    assert (await send_pacing.claim_send_slot(None)).allowed is True
    assert (await send_pacing.claim_send_slot(None)).allowed is True


def test_jitter_widens_the_gap(monkeypatch):
    """A fixed gap is a fingerprint — 09:00:00, 09:01:30, 09:03:00 is as
    machine-shaped as a burst, just slower."""
    settings = get_settings()
    monkeypatch.setattr(settings, "send_min_gap_seconds", 60)
    monkeypatch.setattr(settings, "send_gap_jitter_seconds", 120)

    gaps = {send_pacing._gap_seconds(settings) for _ in range(50)}
    assert all(60 <= g <= 180 for g in gaps)
    assert len(gaps) > 1, "jittered gaps must not all be identical"


def test_no_jitter_gives_the_exact_minimum(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "send_min_gap_seconds", 45)
    monkeypatch.setattr(settings, "send_gap_jitter_seconds", 0)
    assert send_pacing._gap_seconds(settings) == 45


@pytest.mark.asyncio
async def test_redis_failure_degrades_rather_than_blocking(pacing_on, monkeypatch):
    """A Redis blip must not stop outreach. Pacing that fails closed is a worse
    outcome than the burst it prevents."""
    class ExplodingRedis:
        async def set(self, *a, **k):
            raise RuntimeError("redis is down")

        async def ttl(self, *a, **k):
            raise RuntimeError("redis is down")

    monkeypatch.setattr(send_pacing, "_redis_disabled", False)
    monkeypatch.setattr(send_pacing, "_redis", ExplodingRedis())

    account = uuid.uuid4()
    first = await send_pacing.claim_send_slot(account)
    assert first.allowed is True
    assert "degraded" in first.reason

    second = await send_pacing.claim_send_slot(account)
    assert second.allowed is False
    assert "degraded" in second.reason


@pytest.mark.asyncio
async def test_redis_claim_is_exclusive(pacing_on, monkeypatch):
    """The Redis path is the one that matters in production: N workers ask at
    once and exactly one may send."""
    class FakeRedis:
        def __init__(self):
            self.store = {}

        async def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return None
            self.store[key] = (value, ex)
            return True

        async def ttl(self, key):
            return self.store.get(key, (None, 0))[1]

        async def delete(self, key):
            self.store.pop(key, None)

    fake = FakeRedis()
    monkeypatch.setattr(send_pacing, "_redis_disabled", False)
    monkeypatch.setattr(send_pacing, "_redis", fake)

    account = uuid.uuid4()
    decisions = [await send_pacing.claim_send_slot(account) for _ in range(5)]
    assert sum(1 for d in decisions if d.allowed) == 1
    assert all(d.wait_seconds > 0 for d in decisions if not d.allowed)

    await send_pacing.release_send_slot(account)
    assert (await send_pacing.claim_send_slot(account)).allowed is True
