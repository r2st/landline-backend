"""Pace LLM requests per provider, and believe a provider when it says "later".

Why this is separate from the circuit breaker
---------------------------------------------
``llm_providers.CircuitBreaker`` answers "is this provider broken?" — three
consecutive failures and it steps aside for five minutes. That is the right
instrument for an outage and the wrong one for a rate limit, in both directions:

* **It reacts too late.** A free-tier provider caps requests per *minute*
  (OpenRouter free is ~20/min, Gemini Flash free ~15/min). A batch of enrollments
  coming due in the same sweep fires composition calls as fast as the worker can
  issue them, so the cap is hit on the third or fourth email — and then the
  breaker needs three *more* failures before it stops. Every one of those is a
  real HTTP round trip spent to be told no.
* **It reacts too hard.** A 429 with ``Retry-After: 30`` is not an outage; it is
  a queue with a stated wait. Treating it as a failure either blackholes a
  healthy provider for five minutes or, worse, does not blackhole it at all and
  the next email in the batch pays the same round trip to learn the same thing.

So this module does two things the breaker cannot:

1. **Pace proactively** — a sliding window per provider, so we stay under the
   cap rather than discovering it. Same shape as the Apollo and Places buckets
   (``enrichment._SlidingWindowLimiter``), one window per provider name.
2. **Honour ``Retry-After``** — a 429 parks that provider for exactly as long as
   it asked, and every subsequent call in the batch skips it instantly instead of
   re-earning the same 429.

Waiting is a routing decision, not a stall
------------------------------------------
The important design choice is what to do when the window is full. The obvious
answer — sleep until a slot frees — is wrong here, because this chain's whole
premise is that **another provider is sitting idle with an independent quota.**
Blocking a Celery worker for 40 seconds waiting on OpenRouter, while Groq would
have answered in 300ms, converts a throughput problem into a latency problem and
gains nothing.

So :func:`acquire` takes a wait *budget*. Under budget, it waits and returns the
slot. Over budget, it declines, and the caller moves to the next provider — a
rate-limited provider is skipped the way an unconfigured one is, not failed the
way a broken one is. It never counts as a failure and never touches the breaker,
because being early is not being broken.

The one case that would be wrong to skip
----------------------------------------
If *every* provider is paced out at once, declining all of them would raise
``LLMChainExhausted`` and the caller would degrade to static mail-merge copy —
losing the personalization this whole subsystem exists to produce, because we
were two seconds early. :func:`shortest_wait` exists for that: the chain, having
found no provider it could use *now*, waits out the soonest one and tries again.
Degrading to a template is the right answer when the providers are down. It is
the wrong answer when they are merely busy.

State is per-process, like the breaker: the API and each Celery worker keep their
own window. That under-counts against a shared account cap when several workers
run at once, which is why the ``Retry-After`` half matters — the reactive half
catches what the proactive half, without a shared store, cannot.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Ceiling on a parked window, however long the provider asked for. A
# ``Retry-After`` of an hour is a daily-quota answer wearing a rate-limit
# header, and parking a provider for an hour on one response is a decision the
# breaker should be making from a pattern, not one header. Past this we park for
# the ceiling and let the breaker judge the provider on its next real failure.
MAX_PARK_SECONDS = 300.0

# Fallback park when a 429 arrives with no ``Retry-After`` (or an unparseable
# one). Long enough that the next email in a batch does not immediately re-earn
# the same 429, short enough not to write off a provider over one burst.
DEFAULT_PARK_SECONDS = 20.0


async def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralize waiting without a real clock."""
    await asyncio.sleep(seconds)


async def wait(seconds: float) -> None:
    """Public wrapper over :func:`_sleep`, for callers outside this module.

    Routed through the same indirection so a test that neutralizes waiting here
    neutralizes it everywhere, rather than only for the pacing this module does
    itself.
    """
    if seconds > 0:
        await _sleep(seconds)


