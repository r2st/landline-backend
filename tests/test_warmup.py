"""Tests for the automated email warm-up schedule (Feature 1)."""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount
from app.models.sender_warmup import SenderWarmupPlan
from app.services import warmup


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


# ─── Ramp function ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "day,expected_limit,expected_status",
    [
        (1, 5, "warming"),
        (3, 5, "warming"),
        (4, 10, "warming"),
        (7, 10, "warming"),
        (8, 20, "warming"),
        (14, 20, "warming"),
        (15, 35, "warming"),
        (21, 35, "warming"),
        (22, 50, "warming"),
        (30, 50, "warming"),
        (31, 999, "graduated"),
        (60, 999, "graduated"),
    ],
)
def test_limit_for_day_ramp(day, expected_limit, expected_status):
    limit, status = warmup.limit_for_day(day, graduated_limit=999)
    assert (limit, status) == (expected_limit, expected_status)


def test_limit_for_day_clamps_below_one():
    # A day of 0 or negative clamps to day 1 (5/day), never crashes.
    assert warmup.limit_for_day(0, 999) == (5, "warming")
    assert warmup.limit_for_day(-4, 999) == (5, "warming")


def test_day_for_is_one_based():
    today = date(2026, 7, 17)
    assert warmup.day_for(date(2026, 7, 17), today) == 1  # start day == day 1
    assert warmup.day_for(date(2026, 7, 15), today) == 3
    assert warmup.day_for(date(2026, 8, 1), today) == 1  # future start clamps to 1


# ─── used_today rollover ─────────────────────────────────────────────────────


class _Acct:
    def __init__(self, count, reset_at):
        self.daily_send_count = count
        self.daily_count_reset_at = reset_at


def test_used_today_honours_rollover():
    now = datetime.now(timezone.utc)
    assert warmup.used_today(_Acct(7, now)) == 7  # counter is today's
    assert warmup.used_today(_Acct(7, now - timedelta(days=1))) == 0  # stale → 0
    assert warmup.used_today(_Acct(7, None)) == 0  # never sent → 0


# ─── ensure_warmup_plan ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_warmup_plan_idempotent(db_session: AsyncSession):
    acct_id = uuid.uuid4()
    p1 = await warmup.ensure_warmup_plan(
        db_session, account_id=acct_id, account_type="gmail", account_email="a@x.com"
    )
    await db_session.commit()
    assert p1.current_day == 1
    assert p1.daily_limit == 5
    assert p1.status == "warming"

    p2 = await warmup.ensure_warmup_plan(
        db_session, account_id=acct_id, account_type="gmail", account_email="a@x.com"
    )
    assert p2.id == p1.id  # returns existing, no duplicate


@pytest.mark.asyncio
async def test_check_send_allowed_respects_daily_limit(db_session: AsyncSession):
    acct_id = uuid.uuid4()
    plan = SenderWarmupPlan(
        account_id=acct_id, account_type="gmail", account_email="a@x.com",
        start_date=_utc_today(), current_day=1, daily_limit=5, status="warming",
    )
    db_session.add(plan)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    account = GmailAccount(
        id=acct_id, email="a@x.com", google_sub="g1",
        refresh_token_encrypted="x", daily_send_count=4, daily_count_reset_at=now,
    )
    ok, _ = await warmup.check_send_allowed(db_session, account)
    assert ok is True  # 4 < 5

    account.daily_send_count = 5
    ok, reason = await warmup.check_send_allowed(db_session, account)
    assert ok is False
    assert "warm-up daily limit" in reason


@pytest.mark.asyncio
async def test_check_send_allowed_paused_blocks(db_session: AsyncSession):
    acct_id = uuid.uuid4()
    db_session.add(SenderWarmupPlan(
        account_id=acct_id, account_type="gmail", start_date=_utc_today(),
        current_day=1, daily_limit=5, status="paused",
    ))
    await db_session.commit()
    account = GmailAccount(id=acct_id, email="a@x.com", google_sub="g", refresh_token_encrypted="x")
    ok, reason = await warmup.check_send_allowed(db_session, account)
    assert ok is False
    assert "paused" in reason


