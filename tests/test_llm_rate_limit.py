"""Tests for per-provider LLM pacing and ``Retry-After`` handling.

The property under test is the one the product depends on: a free-tier rate
limit must cost us a *hop*, never a quality cliff. Two failure modes bracket it
and both are asserted here — pacing that turns into a stall (blocking a worker
on a busy provider while an idle one waits), and pacing that turns into a
degrade (falling back to static mail-merge copy because we were two seconds
early).

No network, no real clock: the wait indirection is patched, so a test that
exercises a 60-second window runs instantly.
"""

import time

import pytest

from app.config import Settings
from app.services import llm_rate_limit as rl
from app.services.llm_rate_limit import (
    DEFAULT_PARK_SECONDS,
    MAX_PARK_SECONDS,
    ProviderRateLimiter,
    parse_retry_after,
    provider_rpm,
    shortest_wait,
)


@pytest.fixture
def limiter() -> ProviderRateLimiter:
    return ProviderRateLimiter()


@pytest.fixture
def no_sleep(monkeypatch):
    """Record what we would have slept, without sleeping.

    Asserting on the *duration* matters as much as asserting it happened: a
    limiter that waits the wrong amount is a limiter that either bursts through
    the cap or stalls a worker, and both look like "it waited" from outside.
    """
    slept: list[float] = []

    async def _fake(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(rl, "_sleep", _fake)
    return slept


# ---------------------------------------------------------------------------
# Retry-After parsing
# ---------------------------------------------------------------------------
def test_retry_after_reads_delta_seconds():
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("30.5") == 30.5
    assert parse_retry_after(45) == 45.0


def test_retry_after_reads_the_http_date_form():
    """The RFC allows a date, and gateways in front of these APIs do send one."""
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) + timedelta(seconds=90)
    parsed = parse_retry_after(format_datetime(when))
    assert parsed is not None
    assert 80 <= parsed <= 95


def test_a_past_retry_after_date_means_now_not_a_negative_park():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    when = datetime.now(timezone.utc) - timedelta(hours=1)
    assert parse_retry_after(format_datetime(when)) == 0.0


def test_unparseable_retry_after_is_none_so_the_caller_can_default():
    assert parse_retry_after("soon") is None
    assert parse_retry_after("") is None
    assert parse_retry_after(None) is None


# ---------------------------------------------------------------------------
# Parking on 429
# ---------------------------------------------------------------------------
def test_a_429_parks_the_provider_for_the_stated_time(limiter):
    parked = limiter.note_rate_limited("openrouter", "60")
    assert parked == 60.0
    assert 55 < limiter.parked_for("openrouter") <= 60


def test_a_429_with_no_header_parks_for_the_default(limiter):
    assert limiter.note_rate_limited("groq") == DEFAULT_PARK_SECONDS


def test_a_wildly_long_retry_after_is_capped(limiter):
    """An hour-long Retry-After is a daily-quota answer wearing a rate-limit header.

    Writing a provider off for an hour on the strength of one response is a
    judgement the circuit breaker should make from a pattern, not one this
    should make from a header.
    """
    assert limiter.note_rate_limited("openrouter", "3600") == MAX_PARK_SECONDS


def test_a_park_never_shortens(limiter):
    """Two workers can each take a 429; the shorter one must not release early."""
    limiter.note_rate_limited("openrouter", "120")
    limiter.note_rate_limited("openrouter", "5")
    assert limiter.parked_for("openrouter") > 100


def test_a_park_expires_on_its_own(limiter, monkeypatch):
    limiter.note_rate_limited("openrouter", "10")
    monkeypatch.setattr(time, "monotonic", lambda: time.perf_counter() + 10_000)
    assert limiter.parked_for("openrouter") == 0.0


def test_a_served_request_clears_the_park(limiter):
    """A provider that just answered is demonstrably open, whatever it said before."""
    limiter.note_rate_limited("openrouter", "60")
    limiter.note_success("openrouter")
    assert limiter.parked_for("openrouter") == 0.0


async def test_a_parked_provider_declines_without_waiting(limiter, no_sleep):
    limiter.note_rate_limited("openrouter", "60")
    granted, wait = await limiter.acquire("openrouter", 20, max_wait=100.0)
    assert granted is False
    assert wait > 55
    # The park is the provider's own statement about the cap; our window is only
    # an estimate of it. We do not wait one out even with budget to spare.
    assert no_sleep == []


# ---------------------------------------------------------------------------
# Proactive pacing
# ---------------------------------------------------------------------------
async def test_requests_under_the_cap_are_granted_immediately(limiter, no_sleep):
    for _ in range(5):
        granted, wait = await limiter.acquire("groq", 5)
        assert granted is True
        assert wait == 0.0
    assert no_sleep == []


async def test_the_request_over_the_cap_declines_when_the_budget_is_zero(limiter):
    for _ in range(3):
        assert (await limiter.acquire("groq", 3))[0] is True
    granted, wait = await limiter.acquire("groq", 3)
    assert granted is False
    # Reported as "how long this provider would take", which is what lets the
    # chain choose which one to wait on if they all decline.
    assert 55 < wait <= 60


