"""Tests for warm-up plan backfill (email pipeline audit #5).

The ensure-plan wiring only covers newly connected accounts, so mailboxes that
predate it had no plan and ``sender_warmup_plans`` sat empty. The backfill
creates a plan for every connected sender that lacks one, idempotently.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.sender_warmup import SenderWarmupPlan
from app.models.smtp_account import SmtpAccount
from app.services import warmup

pytestmark = pytest.mark.asyncio


async def _gmail(db, email, status="connected"):
    acct = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=str(uuid.uuid4()),
        refresh_token_encrypted="x", is_active=True, status=status,
    )
    db.add(acct)
    await db.flush()
    return acct


async def _smtp(db, email, status="connected"):
    acct = SmtpAccount(
        id=uuid.uuid4(), email=email, host="smtp.x.com", port=587,
        username=email, password_encrypted="x", status=status, imap_enabled=True,
    )
    db.add(acct)
    await db.flush()
    return acct


async def test_backfill_creates_plans_for_existing_accounts(db_session: AsyncSession):
    g = await _gmail(db_session, "a@apprend.tech")
    s = await _smtp(db_session, "b@apprend.tech")
    await db_session.commit()

    # No plans yet — this is the "0 rows" state the audit found.
    before = (await db_session.execute(select(func.count()).select_from(SenderWarmupPlan))).scalar()
    assert before == 0

    result = await warmup.backfill_warmup_plans(db_session)
    assert result["plans_created"] == 2

    g_plan = await warmup.get_plan(db_session, g.id, "gmail")
    s_plan = await warmup.get_plan(db_session, s.id, "smtp")
    assert g_plan is not None and g_plan.status == "warming"
    assert s_plan is not None and s_plan.current_day == 1  # starts today, not back-dated


async def test_backfill_is_idempotent(db_session: AsyncSession):
    await _gmail(db_session, "a@apprend.tech")
    await db_session.commit()

    first = await warmup.backfill_warmup_plans(db_session)
    assert first["plans_created"] == 1
    second = await warmup.backfill_warmup_plans(db_session)
    assert second["plans_created"] == 0

    total = (await db_session.execute(select(func.count()).select_from(SenderWarmupPlan))).scalar()
    assert total == 1


async def test_backfill_skips_disconnected_accounts(db_session: AsyncSession):
    await _gmail(db_session, "revoked@apprend.tech", status="revoked")
    await db_session.commit()

    result = await warmup.backfill_warmup_plans(db_session)
    assert result["plans_created"] == 0


async def test_advance_all_backfills_then_advances(db_session: AsyncSession):
    await _gmail(db_session, "a@apprend.tech")
    await db_session.commit()

    result = await warmup.advance_all(db_session)
    assert result["backfilled"] == 1
    assert result["plans"] == 1


# --- Crediting existing send history -----------------------------------------
#
# The first backfill started every sender at day 1. That is right for a new
# mailbox and wrong for one that had been sending for weeks: it re-throttled an
# already-warm sender from its real volume down to 5/day, and outreach
# throughput collapsed overnight. ``credited_start_date`` back-dates the ramp for
# a mailbox with real history, capped so nobody graduates on a date field alone.


async def test_never_used_account_still_starts_at_day_one(db_session: AsyncSession):
    """No sends means genuinely new — the ramp must start from scratch."""
    acct = await _gmail(db_session, "brandnew@apprend.tech")
    acct.connected_at = datetime.now(timezone.utc) - timedelta(days=90)
    acct.last_used_at = None
    await db_session.commit()

    await warmup.backfill_warmup_plans(db_session)
    plan = await warmup.get_plan(db_session, acct.id, "gmail")
    assert plan.current_day == 1
    assert plan.daily_limit == 5


async def test_account_with_history_is_credited_not_reset_to_five_a_day(
    db_session: AsyncSession,
):
    """The regression: a mailbox sending for a month must not drop to 5/day."""
    acct = await _gmail(db_session, "veteran@apprend.tech")
    acct.connected_at = datetime.now(timezone.utc) - timedelta(days=28)
    acct.last_used_at = datetime.now(timezone.utc) - timedelta(hours=2)
    await db_session.commit()

    await warmup.backfill_warmup_plans(db_session)
    plan = await warmup.get_plan(db_session, acct.id, "gmail")

    assert plan.current_day == warmup.BACKFILL_MAX_CREDIT_DAY
    assert plan.daily_limit == 20  # top of the day 8–14 band, not 5
    assert plan.status == "warming"


async def test_credit_is_capped_so_an_old_account_cannot_graduate_instantly(
    db_session: AsyncSession,
):
    """A two-year-old mailbox still climbs the last bands under observation."""
    acct = await _gmail(db_session, "ancient@apprend.tech")
    acct.connected_at = datetime.now(timezone.utc) - timedelta(days=700)
    acct.last_used_at = datetime.now(timezone.utc)
    await db_session.commit()

    await warmup.backfill_warmup_plans(db_session)
    plan = await warmup.get_plan(db_session, acct.id, "gmail")

    assert plan.current_day == warmup.BACKFILL_MAX_CREDIT_DAY
    assert plan.status != "graduated"


async def test_partial_history_is_credited_proportionally(db_session: AsyncSession):
    """Under the cap, credit tracks actual age rather than snapping to it."""
    acct = await _gmail(db_session, "week-old@apprend.tech")
    acct.connected_at = datetime.now(timezone.utc) - timedelta(days=5)
    acct.last_used_at = datetime.now(timezone.utc)
    await db_session.commit()

    await warmup.backfill_warmup_plans(db_session)
    plan = await warmup.get_plan(db_session, acct.id, "gmail")

    assert plan.current_day == 6      # day 1 == connected_at, so 5 days later is day 6
    assert plan.daily_limit == 10     # day 4–7 band


async def test_credited_plan_keeps_advancing_from_its_credited_day(
    db_session: AsyncSession,
):
    """The back-dated start_date must stay consistent with the daily advance."""
    acct = await _gmail(db_session, "advancing@apprend.tech")
    acct.connected_at = datetime.now(timezone.utc) - timedelta(days=20)
    acct.last_used_at = datetime.now(timezone.utc)
    await db_session.commit()

    await warmup.backfill_warmup_plans(db_session)
    plan = await warmup.get_plan(db_session, acct.id, "gmail")
    assert plan.current_day == warmup.BACKFILL_MAX_CREDIT_DAY

    # Roll the clock forward a day: the plan steps to 15, not back to 2.
    warmup.recompute(plan, today=datetime.now(timezone.utc).date() + timedelta(days=1))
    assert plan.current_day == warmup.BACKFILL_MAX_CREDIT_DAY + 1
    assert plan.daily_limit == 35  # day 15–21 band