@pytest.mark.asyncio
async def test_check_send_allowed_no_plan_or_graduated(db_session: AsyncSession):
    # No plan at all → allowed.
    account = GmailAccount(id=uuid.uuid4(), email="np@x.com", google_sub="g", refresh_token_encrypted="x")
    ok, _ = await warmup.check_send_allowed(db_session, account)
    assert ok is True

    # Graduated plan → allowed even with a high send count.
    gid = uuid.uuid4()
    db_session.add(SenderWarmupPlan(
        account_id=gid, account_type="gmail", start_date=_utc_today() - timedelta(days=40),
        current_day=41, daily_limit=200, status="graduated",
    ))
    await db_session.commit()
    now = datetime.now(timezone.utc)
    grad = GmailAccount(id=gid, email="g@x.com", google_sub="g2", refresh_token_encrypted="x",
                        daily_send_count=199, daily_count_reset_at=now)
    ok, _ = await warmup.check_send_allowed(db_session, grad)
    assert ok is True


# ─── advance_all (daily task body) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_advance_all_steps_day_and_graduates(db_session: AsyncSession):
    # A plan that started 8 days ago should now be on day 9 (20/day).
    warming = SenderWarmupPlan(
        account_id=uuid.uuid4(), account_type="gmail",
        start_date=_utc_today() - timedelta(days=8),
        current_day=1, daily_limit=5, status="warming",
    )
    # A plan that started 35 days ago should graduate.
    old = SenderWarmupPlan(
        account_id=uuid.uuid4(), account_type="gmail",
        start_date=_utc_today() - timedelta(days=35),
        current_day=1, daily_limit=5, status="warming",
    )
    db_session.add_all([warming, old])
    await db_session.commit()

    result = await warmup.advance_all(db_session)
    assert result["plans"] == 2
    assert result["graduated"] == 1

    await db_session.refresh(warming)
    await db_session.refresh(old)
    assert warming.current_day == 9
    assert warming.daily_limit == 20
    assert warming.status == "warming"
    assert old.status == "graduated"


@pytest.mark.asyncio
async def test_advance_all_leaves_paused_untouched(db_session: AsyncSession):
    paused = SenderWarmupPlan(
        account_id=uuid.uuid4(), account_type="gmail",
        start_date=_utc_today() - timedelta(days=20),
        current_day=2, daily_limit=5, status="paused",
    )
    db_session.add(paused)
    await db_session.commit()
    await warmup.advance_all(db_session)
    await db_session.refresh(paused)
    assert paused.current_day == 2  # frozen
    assert paused.status == "paused"


# ─── API: GET / POST /dialtone/warmup ────────────────────────────────────────


@pytest.mark.asyncio
async def test_warmup_api_list_and_create(client: AsyncClient, db_session: AsyncSession):
    account = GmailAccount(
        id=uuid.uuid4(), email="sender@apprend.tech", google_sub="gsub-1",
        refresh_token_encrypted="enc", status="connected",
    )
    db_session.add(account)
    await db_session.commit()

    # Create a plan for the sender.
    resp = await client.post("/api/v1/dialtone/warmup", json={"account_id": str(account.id)})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["account_type"] == "gmail"
    assert body["current_day"] == 1
    assert body["daily_limit"] == 5
    assert body["status"] == "warming"
    assert body["remaining_today"] == 5

    # Duplicate create → 409.
    dup = await client.post("/api/v1/dialtone/warmup", json={"account_id": str(account.id)})
    assert dup.status_code == 409

    # List shows it.
    lst = await client.get("/api/v1/dialtone/warmup")
    assert lst.status_code == 200
    plans = lst.json()
    assert len(plans) == 1
    assert plans[0]["account_email"] == "sender@apprend.tech"


