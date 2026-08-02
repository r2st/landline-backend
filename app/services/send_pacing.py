"""Minimum spacing between two sends from the same mailbox.

The warm-up ramp caps how much a mailbox sends *per day*. Nothing caps how fast
it sends them. The queue sweep runs every five minutes, finds every due
enrollment, and calls ``send_sequence_step.delay(...)`` for each one in a loop —
so N due enrollments become N Celery tasks dispatched in the same instant, and
with concurrent workers a single mailbox can put ten messages on the wire inside
one second.

A human with a Gmail account does not do that. Bulk-send detection at Google and
Microsoft keys on exactly this shape — burst, silence, burst — and the daily
volume that looks safe on a warm-up chart is the same volume that looks like a
script when it all leaves in four seconds.

So: each mailbox gets a next-allowed-send timestamp. A send that arrives early is
**deferred, not dropped** — ``send_sequence_step`` backs the enrollment off and
the next sweep picks it up, which is the same soft-skip path the warm-up cap and
the daily reservation already use.

Two design notes:

**Jitter is not decoration.** A fixed 90-second gap is itself a fingerprint —
sends landing at 09:00:00, 09:01:30, 09:03:00 are as machine-shaped as a burst,
just slower. The gap is drawn uniformly from ``[min_gap, min_gap + jitter]`` so
the inter-send distribution has no period to lock onto.

**Redis is the authority, in-process is the fallback.** The reservation is a
single ``SET key ts NX EX ttl``-style claim, so N workers asking at once produce
one winner. Without Redis the gate degrades to a per-process dict: still correct
for a single worker, weaker across a fleet, and never *looser* than no pacing at
all. That is the same trade ``SendLimiter.reserve_daily_send`` makes, for the
same reason — pacing that fails closed on a Redis blip would stop outreach
entirely, which is a worse outcome than a burst.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# Cached async Redis client, mirroring ``send_limiter``. Disabled on first
# failure so a persistent outage doesn't retry a dead socket on every send.
_redis = None
_redis_disabled = False

# Fallback when Redis is unreachable: account key → epoch seconds of the next
# allowed send. Per-process, so it only paces the worker that holds it.
_local_next_allowed: dict[str, float] = {}


async def _get_redis():
    global _redis, _redis_disabled
    if _redis_disabled:
        return None
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        return _redis
    except Exception as exc:  # noqa: BLE001 — any redis failure → local pacing
        logger.info("Send-pacing redis unavailable, pacing per-process only: %s", exc)
        _redis_disabled = True
        return None


def _reset_for_tests() -> None:
    """Drop the cached client and local state so a test can start clean."""
    global _redis, _redis_disabled
    _redis = None
    _redis_disabled = False
    _local_next_allowed.clear()


def _key(account_id) -> str:
    return f"dialtone:pace:{account_id}"


def _gap_seconds(settings=None) -> float:
    """The gap to impose after a send: ``min_gap`` plus uniform jitter."""
    settings = settings or get_settings()
    base = max(0, int(settings.send_min_gap_seconds))
    jitter = max(0, int(settings.send_gap_jitter_seconds))
    return base + (random.uniform(0, jitter) if jitter else 0)


class PacingDecision:
    """Whether this mailbox may send now, and how long to wait if not."""

    def __init__(self, allowed: bool, *, wait_seconds: float = 0.0, reason: str = "") -> None:
        self.allowed = allowed
        self.wait_seconds = max(0.0, wait_seconds)
        self.reason = reason

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PacingDecision allowed={self.allowed} wait={self.wait_seconds:.0f}s>"


async def claim_send_slot(account_id, *, now: Optional[float] = None) -> PacingDecision:
    """Claim this mailbox's next send slot.

    Returns ``allowed=True`` **and reserves the following gap** in one step, so
    two concurrent workers cannot both be told yes. On a refusal, ``wait_seconds``
    is how long until the mailbox is free — the caller defers the enrollment by
    at least that much.

    ``account_id`` of ``None`` (no resolved sender — the dispatcher will pick one
    later) is allowed through: pacing a mailbox we cannot name would either block
    every such send or pace them all against one shared key, and neither is
    better than the no-pacing status quo for that path.
    """
    settings = get_settings()
    if not settings.send_pacing_enabled or account_id is None:
        return PacingDecision(True, reason="pacing disabled")

    now = now if now is not None else time.time()
    gap = _gap_seconds(settings)
    key = _key(account_id)

    client = await _get_redis()
    if client is not None:
        try:
            # SET NX is the whole reservation: the key *existing* means the
            # mailbox is inside its cooling-off window, and its TTL is what's
            # left of that window. Whoever sets it wins; everyone else reads the
            # remaining TTL and waits. No read-then-write race to lose.
            claimed = await client.set(key, int(now), nx=True, ex=max(1, int(gap)))
            if claimed:
                return PacingDecision(True, reason=f"slot claimed, next in {gap:.0f}s")
            ttl = await client.ttl(key)
            wait = float(ttl) if ttl and ttl > 0 else 1.0
            return PacingDecision(
                False, wait_seconds=wait,
                reason=f"mailbox paced — {wait:.0f}s until its next send",
            )
        except Exception as exc:  # noqa: BLE001 — fall through to local pacing
            logger.warning("Send-pacing redis error, pacing per-process: %s", exc)

    # --- Degraded: per-process pacing ---
    next_allowed = _local_next_allowed.get(key, 0.0)
    if now < next_allowed:
        return PacingDecision(
            False, wait_seconds=next_allowed - now,
            reason=(
                f"mailbox paced — {next_allowed - now:.0f}s until its next send "
                "[degraded: redis unavailable, per-worker pacing only]"
            ),
        )
    _local_next_allowed[key] = now + gap
    return PacingDecision(True, reason=f"slot claimed, next in {gap:.0f}s [degraded]")


async def release_send_slot(account_id) -> None:
    """Hand back a claimed slot when the send didn't happen.

    Called on the paths that claim a slot and then bail (a soft skip, a
    dispatcher exception). Without it a mailbox that declines to send still
    burns its own spacing window, which turns a transient SMTP failure into a
    minute of enforced silence for every enrollment behind it.
    """
    if account_id is None:
        return
    key = _key(account_id)
    client = await _get_redis()
    if client is not None:
        try:
            await client.delete(key)
            return
        except Exception as exc:  # noqa: BLE001
            logger.debug("Send-pacing release failed: %s", exc)
    _local_next_allowed.pop(key, None)
