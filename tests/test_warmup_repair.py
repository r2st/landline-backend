"""Tests for re-crediting warm-up plans that were backfilled at day 1.

``backfill_warmup_plans`` only ever creates plans that are *missing*, so once a
plan existed with a wrong (uncredited) start date, no later run could repair it.
Six mailboxes with weeks of sending history sat pinned at day 1 / 5-per-day,
capping total capacity at 30 sends a day.

``repair_uncredited_plans`` closes that hole, and must only ever move a sender
*forward* on the ramp.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.sender_warmup import SenderWarmupPlan
from app.services import warmup

pytestmark = pytest.mark.asyncio


def _utc_today():
    return datetime.now(timezone.utc).date()


async def _gmail(db, email, *, connected_days_ago: int, has_sent: bool = True):
    """A connected mailbox that connected N days ago (and has sent, by default)."""
    now = datetime.now(timezone.utc)
    acct = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=str(uuid.uuid4()),
        refresh_token_encrypted="x",
        is_active=True,
        status="connected",
        connected_at=now - timedelta(days=connected_days_ago),
        last_used_at=now - timedelta(hours=1) if has_sent else None,
    )
    db.add(acct)
    await db.flush()
    return acct


async def _plan_starting_today(db, account) -> SenderWarmupPlan:
    """The broken row shape: a day-1 plan on a mailbox with real history."""
    plan = SenderWarmupPlan(
        account_id=account.id,
        account_type="gmail",
        account_email=account.email,
        start_date=_utc_today(),
        current_day=1,
        daily_limit=5,
        status="warming",
    )
    db.add(plan)
    await db.flush()
    return plan


async def test_repairs_plan_pinned_at_day_one(db_session: AsyncSession):
    """The production case: 4 weeks of history, plan wrongly restarted at day 1."""
    acct = await _gmail(db_session, "old@apprend.tech", connected_days_ago=28)
    plan = await _plan_starting_today(db_session, acct)
    await db_session.commit()

    result = await warmup.repair_uncredited_plans(db_session)
    assert result["plans_repaired"] == 1

    await db_session.refresh(plan)
    # Credit is clamped to BACKFILL_MAX_CREDIT_DAY (14) → the 20/day band.
    assert plan.current_day == warmup.BACKFILL_MAX_CREDIT_DAY
    assert plan.daily_limit == 20
    assert plan.status == "warming"
    assert plan.start_date == _utc_today() - timedelta(
        days=warmup.BACKFILL_MAX_CREDIT_DAY - 1
    )


async def test_repair_is_idempotent(db_session: AsyncSession):
    acct = await _gmail(db_session, "old@apprend.tech", connected_days_ago=28)
    await _plan_starting_today(db_session, acct)
    await db_session.commit()

    first = await warmup.repair_uncredited_plans(db_session)
    assert first["plans_repaired"] == 1
    second = await warmup.repair_uncredited_plans(db_session)
    assert second["plans_repaired"] == 0


async def test_never_moves_a_sender_backwards(db_session: AsyncSession):
    """A plan already further along than its credit is left alone.

    Guards the destructive failure mode: re-crediting must never *demote* a
    warmed sender back down the ramp.
    """
    acct = await _gmail(db_session, "warm@apprend.tech", connected_days_ago=28)
    plan = SenderWarmupPlan(
        account_id=acct.id,
        account_type="gmail",
        account_email=acct.email,
        start_date=_utc_today() - timedelta(days=40),  # already graduated
        current_day=41,
        daily_limit=warmup.org_normal_limit(),
        status="graduated",
    )
    db_session.add(plan)
    await db_session.commit()

    result = await warmup.repair_uncredited_plans(db_session)
    assert result["plans_repaired"] == 0

    await db_session.refresh(plan)
    assert plan.status == "graduated"
    assert plan.start_date == _utc_today() - timedelta(days=40)


async def test_never_credits_a_mailbox_that_has_never_sent(db_session: AsyncSession):
    """A genuinely new mailbox must still start at day 1, whatever its age."""
    acct = await _gmail(
        db_session, "new@apprend.tech", connected_days_ago=28, has_sent=False
    )
    plan = await _plan_starting_today(db_session, acct)
    await db_session.commit()

    result = await warmup.repair_uncredited_plans(db_session)
    assert result["plans_repaired"] == 0

    await db_session.refresh(plan)
    assert plan.current_day == 1
    assert plan.daily_limit == 5


async def test_leaves_paused_plans_frozen(db_session: AsyncSession):
    """A paused plan is an operator hold — repair must not resume it."""
    acct = await _gmail(db_session, "held@apprend.tech", connected_days_ago=28)
    plan = SenderWarmupPlan(
        account_id=acct.id,
        account_type="gmail",
        account_email=acct.email,
        start_date=_utc_today(),
        current_day=1,
        daily_limit=5,
        status="paused",
    )
    db_session.add(plan)
    await db_session.commit()

    result = await warmup.repair_uncredited_plans(db_session)
    assert result["plans_repaired"] == 0

    await db_session.refresh(plan)
    assert plan.status == "paused"
    assert plan.daily_limit == 5


async def test_advance_all_reports_repairs(db_session: AsyncSession):
    """The daily beat runs the repair and surfaces the count in its summary."""
    acct = await _gmail(db_session, "old@apprend.tech", connected_days_ago=28)
    await _plan_starting_today(db_session, acct)
    await db_session.commit()

    summary = await warmup.advance_all(db_session)
    assert summary["repaired"] == 1

    plan = await warmup.get_plan(db_session, acct.id, "gmail")
    assert plan.daily_limit == 20
