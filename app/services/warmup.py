"""Automated email warm-up ramp for new sending accounts.

The ramp (spec):

    day 1–3   → 5/day
    day 4–7   → 10/day
    day 8–14  → 20/day
    day 15–21 → 35/day
    day 22–30 → 50/day
    day 31+   → graduated (the org's normal daily limit applies)

This module owns three responsibilities:

* :func:`limit_for_day` — the pure ramp function (day → (limit, status)).
* :func:`ensure_warmup_plan` — idempotently create a plan when a sender is added.
* :func:`check_send_allowed` / :func:`advance_all` — the runtime gate the send
  path calls before every send, and the daily beat task that steps each plan
  forward.

A plan supersedes the older config-only per-account warm-up in
:mod:`app.services.send_limiter` (``account_warmup_limit``): when a plan row
exists it is authoritative for that account.
"""

import logging
from datetime import date, datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.sender_warmup import SenderWarmupPlan

logger = logging.getLogger(__name__)

# (inclusive_last_day, daily_limit) steps of the ramp. The final "graduated"
# stage is handled separately since its limit is the org's normal cap.
_RAMP: tuple[tuple[int, int], ...] = (
    (3, 5),
    (7, 10),
    (14, 20),
    (21, 35),
    (30, 50),
)

# Day at which an account is considered fully warmed.
GRADUATION_DAY = 31


def org_normal_limit() -> int:
    """The daily cap a graduated account reverts to (the org's normal limit)."""
    return get_settings().max_emails_per_day


def limit_for_day(day: int, graduated_limit: Optional[int] = None) -> tuple[int, str]:
    """Ramp: map a 1-based warm-up day to ``(daily_limit, status)``.

    Days before 1 are clamped to day 1 (a plan can't be pre-start). Day 31+
    graduates to ``graduated_limit`` (defaults to the org's normal daily cap).
    """
    if graduated_limit is None:
        graduated_limit = org_normal_limit()
    d = max(1, int(day))
    for last_day, limit in _RAMP:
        if d <= last_day:
            return limit, "warming"
    return graduated_limit, "graduated"


def day_for(start: date, today: Optional[date] = None) -> int:
    """1-based day on the ramp for ``start`` as of ``today`` (day 1 == start)."""
    if today is None:
        today = datetime.now(timezone.utc).date()
    if isinstance(start, datetime):
        start = start.date()
    return max(1, (today - start).days + 1)


def _transport_for(account) -> str:
    """gmail | smtp for a sending-account object."""
    return "smtp" if type(account).__name__ == "SmtpAccount" else "gmail"


def used_today(account) -> int:
    """Today's send count for an account, honouring the UTC daily rollover.

    ``daily_send_count`` is only "today's" count when ``daily_count_reset_at`` is
    today; a stale reset stamp means the stored number is yesterday's and will be
    zeroed on the next send, so it reads as 0 for warm-up accounting.
    """
    reset_at = getattr(account, "daily_count_reset_at", None)
    if reset_at is not None and reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    today = datetime.now(timezone.utc).date()
    if reset_at is None or reset_at.date() < today:
        return 0
    try:
        return int(account.daily_send_count or 0)
    except (TypeError, ValueError):
        return 0


async def get_plan(
    db: AsyncSession, account_id: UUID, account_type: str
) -> Optional[SenderWarmupPlan]:
    """The warm-up plan for one account, or ``None`` if it has none."""
    return (
        await db.execute(
            select(SenderWarmupPlan).where(
                SenderWarmupPlan.account_type == account_type,
                SenderWarmupPlan.account_id == account_id,
            )
        )
    ).scalar_one_or_none()


async def ensure_warmup_plan(
    db: AsyncSession,
    *,
    account_id: UUID,
    account_type: str,
    account_email: Optional[str] = None,
    start: Optional[date] = None,
    org_id: Optional[UUID] = None,
) -> SenderWarmupPlan:
    """Create a warm-up plan for a sender if it doesn't already have one.

    Idempotent: returns the existing plan when one is present, so it is safe to
    call every time an account is (re)connected. ``start`` defaults to today, so
    a newly added mailbox begins on day 1 of the ramp.

    ``org_id`` is passed explicitly at account-creation time because that path
    may run without a bound org context (OAuth callback); when omitted the
    org-stamp hook fills it from context.
    """
    existing = await get_plan(db, account_id, account_type)
    if existing is not None:
        return existing

    if start is None:
        start = datetime.now(timezone.utc).date()
    day = day_for(start)
    limit, status = limit_for_day(day)

    plan = SenderWarmupPlan(
        account_id=account_id,
        account_type=account_type,
        account_email=account_email,
        start_date=start,
        current_day=day,
        daily_limit=limit,
        status=status,
    )
    if org_id is not None:
        plan.org_id = org_id
    db.add(plan)
    await db.flush()
    logger.info(
        "Created warm-up plan for %s:%s (day %d, limit %d, %s)",
        account_type, account_email or account_id, day, limit, status,
    )
    return plan