async def test_a_short_wait_within_budget_is_taken_not_declined(limiter, monkeypatch):
    """Under budget, waiting beats hopping: the slot is nearly free.

    The window is advanced during the fake sleep, which is what a real wait
    would have accomplished.
    """
    slept: list[float] = []
    base = time.monotonic()
    clock = {"now": base}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    async def _fake(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(rl, "_sleep", _fake)

    assert (await limiter.acquire("groq", 1))[0] is True
    clock["now"] = base + 58.0  # 2s left on the window

    granted, waited = await limiter.acquire("groq", 1, max_wait=5.0)
    assert granted is True
    assert slept and 1.5 < slept[0] <= 2.5
    assert waited == slept[0]


async def test_a_long_wait_over_budget_declines_so_the_chain_can_hop(limiter, no_sleep):
    """The whole premise: another provider is idle with an independent quota.

    Blocking a worker for the best part of a minute on the busy one converts a
    throughput problem into a latency problem and gains nothing.
    """
    assert (await limiter.acquire("openrouter", 1))[0] is True
    granted, wait = await limiter.acquire("openrouter", 1, max_wait=3.0)
    assert granted is False
    assert wait > 3.0
    assert no_sleep == []


async def test_pacing_is_per_provider(limiter, no_sleep):
    """An exhausted window on one provider says nothing about another's."""
    assert (await limiter.acquire("openrouter", 1))[0] is True
    assert (await limiter.acquire("openrouter", 1))[0] is False
    assert (await limiter.acquire("groq", 1))[0] is True


async def test_a_zero_cap_disables_pacing(limiter, no_sleep):
    """0 rpm is the escape hatch, not a limit of zero requests."""
    for _ in range(50):
        assert (await limiter.acquire("groq", 0))[0] is True


async def test_the_window_slides(limiter, monkeypatch):
    clock = {"now": time.monotonic()}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])

    assert (await limiter.acquire("groq", 2))[0] is True
    assert (await limiter.acquire("groq", 2))[0] is True
    assert (await limiter.acquire("groq", 2))[0] is False

    clock["now"] += 61.0
    assert (await limiter.acquire("groq", 2))[0] is True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def _settings(**overrides) -> Settings:
    kwargs = dict(secret_key="test-secret-key-not-for-production")
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_per_provider_rpm_is_read_from_the_spec():
    s = _settings(llm_provider_rpm="openrouter:20,gemini:15,groq:30", llm_default_rpm=7)
    assert provider_rpm(s, "openrouter") == 20
    assert provider_rpm(s, "gemini") == 15
    assert provider_rpm(s, "groq") == 30


def test_an_unlisted_provider_falls_back_to_the_default():
    s = _settings(llm_provider_rpm="openrouter:20", llm_default_rpm=7)
    assert provider_rpm(s, "cerebras") == 7


def test_an_unparseable_rpm_entry_falls_back_rather_than_raising():
    """A typo in an env var must not take the AI path down."""
    s = _settings(llm_provider_rpm="openrouter:lots", llm_default_rpm=7)
    assert provider_rpm(s, "openrouter") == 7


def test_the_shipped_default_covers_every_provider_in_the_shipped_chain():
    """A provider in the chain with no configured cap would pace on the default.

    Which is not wrong, but the caps differ by an order of magnitude between
    these four, so a default standing in for a real one is a silent mis-pace.
    """
    s = _settings()
    for name in s.llm_provider_chain.split(","):
        assert f"{name.strip()}:" in s.llm_provider_rpm


# ---------------------------------------------------------------------------
# Choosing who to wait on
# ---------------------------------------------------------------------------
def test_shortest_wait_picks_the_soonest_provider():
    assert shortest_wait({"openrouter": 40.0, "groq": 3.0, "gemini": 12.0}) == ("groq", 3.0)


def test_shortest_wait_on_nothing_is_not_an_error():
    assert shortest_wait({}) == (None, 0.0)


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------
async def test_the_snapshot_distinguishes_throttled_from_broken(limiter, no_sleep):
    """"Sent as templates" and "sent personalized but slowly" look identical outside."""
    await limiter.acquire("groq", 5)
    await limiter.acquire("openrouter", 1)
    await limiter.acquire("openrouter", 1)  # declined
    limiter.note_rate_limited("gemini", "30")

    snap = limiter.snapshot()
    assert snap["groq"]["requests_last_minute"] == 1
    assert snap["groq"]["parked"] is False
    assert snap["openrouter"]["deferred_requests"] == 1
    assert snap["gemini"]["parked"] is True
    assert snap["gemini"]["rate_limited_responses"] == 1
    assert snap["gemini"]["park_remaining_seconds"] <= 30


def test_the_shared_limiter_is_one_instance():
    """Module-level for the same reason the Apollo bucket is.

    A per-call instance resets its window on every use and limits nothing.
    """
    rl.reset_rate_limiter()
    assert rl.get_rate_limiter() is rl.get_rate_limiter()
