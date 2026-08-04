"""The per-mailbox daily counter has to be right in both directions.

``daily_send_count`` is the number the warm-up ramp is enforced against and the
number rotation reads to decide a mailbox has had enough for today. Two separate
faults made it wrong, and neither produced an error anywhere:

* **it was incremented in Python.** ``count = (count or 0) + 1`` on a loaded
  instance is a read-modify-write whose read happened when the row was loaded —
  before recipient validation, composition and the SMTP round trip. Concurrent
  sends from one mailbox all read the same value and all wrote the same value,
  so every send after the first was free. Under-counting is the one direction a
  volume control must not be wrong in: a cold domain sends more than its ramp
  while the ramp reports itself enforced.

* **it was read raw.** The counter is only *today's* count when
  ``daily_count_reset_at`` is today, and nothing rolls it over except a send.
  Rotation's cap check compared the raw column, so a mailbox that finished
  yesterday's ramp was still "at cap" this morning — excluded from rotation, so
  never sent from, so never rolled over. It stayed frozen for as long as any
  sibling mailbox remained under cap, which inverts the whole point of a fleet:
  the day's volume concentrates on one mailbox instead of spreading across them.

``warmup.used_today`` already encoded the rollover rule and already had the
callers that mattered. These pin the two that didn't.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_account import GmailAccount

from app.models.sender_warmup import SenderWarmupPlan
from app.services import email_dispatch
from app.services.send_limiter import get_send_limiter


async def _mk_account(db: AsyncSession, **overrides) -> GmailAccount:
    fields = {
        "id": uuid.uuid4(),
        "email": f"rep-{uuid.uuid4().hex[:8]}@apprend.co",
        "google_sub": str(uuid.uuid4()),
        "display_name": "Rep",
        "refresh_token_encrypted": "x",
        "status": "connected",
        "sending_enabled": True,
        "connected_at": datetime.now(timezone.utc),
        **overrides,
    }
    account = GmailAccount(**fields)
    db.add(account)
    await db.flush()
    return account


async def _mk_plan(db: AsyncSession, account, *, limit: int) -> SenderWarmupPlan:
    plan = SenderWarmupPlan(
        account_id=account.id,
        account_type="gmail",
        account_email=account.email,
        start_date=date.today(),
        current_day=1,
        daily_limit=limit,
        status="warming",
    )
    db.add(plan)
    await db.flush()
    return plan


# ─── the increment is atomic ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_increment_is_computed_by_the_database_not_by_python(db_session):
    """The UPDATE must say ``count + 1``, not carry a value read minutes ago.

    Written against the row rather than the instance: another writer moves the
    counter after this instance was loaded, and the send still has to land on
    top of *that*. The Python read-modify-write wrote 6 here — the value it read
    at load time plus one — silently discarding four sends.
    """
    account = await _mk_account(
        db_session, daily_send_count=5, daily_count_reset_at=datetime.now(timezone.utc)
    )
    await db_session.commit()

    # Stand in for a concurrent worker that got there first: the row moves while
    # the instance in hand still remembers 5.
    await db_session.execute(
        GmailAccount.__table__.update()
        .where(GmailAccount.id == account.id)
        .values(daily_send_count=9)
    )
    assert account.daily_send_count == 5, "precondition: the instance is stale"

    await email_dispatch.record_account_send(db_session, account, sent=True)

    row = (
        await db_session.execute(
            select(GmailAccount.daily_send_count).where(GmailAccount.id == account.id)
        )
    ).scalar_one()
    assert row == 10, "the send must be added to the row's value, not the instance's"
    assert account.daily_send_count == 10, "and the instance must be reloaded"


@pytest.mark.asyncio
async def test_concurrent_sends_from_one_mailbox_each_cost_a_slot(db_session):
    """N sends move the counter by N, however they interleave.

    The read-modify-write lost every send that overlapped another: five calls
    against a counter each of them read as 0 all wrote 1.
    """
    account = await _mk_account(
        db_session, daily_send_count=0, daily_count_reset_at=datetime.now(timezone.utc)
    )
    await db_session.commit()

    for _ in range(5):
        # Sequential on one session — asyncio gives no real concurrency against a
        # single connection. What this pins is that each call re-reads the row,
        # which is what makes the concurrent case correct too.
        await email_dispatch.record_account_send(db_session, account, sent=True)

    assert account.daily_send_count == 5


@pytest.mark.asyncio
async def test_a_stale_worker_cannot_zero_a_counter_already_rolled_over(db_session):
    """The rollover decision is made in SQL, against the row's own stamp.

    A worker holding an instance loaded before midnight would otherwise decide
    "the reset stamp is yesterday, zero it" and wipe the sends a worker on the
    other side of the boundary had already recorded.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    account = await _mk_account(
        db_session, daily_send_count=17, daily_count_reset_at=yesterday
    )
    await db_session.commit()

    # Another worker crossed the boundary first: rolled over and sent three.
    await db_session.execute(
        GmailAccount.__table__.update()
        .where(GmailAccount.id == account.id)
        .values(daily_send_count=3, daily_count_reset_at=datetime.now(timezone.utc))
    )
    assert account.daily_count_reset_at == yesterday, "precondition: instance is stale"

    await email_dispatch.record_account_send(db_session, account, sent=True)

    assert account.daily_send_count == 4, "today's three sends must survive"


