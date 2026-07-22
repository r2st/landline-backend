"""Tests for warm-up plan backfill (email pipeline audit #5).

The ensure-plan wiring only covers newly connected accounts, so mailboxes that
predate it had no plan and ``sender_warmup_plans`` sat empty. The backfill
creates a plan for every connected sender that lacks one, idempotently.
"""

import uuid

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
