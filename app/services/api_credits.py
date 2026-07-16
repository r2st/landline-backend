"""Credit accounting and the pre-call budget gate for paid external APIs.

Two rules shape this module:

* **The gate is checked before the call, never after.** Debiting after the fact
  is accounting; refusing beforehand is a budget. An exhausted budget must cost
  zero credits.
* **Every attempt is recorded — success, 429, or error.** A rate-limited call
  may still have cost a credit, and conflating "Apollo rate-limited us all day"
  with "the ICP found nothing" hides an outage as a bad profile.

The ledger is a *guardrail*, not billing truth: reconcile against the provider's
own dashboard. Budgets of ``0`` mean unlimited — the escape hatch for when the
gate starves a legitimate run.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.api_credit import ApiCreditLedger

logger = logging.getLogger(__name__)


@dataclass
class BudgetStatus:
    """A point-in-time read of one provider's spend against its budgets."""

    provider: str
    daily_spent: int
    daily_budget: int          # 0 = unlimited
    monthly_spent: int
    monthly_budget: int        # 0 = unlimited
    allowed: bool
    reason: Optional[str] = None

    @property
    def daily_fraction(self) -> float:
        return (self.daily_spent / self.daily_budget) if self.daily_budget else 0.0

    @property
    def monthly_fraction(self) -> float:
        return (self.monthly_spent / self.monthly_budget) if self.monthly_budget else 0.0

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "daily": {
                "spent": self.daily_spent,
                "budget": self.daily_budget,
                "fraction": round(self.daily_fraction, 4),
                "unlimited": self.daily_budget == 0,
            },
            "monthly": {
                "spent": self.monthly_spent,
                "budget": self.monthly_budget,
                "fraction": round(self.monthly_fraction, 4),
                "unlimited": self.monthly_budget == 0,
            },
            "allowed": self.allowed,
            "reason": self.reason,
        }


def _day_start(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _budgets_for(provider: str) -> tuple[int, int]:
    """(daily, monthly) budget for a provider. 0 means unlimited."""
    s = get_settings()
    if provider == "apollo":
        return s.apollo_daily_credit_budget, s.apollo_monthly_credit_budget
    # Providers without configured budgets are unlimited until they get one.
    # Better to under-guard a provider than to silently block a live path.
    return 0, 0


async def _spent_since(db: AsyncSession, provider: str, since: datetime) -> int:
    """Credits recorded for a provider since ``since``.

    Counts every status, not just ``ok``: a 429 or an error can still be billed,
    and a gate that ignores failed calls is a gate an outage can walk straight
    through.
    """
    total = (
        await db.execute(
            select(func.coalesce(func.sum(ApiCreditLedger.credits), 0)).where(
                ApiCreditLedger.provider == provider,
                ApiCreditLedger.created_at >= since,
            )
        )
    ).scalar()
    return int(total or 0)


async def get_budget_status(
    db: AsyncSession, provider: str, *, cost: int = 1, now: Optional[datetime] = None
) -> BudgetStatus:
    """Read spend vs budget, and whether a call costing ``cost`` may proceed."""
    now = now or datetime.now(timezone.utc)
    daily_budget, monthly_budget = _budgets_for(provider)

    daily_spent = await _spent_since(db, provider, _day_start(now))
    monthly_spent = await _spent_since(db, provider, _month_start(now))

    allowed, reason = True, None
    # Check that the call *fits* — spent + cost, not spent alone. Otherwise the
    # last call of a budget is always allowed to overshoot.
    if daily_budget and daily_spent + cost > daily_budget:
        allowed = False
        reason = f"daily {provider} credit budget exhausted ({daily_spent}/{daily_budget})"
    elif monthly_budget and monthly_spent + cost > monthly_budget:
        allowed = False
        reason = f"monthly {provider} credit budget exhausted ({monthly_spent}/{monthly_budget})"

    return BudgetStatus(
        provider=provider,
        daily_spent=daily_spent,
        daily_budget=daily_budget,
        monthly_spent=monthly_spent,
        monthly_budget=monthly_budget,
        allowed=allowed,
        reason=reason,
    )


async def check_budget(
    db: AsyncSession, provider: str, *, cost: int = 1, now: Optional[datetime] = None
) -> BudgetStatus:
    """The pre-call gate. Call this and honour ``.allowed`` before spending."""
    status = await get_budget_status(db, provider, cost=cost, now=now)
    if not status.allowed:
        logger.warning("Credit gate blocked a %s call: %s", provider, status.reason)
    return status


async def record_usage(
    db: AsyncSession,
    provider: str,
    operation: str,
    *,
    credits: int = 1,
    status: str = "ok",
    run_id: Optional[UUID] = None,
    icp_profile_id: Optional[UUID] = None,
    details: Optional[dict] = None,
) -> ApiCreditLedger:
    """Debit the ledger for one external call.

    Flushes but never commits — the caller owns the transaction (the same rule
    that made ``search_companies``' internal commit a bug).
    """
    entry = ApiCreditLedger(
        provider=provider,
        operation=operation,
        credits=max(0, int(credits)),
        status=status,
        run_id=run_id,
        icp_profile_id=icp_profile_id,
        details=details or {},
    )
    db.add(entry)
    await db.flush()
    return entry


async def should_alert(
    db: AsyncSession, provider: str, *, now: Optional[datetime] = None
) -> Optional[str]:
    """Return an alert message when spend has crossed the alert threshold.

    Change-only alerting (fire when the threshold is *crossed*, not every call)
    is the caller's job — mirrors the domain-health anti-fatigue precedent.
    """
    s = get_settings()
    threshold = s.prospecting_credit_alert_threshold
    if threshold <= 0:
        return None
    status = await get_budget_status(db, provider, cost=0, now=now)
    if status.daily_budget and status.daily_fraction >= threshold:
        return (
            f"{provider} daily credits at {status.daily_fraction:.0%} "
            f"({status.daily_spent}/{status.daily_budget})"
        )
    if status.monthly_budget and status.monthly_fraction >= threshold:
        return (
            f"{provider} monthly credits at {status.monthly_fraction:.0%} "
            f"({status.monthly_spent}/{status.monthly_budget})"
        )
    return None