@pytest.mark.asyncio
async def test_rollover_still_happens_when_the_stamp_really_is_yesterday(db_session):
    """The behaviour the previous test must not have broken."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    account = await _mk_account(
        db_session, daily_send_count=17, daily_count_reset_at=yesterday
    )

    await email_dispatch.record_account_send(db_session, account, sent=True)

    assert account.daily_send_count == 1
    # SQLite drops the offset on read-back; compare in one frame of reference.
    rolled = account.daily_count_reset_at
    if rolled.tzinfo is None:
        rolled = rolled.replace(tzinfo=timezone.utc)
    assert rolled > yesterday


@pytest.mark.asyncio
async def test_an_unsent_attempt_still_rolls_the_day_over_without_counting(db_session):
    """A failed attempt costs the mailbox nothing but is still today's news."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    account = await _mk_account(
        db_session, daily_send_count=17, daily_count_reset_at=yesterday
    )

    await email_dispatch.record_account_send(db_session, account, sent=False)

    assert account.daily_send_count == 0, "yesterday's count is not today's"
    assert account.last_used_at is not None, "rotation still saw an attempt"


# ─── the read honours the rollover ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_yesterdays_capped_mailbox_is_eligible_again_this_morning(db_session):
    """Rotation must not freeze out a mailbox on a counter it will never reset.

    The trap is circular and silent: excluded because the stale count reads as
    at-cap, and the count is only ever reset *by a send*, which only happens if
    it is not excluded. A single mailbox self-heals because
    ``get_next_rotation_account`` falls back to the unfiltered pool when the
    filter empties it — so this needs two, one of which is genuinely under cap
    today and keeps the fallback from firing.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    spent = await _mk_account(
        db_session,
        email="spent@apprend.co",
        daily_send_count=5,
        daily_count_reset_at=yesterday,
        last_used_at=yesterday,
    )
    fresh = await _mk_account(
        db_session,
        email="fresh@apprend.co",
        daily_send_count=1,
        daily_count_reset_at=datetime.now(timezone.utc),
        last_used_at=datetime.now(timezone.utc),
    )
    await _mk_plan(db_session, spent, limit=5)
    await _mk_plan(db_session, fresh, limit=5)

    kept = await email_dispatch._filter_by_sender_health(db_session, [spent, fresh])

    kept_emails = {a.email for a in kept}
    assert "spent@apprend.co" in kept_emails, (
        "yesterday's spent ramp is not today's — the mailbox has a full day"
    )
    assert "fresh@apprend.co" in kept_emails


@pytest.mark.asyncio
async def test_a_mailbox_actually_at_cap_today_is_still_excluded(db_session):
    """The control: the fix must not have removed the cap it reads."""
    now = datetime.now(timezone.utc)
    spent = await _mk_account(
        db_session, email="spent2@apprend.co", daily_send_count=5, daily_count_reset_at=now
    )
    fresh = await _mk_account(
        db_session, email="fresh2@apprend.co", daily_send_count=1, daily_count_reset_at=now
    )
    await _mk_plan(db_session, spent, limit=5)
    await _mk_plan(db_session, fresh, limit=5)

    kept = await email_dispatch._filter_by_sender_health(db_session, [spent, fresh])

    assert {a.email for a in kept} == {"fresh2@apprend.co"}


@pytest.mark.asyncio
async def test_capacity_report_does_not_show_yesterday_as_spent(db_session):
    """What the Sender Health page shows an operator, and what ``can_send`` says.

    Read raw, a mailbox that finished yesterday's ramp reported ``0 remaining``
    and ``can_send: False`` for the whole of the next morning — an operator
    looking at a fleet that is, as far as the page is concerned, out of road.
    """
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    account = await _mk_account(
        db_session, daily_send_count=5, daily_count_reset_at=yesterday
    )
    await _mk_plan(db_session, account, limit=5)

    report = await get_send_limiter().account_capacity(db_session, account)

    assert report["used_today"] == 0
    assert report["remaining_today"] == 5
    assert report["can_send"] is True


@pytest.mark.asyncio
async def test_capacity_report_still_reports_a_mailbox_spent_today(db_session):
    """The control for the test above."""
    account = await _mk_account(
        db_session, daily_send_count=5, daily_count_reset_at=datetime.now(timezone.utc)
    )
    await _mk_plan(db_session, account, limit=5)

    report = await get_send_limiter().account_capacity(db_session, account)

    assert report["used_today"] == 5
    assert report["remaining_today"] == 0
    assert report["can_send"] is False


@pytest.mark.asyncio
async def test_a_non_mapped_account_costs_nothing_rather_than_raising():
    """The counter write runs *after* the message is on the wire.

    ``record_account_send`` builds its UPDATE from the account's mapped class,
    so an object that isn't one has no table to name. Raising there would not
    lose a counter — it would roll back the send's whole bookkeeping
    transaction and let Celery retry the step, which is the duplicate cold email
    ``send_claim`` exists to prevent. An account shape we can't book against
    must cost the send nothing, not turn a delivered message into a resend.
    """
    db = AsyncMock()
    account = MagicMock()
    account.id = uuid.uuid4()

    await email_dispatch.record_account_send(db, account, sent=True)

    db.execute.assert_not_awaited()
    db.refresh.assert_not_awaited()
