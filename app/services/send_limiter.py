"""Send rate limiter — prevents spam by enforcing sending limits.

**The daily cap is enforced by an atomic Redis reservation, not by counting
rows.** ``can_send_today`` reads ``COUNT(*)`` of today's sends and compares it to
the limit; between that read and the ``OutreachEvent`` insert there is a window
several seconds wide (recipient validation, Hunter verification, AI composition,
the SMTP round-trip). Every concurrent worker reads the same pre-send count, so
N workers each see "499/500, fine" and 500+N emails go out. At the volumes that
matter — a queue sweep dispatching dozens of ``send_sequence_step`` tasks at once
across the worker pool — that overshoot is exactly the thing the cap exists to
prevent.

:func:`reserve_daily_send` closes the window: one ``INCR`` per intended send,
checked against the limit *before* the send, with :func:`release_daily_send`
handing the slot back when the send doesn't happen. The counter is seeded from
the DB on first use each day (``SET … NX``) so a worker restart, a Redis
eviction, or a mid-day deploy resumes from the real count rather than zero.

When Redis is unreachable the reservation degrades to the row-count check — the
same per-worker enforcement as before, never *more* permissive — and says so in
the returned reason so the degradation is visible rather than silent.
"""

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import get_settings
from app.models.outreach import OutreachEvent, OutreachEnrollment
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.org_context import get_current_org_id

logger = logging.getLogger(__name__)


def _as_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC.

    Postgres hands back tz-aware values for these columns; SQLite (the test
    engine) drops the offset. Subtracting a naive from an aware datetime raises,
    so normalise before any arithmetic.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

# Lazily-created, cached async Redis client (mirrors app.services.api_key_auth).
# Disabled on first failure so a persistent outage doesn't retry a dead socket
# on every send.
_redis = None
_redis_disabled = False


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
    except Exception as exc:  # noqa: BLE001 — any redis failure → DB-count fallback
        logger.info("Send-limit redis unavailable, falling back to row counts: %s", exc)
        _redis_disabled = True
        return None


def _reset_redis_for_tests() -> None:
    """Drop the cached client so a test can swap in its own fake."""
    global _redis, _redis_disabled
    _redis = None
    _redis_disabled = False


def _daily_key(now: Optional[datetime] = None) -> str:
    """Per-org, per-UTC-day counter key.

    Org-scoped because the daily cap is a tenant's cap: one busy org must not be
    able to spend another's allowance.
    """
    now = now or datetime.now(timezone.utc)
    return f"dialtone:daily_sends:{get_current_org_id() or 'global'}:{now:%Y-%m-%d}"


def _seconds_until_utc_midnight(now: Optional[datetime] = None) -> int:
    """TTL for the daily key — the key expires when the day it counts ends."""
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(60, int((tomorrow - now).total_seconds()) + 300)