async def check_send_allowed(db: AsyncSession, account) -> tuple[bool, str]:
    """Whether ``account`` may send one more email right now under its warm-up plan.

    * No plan, or a graduated plan → allowed (warm-up doesn't constrain it).
    * ``paused`` plan → blocked (the operator is holding this sender).
    * ``warming`` plan → allowed only while today's sends are under the plan's
      ``daily_limit``.

    Returns ``(allowed, reason)``. Fail-open on any error so a warm-up hiccup
    never wedges sending.
    """
    try:
        plan = await get_plan(db, account.id, _transport_for(account))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("warm-up lookup failed for %s: %s", getattr(account, "email", "?"), exc)
        return True, "warm-up check unavailable"

    if plan is None or plan.status == "graduated":
        return True, "no active warm-up limit"
    if plan.status == "paused":
        return False, "warm-up paused for this sender"

    sent = used_today(account)
    if sent >= plan.daily_limit:
        return False, (
            f"warm-up daily limit reached "
            f"(day {plan.current_day}: {sent}/{plan.daily_limit})"
        )
    return True, f"warm-up day {plan.current_day}: {sent}/{plan.daily_limit}"


async def plan_daily_limit(db: AsyncSession, account) -> Optional[int]:
    """The warm-up daily cap for ``account`` from its plan, or ``None``.

    ``None`` means "warm-up doesn't cap this account" (no plan, or graduated), so
    callers fall back to their own limit. A ``paused`` plan reports 0 (send
    nothing).
    """
    plan = await get_plan(db, account.id, _transport_for(account))
    if plan is None or plan.status == "graduated":
        return None
    if plan.status == "paused":
        return 0
    return plan.daily_limit


def recompute(plan: SenderWarmupPlan, today: Optional[date] = None) -> bool:
    """Step a plan forward to ``today``. Returns True if anything changed.

    Recomputes ``current_day`` from ``start_date`` and re-derives the ramp limit.
    A ``paused`` plan is left untouched (its day/limit are frozen until resumed).
    """
    if plan.status == "paused":
        return False
    day = day_for(plan.start_date, today)
    limit, status = limit_for_day(day)
    changed = (
        plan.current_day != day
        or plan.daily_limit != limit
        or plan.status != status
    )
    plan.current_day = day
    plan.daily_limit = limit
    plan.status = status
    return changed


async def backfill_warmup_plans(db: AsyncSession) -> dict:
    """Create a warm-up plan for every connected sender that lacks one.

    ``ensure_warmup_plan`` is wired into the Gmail/SMTP connect paths, so *new*
    accounts get a plan. But accounts connected before that wiring existed have
    none — which is why ``sender_warmup_plans`` sat empty — so a mailbox that
    should be ramping instead sends at full volume. This closes that gap
    idempotently: it is safe to run on every warm-up beat and only ever adds the
    plans that are missing.

    Existing accounts start their ramp *today* (day 1), not back-dated to when
    they were connected: back-dating an already-active sender to a graduated day
    would hand it full volume immediately, defeating the point of a ramp.
    """
    from app.models.gmail_account import GmailAccount
    from app.models.smtp_account import SmtpAccount

    created = 0

    gmail_accounts = (
        await db.execute(
            select(GmailAccount).where(GmailAccount.status == "connected")
        )
    ).scalars().all()
    for account in gmail_accounts:
        if await get_plan(db, account.id, "gmail") is None:
            await ensure_warmup_plan(
                db,
                account_id=account.id,
                account_type="gmail",
                account_email=account.email,
                org_id=getattr(account, "org_id", None),
            )
            created += 1

    smtp_accounts = (
        await db.execute(
            select(SmtpAccount).where(SmtpAccount.status == "connected")
        )
    ).scalars().all()
    for account in smtp_accounts:
        if await get_plan(db, account.id, "smtp") is None:
            await ensure_warmup_plan(
                db,
                account_id=account.id,
                account_type="smtp",
                account_email=account.email,
                org_id=getattr(account, "org_id", None),
            )
            created += 1

    if created:
        await db.commit()
    return {
        "gmail_accounts": len(gmail_accounts),
        "smtp_accounts": len(smtp_accounts),
        "plans_created": created,
    }


async def advance_all(db: AsyncSession) -> dict:
    """Daily task body: step every plan in the current org forward one day.

    Backfills any missing plans first, so a sender connected before the
    ensure-plan wiring existed still gets a ramp (and the plans table stops being
    empty) rather than silently sending at full volume.

    Returns a small summary ({plans, advanced, graduated, backfilled}) for the
    task log.
    """
    backfill = await backfill_warmup_plans(db)

    plans = (await db.execute(select(SenderWarmupPlan))).scalars().all()
    advanced = 0
    graduated = 0
    for plan in plans:
        if recompute(plan):
            advanced += 1
        if plan.status == "graduated":
            graduated += 1
    await db.commit()
    return {
        "plans": len(plans),
        "advanced": advanced,
        "graduated": graduated,
        "backfilled": backfill["plans_created"],
    }