def parse_retry_after(value: Any) -> Optional[float]:
    """Seconds from a ``Retry-After`` header value, or ``None``.

    Handles the delta-seconds form (``"30"``, ``"30.5"``) and the HTTP-date form
    (``"Wed, 21 Oct 2026 07:28:00 GMT"``), which the RFC allows and some
    gateways in front of these APIs actually send. A date already in the past
    yields ``0.0`` — "you may retry now" — not a negative park.

    Returns ``None`` for anything unparseable, so the caller can fall back to
    :data:`DEFAULT_PARK_SECONDS` rather than trusting a garbage number.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone

        when = parsedate_to_datetime(text)
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:  # noqa: BLE001 — a bad header must not break a send
        return None


class ProviderRateLimiter:
    """Per-provider sliding windows plus ``Retry-After`` parking.

    One instance holds every provider's state, keyed by provider name. Thread
    safe: the API process and each Celery worker are multi-threaded, and two
    threads racing the same window would both see a free slot and both take it —
    which is precisely the burst the window exists to prevent.
    """

    def __init__(self) -> None:
        self._windows: dict[str, deque[float]] = {}
        self._parked_until: dict[str, float] = {}
        self._rate_limited: dict[str, int] = {}
        self._deferred: dict[str, int] = {}
        self._lock = threading.Lock()

    # -- state ------------------------------------------------------------
    def reset(self) -> None:
        """Drop all state. Tests only; never called on a production path."""
        with self._lock:
            self._windows.clear()
            self._parked_until.clear()
            self._rate_limited.clear()
            self._deferred.clear()

    def parked_for(self, provider: str) -> float:
        """Seconds until ``provider`` is usable again; ``0.0`` when it is now.

        Self-healing, like the breaker's cooldown: an elapsed park is cleared on
        read, so nothing has to sweep it.
        """
        with self._lock:
            until = self._parked_until.get(provider)
            if until is None:
                return 0.0
            remaining = until - time.monotonic()
            if remaining <= 0:
                self._parked_until.pop(provider, None)
                return 0.0
            return remaining

    def note_rate_limited(self, provider: str, retry_after: Any = None) -> float:
        """Park ``provider`` after a 429. Returns the park length in seconds.

        Takes the raw header value rather than a number so every caller parses
        it the same way — including the HTTP-date form, which is easy to write a
        second, subtly wrong parser for.
        """
        seconds = parse_retry_after(retry_after)
        if seconds is None:
            seconds = DEFAULT_PARK_SECONDS
        seconds = min(MAX_PARK_SECONDS, max(0.0, seconds))

        with self._lock:
            self._rate_limited[provider] = self._rate_limited.get(provider, 0) + 1
            if seconds > 0:
                until = time.monotonic() + seconds
                # Never shorten an existing park: two concurrent workers can each
                # take a 429, and the second response arriving with a smaller
                # Retry-After must not release the provider early.
                self._parked_until[provider] = max(
                    self._parked_until.get(provider, 0.0), until
                )
        if seconds > 0:
            logger.warning(
                "LLM provider %s rate limited — parking it for %.0fs", provider, seconds
            )
        return seconds

    def note_success(self, provider: str) -> None:
        """A served request clears any park: the provider is demonstrably open.

        This is what makes a park safe next to a sibling-model fallback. When
        OpenRouter's free model 429s and its paid model answers on the same call,
        the park set by that 429 would otherwise skip the whole provider next
        time — costing us a working paid model for the sake of a limit that only
        binds the free one. A provider that just served us is not rate limited,
        whatever one of its models said.
        """
        with self._lock:
            self._parked_until.pop(provider, None)

    # -- pacing -----------------------------------------------------------
    def _slot_wait(self, provider: str, per_minute: int) -> float:
        """Seconds until a window slot frees, taking it immediately if free.

        Does the check and the take under one lock, so two threads cannot both
        observe the same free slot.
        """
        with self._lock:
            window = self._windows.setdefault(provider, deque())
            now = time.monotonic()
            while window and (now - window[0]) >= 60.0:
                window.popleft()
            if len(window) < per_minute:
                window.append(now)
                return 0.0
            return max(0.0, 60.0 - (now - window[0]))

    async def acquire(
        self, provider: str, per_minute: int, *, max_wait: float = 0.0
    ) -> tuple[bool, float]:
        """Try to take a slot for ``provider``. Returns ``(granted, wait_needed)``.

        ``per_minute <= 0`` disables pacing for that provider and always grants.

        When a slot is not free, ``max_wait`` decides between waiting and
        declining. Declining is not an error — ``wait_needed`` tells the caller
        how long this provider would have taken, which is what
        :func:`shortest_wait` needs to pick one to wait on if the whole chain
        declines.

        A parked provider (a 429 we were told to respect) declines outright: the
        window is our estimate of the cap, the park is the provider's own
        statement about it, and its statement wins.
        """
        if per_minute <= 0:
            return True, 0.0

        parked = self.parked_for(provider)
        if parked > 0:
            self._note_deferred(provider)
            return False, parked

        wait = self._slot_wait(provider, per_minute)
        if wait <= 0:
            return True, 0.0

        if wait <= max_wait:
            logger.debug(
                "Pacing %s — waiting %.1fs for a rate-limit slot", provider, wait
            )
            await _sleep(wait)
            # Re-check rather than assume: another thread may have taken the slot
            # that freed while we slept. A second full wait would exceed the
            # budget the caller set, so a miss here declines.
            second = self._slot_wait(provider, per_minute)
            if second <= 0:
                return True, wait
            self._note_deferred(provider)
            return False, second

        self._note_deferred(provider)
        return False, wait

    def _note_deferred(self, provider: str) -> None:
        with self._lock:
            self._deferred[provider] = self._deferred.get(provider, 0) + 1

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Per-provider pacing state, for ``GET /api/v1/ai/providers``.

        Included in the monitoring surface for the same reason the breaker is:
        "the emails went out as templates" and "the emails went out personalized
        but slowly" look identical from the outside, and the difference is
        entirely in here.
        """
        now = time.monotonic()
        with self._lock:
            names = (
                set(self._windows) | set(self._parked_until)
                | set(self._rate_limited) | set(self._deferred)
            )
            out: dict[str, dict[str, Any]] = {}
            for name in sorted(names):
                window = self._windows.get(name) or deque()
                recent = sum(1 for t in window if (now - t) < 60.0)
                until = self._parked_until.get(name)
                remaining = max(0.0, until - now) if until is not None else 0.0
                out[name] = {
                    "requests_last_minute": recent,
                    "parked": remaining > 0,
                    "park_remaining_seconds": round(remaining, 1) if remaining else None,
                    "rate_limited_responses": self._rate_limited.get(name, 0),
                    "deferred_requests": self._deferred.get(name, 0),
                }
            return out