class SendLimiter:
    """Enforces email sending limits to prevent spam flagging."""

    def __init__(self):
        self.settings = get_settings()

    async def can_send_to_contact(self, db: AsyncSession, contact_email: str) -> tuple[bool, str]:
        """Check if we can send to this contact. Returns (allowed, reason)."""
        now = datetime.now(timezone.utc)

        # Check monthly cap per contact
        thirty_days_ago = now - timedelta(days=30)
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(Contact.email == contact_email)
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= thirty_days_ago)
        )
        monthly_count = result.scalar() or 0
        if monthly_count >= self.settings.max_emails_per_contact_per_month:
            return False, f"Monthly limit reached for {contact_email} ({monthly_count}/{self.settings.max_emails_per_contact_per_month})"

        # Check minimum gap between emails to same contact
        result = await db.execute(
            select(OutreachEvent.occurred_at)
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(Contact.email == contact_email)
            .where(OutreachEvent.event_type == "sent")
            .order_by(OutreachEvent.occurred_at.desc())
            .limit(1)
        )
        last_sent = result.scalar_one_or_none()
        if last_sent:
            min_gap = timedelta(days=self.settings.min_days_between_followups)
            if now - last_sent < min_gap:
                days_remaining = (min_gap - (now - last_sent)).days + 1
                return False, f"Too soon to email {contact_email} again (wait {days_remaining} more days)"

        return True, "OK"

    async def can_send_to_company(
        self,
        db: AsyncSession,
        company_id,
        *,
        exclude_contact_id=None,
        now: Optional[datetime] = None,
    ) -> tuple[bool, str, timedelta]:
        """Whether we may mail *another* person at this company yet.

        Returns ``(allowed, reason, wait)`` — ``wait`` is how long remains on the
        gap, so the caller can defer by the real amount instead of re-checking
        hourly.

        **Why this is not covered by the per-contact caps.**
        ``can_send_to_contact`` is keyed on an email address. ``info@acme.com``,
        ``sales@acme.com``, ``jane@acme.com`` and ``bob@acme.com`` are four
        different addresses and one buyer, so all four pass a per-address gate
        while the recipient's mail server sees a burst from an unknown sender —
        the canonical spam pattern, and the single fastest way to burn a sending
        domain. It reads as a blast to the humans too: colleagues compare notes,
        and near-identical cold copy arriving to four of them the same minute
        destroys the credibility the copy was written to build.

        That fan-out is reachable today. ``signal_enrollment`` enrols *every*
        un-enrolled emailable contact at a company on one signal and dispatches
        each immediately, and it fires on the strongest buying signals the system
        detects — so the failure lands hardest on the accounts most worth
        winning.

        **The current contact is excluded.** A follow-up to someone we are
        already sequencing is not fan-out; its spacing belongs to
        ``step_delay``/``can_send_to_contact``. Only *other* people at the
        company gate this send.

        **Both send tables are read.** Sequence sends write ``OutreachEvent``
        and nothing else; manual/template sends write ``EmailSend``. Reading one
        would make the gate blind to whichever path sent last — the same split
        that made the unified inbox blind to every sequence send.
        """
        now = now or datetime.now(timezone.utc)
        zero = timedelta(0)
        if company_id is None:
            # A contact with no company cannot collide with a colleague.
            return True, "OK", zero

        min_gap = timedelta(hours=self.settings.min_hours_between_company_sends)
        if min_gap <= zero:
            return True, "OK", zero

        window_start = now - min_gap

        # Colleagues: every other contact at this company.
        peers = select(Contact.id).where(Contact.company_id == company_id)
        if exclude_contact_id is not None:
            peers = peers.where(Contact.id != exclude_contact_id)

        # Sequence sends — OutreachEvent → enrollment → contact.
        seq_last = (
            await db.execute(
                select(func.max(OutreachEvent.occurred_at))
                .join(
                    OutreachEnrollment,
                    OutreachEvent.enrollment_id == OutreachEnrollment.id,
                )
                .where(
                    OutreachEnrollment.contact_id.in_(peers),
                    OutreachEvent.event_type == "sent",
                    OutreachEvent.occurred_at >= window_start,
                )
            )
        ).scalar()

        # Manual / template sends — EmailSend → contact.
        direct_last = (
            await db.execute(
                select(func.max(EmailSend.sent_at)).where(
                    EmailSend.contact_id.in_(peers),
                    EmailSend.status == "sent",
                    EmailSend.sent_at >= window_start,
                )
            )
        ).scalar()

        candidates = [t for t in (seq_last, direct_last) if t is not None]
        if not candidates:
            return True, "OK", zero

        last = max(_as_utc(t) for t in candidates)
        elapsed = now - last
        if elapsed >= min_gap:
            return True, "OK", zero

        wait = min_gap - elapsed
        hours = max(1, int(wait.total_seconds() // 3600) + 1)
        return (
            False,
            (
                f"A colleague at this company was emailed "
                f"{int(elapsed.total_seconds() // 3600)}h ago "
                f"(wait ~{hours}h)"
            ),
            wait,
        )

    def within_send_window(self, tz_name: Optional[str] = None) -> tuple[bool, str]:
        """Whether the current time is inside the configured send window.

        Cold email that arrives at 3 AM (or Sunday) reads as a bot and depresses
        reply rates. We restrict sends to business hours on weekdays. When
        ``tz_name`` is given (a contact's own IANA timezone) the window is
        evaluated in THAT timezone so a lead in Los Angeles isn't emailed at
        5 AM their time; otherwise the global default timezone is used. An
        unknown/invalid tz falls back to the global default. Returns
        (allowed, reason).
        """
        if not self.settings.send_window_enabled:
            return True, "send window disabled"

        from zoneinfo import ZoneInfo

        tz = None
        used_tz_name = tz_name or self.settings.send_window_timezone
        if tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = None  # bad contact tz → fall back to global default below
        if tz is None:
            used_tz_name = self.settings.send_window_timezone
            try:
                tz = ZoneInfo(self.settings.send_window_timezone)
            except Exception:
                # Unknown tz name → fail open rather than block all sending.
                return True, "send window tz unavailable"

        now_local = datetime.now(tz)
        if self.settings.send_window_weekdays_only and now_local.weekday() >= 5:
            return False, f"outside send window (weekend, {now_local:%a %H:%M} {used_tz_name})"

        start = self.settings.send_window_start_hour
        end = self.settings.send_window_end_hour
        if not (start <= now_local.hour < end):
            return False, (
                f"outside send window ({now_local:%H:%M} not in "
                f"{start:02d}:00–{end:02d}:00 {used_tz_name})"
            )
        return True, f"within send window ({now_local:%a %H:%M} {used_tz_name})"

    async def can_send_today(self, db: AsyncSession) -> tuple[bool, str]:
        """Whether today's recorded sends are still under the daily limit.

        An *advisory* pre-check, safe for dispatchers and dashboards: it counts
        rows already written, so concurrent in-flight sends are invisible to it.
        The send path must claim its slot with :meth:`reserve_daily_send`, which
        is the only check that holds under concurrency.
        """
        today_count = await self._db_sent_today(db)

        daily_limit = await self._get_daily_limit_db(db)

        if today_count >= daily_limit:
            warmup_note = " Warmup mode active." if self.settings.warmup_enabled else ""
            return False, f"Daily limit reached ({today_count}/{daily_limit}).{warmup_note}"

        return True, f"OK ({today_count}/{daily_limit} sent today)"

    async def daily_capacity(
        self, db: AsyncSession, *, now: Optional[datetime] = None
    ) -> tuple[int, int]:
        """Today's ``(limit, sent_so_far)`` — the two numbers behind the verdict.

        :meth:`can_send_today` answers "may I send", which is the only question
        the dispatcher has. A reporting surface needs the arithmetic instead: how
        much capacity exists today, and how much of it is gone. Same warm-up-aware
        limit and same advisory row count, without parsing them back out of a
        sentence.
        """
        return (
            await self._get_daily_limit_db(db),
            await self._db_sent_today(db, now),
        )

    async def _db_sent_today(self, db: AsyncSession, now: Optional[datetime] = None) -> int:
        """Sends recorded in the current UTC day (the counter's seed value)."""
        now = now or datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= today_start)
        )
        return result.scalar() or 0

    async def reserve_daily_send(
        self, db: AsyncSession, *, now: Optional[datetime] = None
    ) -> tuple[bool, str]:
        """Atomically claim one slot against today's send limit.

        Call this immediately before putting a message on the wire, and hand the
        slot back with :meth:`release_daily_send` if the send doesn't happen. The
        claim is a single Redis ``INCR``, so N concurrent workers get N distinct
        numbers and only the ones at or below the limit proceed — unlike
        :meth:`can_send_today`, where every worker reads the same stale count and
        they all think they're the last one under the cap.

        The counter is seeded from ``COUNT(*)`` of today's sends on first use
        (``SET … NX``, so exactly one worker's seed wins), which makes a restart,
        an eviction, or a deploy resume from the real number instead of zero.

        Returns ``(allowed, reason)``. With Redis down it falls back to the
        row-count check — per-worker enforcement, identical to the pre-Redis
        behaviour, and the reason says so.
        """
        now = now or datetime.now(timezone.utc)
        limit = await self._get_daily_limit_db(db)

        client = await _get_redis()
        if client is None:
            allowed, reason = await self.can_send_today(db)
            return allowed, f"{reason} [degraded: no redis, per-worker count]"

        key = _daily_key(now)
        try:
            # Seed from the DB only when this is the first send of the day (or
            # the key was lost). NX makes the seed race-safe: the loser's SET is
            # a no-op and both workers then INCR the same seeded counter.
            seeded = await client.set(
                key, await self._db_sent_today(db, now),
                nx=True, ex=_seconds_until_utc_midnight(now),
            )
            if seeded:
                logger.info("Seeded daily send counter %s from DB", key)
            count = int(await client.incr(key))
            # Belt and braces: a key that somehow lost its TTL would otherwise
            # freeze today's count into tomorrow.
            if await client.ttl(key) < 0:
                await client.expire(key, _seconds_until_utc_midnight(now))
        except Exception as exc:  # noqa: BLE001 — any redis failure → DB fallback
            logger.warning("Daily send reservation failed (%s); using row count", exc)
            allowed, reason = await self.can_send_today(db)
            return allowed, f"{reason} [degraded: redis error, per-worker count]"

        if count > limit:
            # Give the slot straight back — this worker isn't sending.
            try:
                await client.decr(key)
            except Exception:  # noqa: BLE001 — best effort; TTL bounds the damage
                logger.debug("Could not release over-limit reservation on %s", key)
            warmup_note = " Warmup mode active." if self.settings.warmup_enabled else ""
            return False, f"Daily limit reached ({limit}/{limit}).{warmup_note}"

        return True, f"OK (reserved {count}/{limit} today)"

    async def release_daily_send(self, *, now: Optional[datetime] = None) -> None:
        """Return an unused reservation to today's pool.

        Called when a claimed send is declined downstream (warm-up cap, kill
        switch, dispatcher error). Never raises and never drops the counter below
        zero, so a double release can't manufacture extra allowance.
        """
        client = await _get_redis()
        if client is None:
            return
        key = _daily_key(now)
        try:
            remaining = int(await client.decr(key))
            if remaining < 0:
                await client.set(key, 0, ex=_seconds_until_utc_midnight(now))
        except Exception as exc:  # noqa: BLE001 — release is best-effort
            logger.debug("Could not release daily send reservation: %s", exc)

    def _daily_limit_for_start(self, start_date: Optional[str]) -> int:
        """Daily limit for a given warm-up start date (ISO string or None)."""
        if not self.settings.warmup_enabled or not start_date:
            return self.settings.max_emails_per_day
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            days_active = (datetime.now(timezone.utc) - start).days
            if days_active < 7:
                return self.settings.warmup_daily_limit_week1
            elif days_active < 14:
                return self.settings.warmup_daily_limit_week2
            elif days_active < 21:
                return self.settings.warmup_daily_limit_week3
            return self.settings.max_emails_per_day
        except (ValueError, TypeError):
            return self.settings.max_emails_per_day

    def _get_daily_limit(self) -> int:
        """Config-only daily limit (no DB). Used where no session is available."""
        return self._daily_limit_for_start(self.settings.warmup_start_date)

    async def _get_daily_limit_db(self, db: AsyncSession) -> int:
        """Daily limit using the persisted (system_settings) warm-up start date.

        Prefers the warm-up start stamped on first send / reset from the UI over
        the static config value, so a stale env date can't keep the ramp pinned.
        """
        from app.services import system_settings

        start_date = await system_settings.get_warmup_start_date(db)
        return self._daily_limit_for_start(start_date)

    async def get_send_stats(self, db: AsyncSession) -> dict:
        """Get current sending statistics for display."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        thirty_days_ago = now - timedelta(days=30)

        # Today's sends
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= today_start)
        )
        today_count = result.scalar() or 0

        # 30-day sends
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= thirty_days_ago)
        )
        monthly_count = result.scalar() or 0

        from app.services import system_settings

        warmup_start = await system_settings.get_warmup_start_date(db)
        daily_limit = self._daily_limit_for_start(warmup_start)

        return {
            "today_sent": today_count,
            "daily_limit": daily_limit,
            "daily_remaining": max(0, daily_limit - today_count),
            "monthly_sent": monthly_count,
            "warmup_active": self.settings.warmup_enabled and bool(warmup_start),
            "warmup_week": self._get_warmup_week(warmup_start),
            "max_per_contact_monthly": self.settings.max_emails_per_contact_per_month,
            "min_days_between_followups": self.settings.min_days_between_followups,
        }

    def account_warmup_limit(self, account) -> int | None:
        """Per-account warm-up cap based on how long ago the account connected.

        A freshly connected mailbox ramps its own volume (week1 → week2 → week3
        → uncapped) independent of the global ``warmup_start_date``, so adding a
        new sender never dumps full volume from a cold IP/mailbox. Returns
        ``None`` when per-account warm-up is disabled or the account is old
        enough to be fully warmed.
        """
        s = self.settings
        if not s.account_warmup_enabled:
            return None
        connected = getattr(account, "connected_at", None)
        try:
            if connected is None:
                return None
            if connected.tzinfo is None:
                connected = connected.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - connected).days
        except (TypeError, AttributeError):
            # Non-datetime (e.g. a bare mock) → no warm-up cap.
            return None
        if days < 7:
            return s.account_warmup_daily_limit_week1
        if days < 14:
            return s.account_warmup_daily_limit_week2
        if days < 21:
            return s.account_warmup_daily_limit_week3
        return None  # fully warmed

    async def account_capacity(self, db: AsyncSession, account) -> dict:
        """Deliverability-aware daily capacity for a single sending account.

        The static daily cap is (a) scaled by the account's ``throttle_factor``
        from its ``SenderHealth`` row (feature 3.3; 1.0 when unknown) and (b)
        capped by a per-account **warm-up** ramp keyed off ``connected_at``. The
        effective limit is the lower of the two, so a struggling *or* brand-new
        account sends less and volume shifts to healthy, warmed accounts.
        """
        from app.models.sender_health import SenderHealth

        transport = "smtp" if type(account).__name__ == "SmtpAccount" else "gmail"
        row = (
            await db.execute(
                select(SenderHealth).where(
                    SenderHealth.account_type == transport,
                    SenderHealth.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        throttle = row.throttle_factor if row is not None else 1.0
        base = self.settings.max_emails_per_day
        effective = max(1, round(base * throttle))

        # Warm-up cap: a persisted SenderWarmupPlan (Feature 1) is authoritative
        # for the account when present; otherwise fall back to the config-only
        # per-account ramp keyed off connected_at.
        from app.services import warmup

        warmup_limit = await warmup.plan_daily_limit(db, account)
        if warmup_limit is None:
            warmup_limit = self.account_warmup_limit(account)
        if warmup_limit is not None:
            effective = min(effective, warmup_limit)

        used = account.daily_send_count or 0
        remaining = max(0, effective - used)
        return {
            "throttle_factor": throttle,
            "reputation_score": row.reputation_score if row is not None else None,
            "health_status": row.status if row is not None else "unknown",
            "base_daily_limit": base,
            "warmup_daily_limit": warmup_limit,
            "effective_daily_limit": effective,
            "used_today": used,
            "remaining_today": remaining,
            "can_send": remaining > 0 and (row is None or row.status != "critical"),
        }

    def _get_warmup_week(self, start_date: Optional[str] = None) -> int | None:
        start_date = start_date if start_date is not None else self.settings.warmup_start_date
        if not self.settings.warmup_enabled or not start_date:
            return None
        try:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - start).days
            return min(days // 7 + 1, 4)  # Cap at week 4 (warmup complete)
        except (ValueError, TypeError):
            return None


def get_send_limiter() -> SendLimiter:
    return SendLimiter()
