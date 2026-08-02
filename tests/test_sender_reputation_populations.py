"""Sender reputation must count the mail the sender actually sent.

`deliverability_optimizer` folds the ingested bounce ledger into the numerator
and read only `EmailSend` for the denominator. Dialtone's sequence sends write
no `EmailSend` row, so an account used purely by the autonomous agent — the
normal configuration — reported `sent_7d = 0` however much mail it had sent,
while every DSN that account produced was counted against it.

The failure is not a wrong rate. `_score` treats `sent < 10` as "too little
volume to divide by" and falls back to scoring **absolute** bad-event counts at
-20 per bounce, so an account was never assessed on a rate at all: bounces
subtracted from its reputation with nothing on the other side of the fraction.
`throttle_factor` multiplies straight into the daily cap
(`send_limiter.account_capacity`), so the reputation system was capping the
sender it exists to protect, in proportion to how much that sender sent.

Note the direction. A blind rail reads healthy and lets damage through; this one
read *unhealthy* and suppressed volume, which looks like caution rather than a
defect — and the symptom, "why is Dialtone barely sending?", points nowhere near
sender health.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.complaint_record import ComplaintRecord
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.deliverability_optimizer import DeliverabilityOptimizer


async def _company(db: AsyncSession) -> Company:
    c = Company(name="Prospect Co", country="US", source="test")
    db.add(c)
    await db.flush()
    return c


async def _contact(db: AsyncSession, company: Company, email: str) -> Contact:
    c = Contact(company_id=company.id, first_name="P", email=email, source="test")
    db.add(c)
    await db.flush()
    return c


async def _account(db: AsyncSession, email="agent@apprend.tech") -> GmailAccount:
    a = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted="x", status="connected",
    )
    db.add(a)
    await db.flush()
    return a


async def _sequence(db: AsyncSession) -> OutreachSequence:
    s = OutreachSequence(id=uuid.uuid4(), name="Cold", is_active=True)
    db.add(s)
    await db.flush()
    return s


async def _sequence_send(db, seq, contact, account, *, event_type="sent", when=None):
    """One autonomous send, recorded the way `tasks/outreach.py` records it."""
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status="active", sender_account_id=account.id,
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        enrollment_id=enr.id, step_number=1, event_type=event_type,
        occurred_at=when or datetime.now(timezone.utc),
    ))
    await db.flush()
    return enr


async def _sequence_traffic(db, seq, company, account, n, *, prefix="s", opens=0):
    for i in range(n):
        contact = await _contact(db, company, f"{prefix}{i}@acme.com")
        enr = await _sequence_send(db, seq, contact, account)
        if i < opens:
            db.add(OutreachEvent(
                enrollment_id=enr.id, step_number=1, event_type="opened",
                occurred_at=datetime.now(timezone.utc),
            ))
    await db.flush()


# ---------------------------------------------------------------------------
# The headline case: a healthy autonomous sender that was being throttled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_healthy_autonomous_sender_is_not_throttled(db_session: AsyncSession):
    """100 sequence sends, 4 async DSN bounces — 4%, under every threshold.

    This is the ordinary production shape: Gmail accepts the message and reports
    the failure minutes later, so the bounces arrive as `BounceRecord` rows and
    the sends exist only as `OutreachEvent`.

    Before the denominator was unioned this scored `sent_7d = 0, bounces_7d = 4`
    → the absolute-count branch → `100 - 4×20 = 20` → below `sender_min_
    reputation` → **throttled to 0.4**, cutting the mailbox's daily cap by 60%
    for a bounce rate a deliverability team would not comment on.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session)
    await _sequence_traffic(db_session, seq, company, account, 100, opens=30)
    for i in range(4):
        db_session.add(BounceRecord(
            gmail_message_id=f"dsn-{i}", gmail_account_id=account.id,
            recipient_email=f"s{i}@acme.com", bounce_kind="hard",
        ))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)
    assert m["sent_7d"] == 100
    assert m["sources"]["sent_sequence"] == 100
    assert m["bounces_7d"] == 4
    assert m["bounce_rate_7d"] == 0.04

    rep, throttle, status = opt._score(m)
    assert throttle == 1.0, "a 4% bounce rate must not cut the daily cap"
    assert status == "healthy"
    assert rep is not None and rep >= 80


@pytest.mark.asyncio
async def test_a_genuinely_bad_sender_is_still_throttled(db_session: AsyncSession):
    """The other half: a real denominator must not become an alibi.

    Restoring the sends restores the fraction in both directions — 30 bounces in
    100 is a 30% bounce rate and the account should lose its capacity.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "burning@apprend.tech")
    await _sequence_traffic(db_session, seq, company, account, 100, opens=30)
    for i in range(30):
        db_session.add(BounceRecord(
            gmail_message_id=f"bad-dsn-{i}", gmail_account_id=account.id,
            recipient_email=f"s{i}@acme.com", bounce_kind="hard",
        ))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)
    assert m["bounce_rate_7d"] == 0.3
    rep, throttle, status = opt._score(m)
    assert throttle < 1.0
    assert status in ("throttled", "critical")


# ---------------------------------------------------------------------------
# The individual populations.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronous_sequence_bounces_count(db_session: AsyncSession):
    """A send that failed on the wire is an attempt, and it is a bounce.

    Counting only successes would leave the worst case — every attempt failing
    — dividing by zero and reading clean, which is the original bug in a hat.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "sync@apprend.tech")
    for i in range(10):
        contact = await _contact(db_session, company, f"dead{i}@gone.com")
        await _sequence_send(db_session, seq, contact, account, event_type="bounced")
    await db_session.commit()

    m = await DeliverabilityOptimizer()._account_metrics(db_session, account)
    assert m["sent_7d"] == 10, "an attempt is in the denominator"
    assert m["bounces_7d"] == 10
    assert m["bounce_rate_7d"] == 1.0