_limiter: ProviderRateLimiter | None = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> ProviderRateLimiter:
    """The process-wide limiter.

    Module-level for the same reason the Apollo bucket is: a per-call instance
    resets its window on every use and limits nothing.
    """
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = ProviderRateLimiter()
        return _limiter


def reset_rate_limiter() -> None:
    """Drop the process-wide limiter. Test-only helper."""
    global _limiter
    with _limiter_lock:
        _limiter = None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def provider_rpm(settings: Any, provider: str) -> int:
    """Requests-per-minute cap configured for ``provider``.

    Read from ``llm_provider_rpm``, a ``"name:rpm"`` comma list, falling back to
    ``llm_default_rpm``. Per-provider rather than one global number because the
    caps differ by an order of magnitude — Gemini Flash free is ~15/min while
    Groq is ~30 with a far larger daily allowance — and a single number set low
    enough for the tightest provider throttles the chain to its worst hop, which
    is the opposite of why the chain exists.
    """
    default = int(getattr(settings, "llm_default_rpm", 0) or 0)
    raw = getattr(settings, "llm_provider_rpm", "") or ""
    for item in raw.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        name, _, value = item.partition(":")
        if name.strip().lower() != provider:
            continue
        try:
            return max(0, int(value.strip()))
        except ValueError:
            logger.warning(
                "Ignoring unparseable llm_provider_rpm entry %r — using default %d",
                item, default,
            )
            return default
    return default


def shortest_wait(waits: dict[str, float]) -> tuple[Optional[str], float]:
    """The provider that frees up soonest, from ``{provider: wait_seconds}``.

    Used when the chain found nothing it could use immediately. Returns
    ``(None, 0.0)`` for an empty mapping.
    """
    if not waits:
        return None, 0.0
    name = min(waits, key=lambda k: waits[k])
    return name, waits[name]