@pytest.mark.asyncio
async def test_warmup_api_create_unknown_account_404(client: AsyncClient):
    resp = await client.post("/api/v1/dialtone/warmup", json={"account_id": str(uuid.uuid4())})
    assert resp.status_code == 404


# ─── Send-path gate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_outreach_email_skipped_when_warmup_capped(
    db_session: AsyncSession, monkeypatch
):
    """send_outreach_email must not put mail on the wire past the warm-up cap."""
    from app.services.email_dispatch import send_outreach_email

    sent = {"count": 0}

    def _fake_send(self, **kw):
        sent["count"] += 1
        return {"status": "sent", "message_id": "x", "transport": "gmail"}

    monkeypatch.setattr(
        "app.services.email_dispatch.GmailSender.send_email", _fake_send
    )

    now = datetime.now(timezone.utc)
    account = GmailAccount(
        id=uuid.uuid4(), email="warm@x.com", google_sub="gw",
        refresh_token_encrypted="enc", access_token_encrypted="enc",
        is_active=True, sending_enabled=True, status="connected",
        daily_send_count=5, daily_count_reset_at=now,
    )
    db_session.add(account)
    db_session.add(SenderWarmupPlan(
        account_id=account.id, account_type="gmail", account_email="warm@x.com",
        start_date=_utc_today(), current_day=1, daily_limit=5, status="warming",
    ))
    await db_session.commit()

    result = await send_outreach_email(
        db_session, to_email="lead@x.com", subject="Hi", body_html="<p>Hi</p>",
        sender_account_id=account.id, include_unsubscribe=False,
    )
    assert result["status"] == "skipped"
    assert "warm-up" in result["reason"]
    assert sent["count"] == 0  # nothing was actually sent


@pytest.mark.asyncio
async def test_warmup_api_rejects_mismatched_account_type(
    client: AsyncClient, db_session: AsyncSession
):
    """A supplied ``account_type`` that disagrees with the account is a 400.

    The runtime gate resolves a plan by the *class of the account object*
    (``warmup._transport_for``), so a plan stored under the other transport is
    one it can never find. The failure is silent in the direction that matters:
    the plan is listed on the Sender Health page and its ramp advances daily
    while the mailbox sends with no warm-up cap at all. Nothing errors, and the
    control reports itself enforced.
    """
    account = GmailAccount(
        id=uuid.uuid4(), email="mismatch@apprend.tech", google_sub="gsub-mismatch",
        refresh_token_encrypted="enc", status="connected",
    )
    db_session.add(account)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/dialtone/warmup",
        json={"account_id": str(account.id), "account_type": "smtp"},
    )
    assert resp.status_code == 400, resp.text
    assert "gmail" in resp.json()["detail"]

    # Nothing was written — in particular no plan the gate cannot see.
    assert await warmup.get_plan(db_session, account.id, "smtp") is None
    assert await warmup.get_plan(db_session, account.id, "gmail") is None


@pytest.mark.asyncio
async def test_warmup_api_accepts_matching_account_type(
    client: AsyncClient, db_session: AsyncSession
):
    """Naming the right transport explicitly still works (it is only inferred)."""
    account = GmailAccount(
        id=uuid.uuid4(), email="explicit@apprend.tech", google_sub="gsub-explicit",
        refresh_token_encrypted="enc", status="connected",
    )
    db_session.add(account)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/dialtone/warmup",
        json={"account_id": str(account.id), "account_type": "gmail"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["account_type"] == "gmail"

    # And the gate — which looks the plan up by the account object's own type —
    # finds the plan that was just created.
    plan = await warmup.get_plan(db_session, account.id, "gmail")
    assert plan is not None
    allowed, reason = await warmup.check_send_allowed(db_session, account)
    assert allowed is True, reason