@pytest.mark.asyncio
async def test_sequence_opens_count_toward_the_open_rate(db_session: AsyncSession):
    """Opens on sequence mail are `OutreachEvent`, not `EmailSend.opened_at`.

    `_score` docks 10 points for an open rate under 5% as an inboxing signal.
    Reading opens from the table sequence mail does not write, while reading
    sends from the one it does, would apply that penalty to every autonomous
    account permanently.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "opens@apprend.tech")
    await _sequence_traffic(db_session, seq, company, account, 40, opens=16)
    await db_session.commit()

    m = await DeliverabilityOptimizer()._account_metrics(db_session, account)
    assert m["opens_7d"] == 16
    assert m["open_rate_7d"] == 0.4
    assert m["sources"]["opens_sequence"] == 16


@pytest.mark.asyncio
async def test_ingested_complaints_count(db_session: AsyncSession):
    """`complaint_rate_7d` was a structural zero with the data one table away.

    `_COMPLAINT_STATUSES` names `EmailSend.status` values nothing writes, while
    the ARF/FBL sweep files the real reports in `complaint_records` — which
    nothing read. A complaint rate is what a mailbox provider blocklists a
    domain over, and it throttles at 0.3% where bounce throttles at 5%.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "reported@apprend.tech")
    await _sequence_traffic(db_session, seq, company, account, 100, opens=30)
    for i in range(2):
        db_session.add(ComplaintRecord(
            gmail_message_id=f"fbl-{uuid.uuid4()}", gmail_account_id=account.id,
            recipient_email=f"s{i}@acme.com", feedback_type="abuse",
        ))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)
    assert m["complaints_7d"] == 2
    assert m["complaint_rate_7d"] == 0.02
    assert m["sources"]["complaints_fbl"] == 2

    _rep, throttle, status = opt._score(m)
    assert throttle < 1.0, "2% of recipients reporting spam must cost capacity"
    assert status in ("throttled", "critical")


@pytest.mark.asyncio
async def test_manual_sends_are_still_counted(db_session: AsyncSession):
    """Unioned, not replaced. A mailbox's reputation is all of its mail."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "mixed@apprend.tech")
    await _sequence_traffic(db_session, seq, company, account, 20)
    now = datetime.now(timezone.utc)
    for i in range(30):
        contact = await _contact(db_session, company, f"m{i}@acme.com")
        db_session.add(EmailSend(
            contact_id=contact.id, to_email=contact.email, subject="hi",
            status="sent", sent_at=now, sender_account_id=account.id,
        ))
    await db_session.commit()

    m = await DeliverabilityOptimizer()._account_metrics(db_session, account)
    assert m["sent_7d"] == 50
    assert m["sources"] == {
        "sent_email": 30, "sent_sequence": 20,
        "bounces_email": 0, "bounces_sequence": 0, "bounces_dsn": 0,
        "complaints_email": 0, "complaints_fbl": 0,
        "opens_email": 0, "opens_sequence": 0,
    }


# ---------------------------------------------------------------------------
# Attribution.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_traffic_does_not_leak_between_accounts(db_session: AsyncSession):
    """Throttling the wrong mailbox is worse than throttling none.

    It leaves the account earning the bounces at full capacity and takes a clean
    one offline.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    good = await _account(db_session, "clean@apprend.tech")
    bad = await _account(db_session, "dirty@apprend.tech")
    await _sequence_traffic(db_session, seq, company, good, 50, prefix="g")
    await _sequence_traffic(db_session, seq, company, bad, 50, prefix="b")
    for i in range(20):
        db_session.add(BounceRecord(
            gmail_message_id=f"scoped-{i}", gmail_account_id=bad.id,
            recipient_email=f"b{i}@acme.com", bounce_kind="hard",
        ))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    good_m = await opt._account_metrics(db_session, good)
    bad_m = await opt._account_metrics(db_session, bad)
    assert good_m["sent_7d"] == bad_m["sent_7d"] == 50
    assert good_m["bounces_7d"] == 0
    assert bad_m["bounces_7d"] == 20


@pytest.mark.asyncio
async def test_traffic_outside_the_window_is_not_counted(db_session: AsyncSession):
    """A 7-day rollup. Last month's sends are not this week's denominator."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    account = await _account(db_session, "stale@apprend.tech")
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(20):
        contact = await _contact(db_session, company, f"old{i}@acme.com")
        await _sequence_send(db_session, seq, contact, account, when=old)
    await db_session.commit()

    m = await DeliverabilityOptimizer()._account_metrics(db_session, account)
    assert m["sent_7d"] == 0


@pytest.mark.asyncio
async def test_a_silent_account_still_reports_pending(db_session: AsyncSession):
    """No traffic is not a clean record, and must not become one.

    The `pending` branch is what stops an account that has never sent looking
    like a proven-clean sender; widening the denominator must not fill it in.
    """
    account = await _account(db_session, "unused@apprend.tech")
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)
    assert m["sent_7d"] == 0 and m["bounces_7d"] == 0 and m["complaints_7d"] == 0
    rep, throttle, status = opt._score(m)
    assert rep is None
    assert status == "pending"
    assert throttle == 1.0
