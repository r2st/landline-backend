"""Every send spends the mailbox volume it costs, and the cap sees all of it.

``tests/test_send_volume_coverage.py`` is the mechanical half — every send site
*claims*. This is the behavioural half: that the claim moves the numbers the
warm-up ramp and the daily cap are enforced against, on each of the paths that
used to send for free.

The two counters and how each was blind:

* **the per-account warm-up ramp** is enforced against
  ``GmailAccount.daily_send_count``, which only ``send_outreach_email``
  incremented. So the ramp governed the sequence queue and nothing else, while
  the meeting agent, the smart-reply, the inbox composer and the test endpoint
  sent from the same mailbox without moving the number the ramp reads.
* **the org's daily cap** was reserved only in ``app.tasks.outreach``. A
  template blast went through ``send_outreach_email`` — the function whose
  docstring claims every outreach path funnels through it — one contact at a
  time, past a cap that was never asked.

Refusability is deliberate and asymmetric, which is what most of these pin:
autonomous cold volume is *refused* when the day is spent; a human's reply, an
operator's test and a new colleague's credentials are *never* refused and are
still counted. Cold volume yields to a live conversation; it does not get to
pretend the conversation never happened.
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.meeting_booking import MeetingBookingAgent
from app.models.sender_warmup import SenderWarmupPlan
from app.models.sequence import OutreachSequence
from app.services import email_dispatch, meeting_booking_agent as mba
from app.services import send_limiter as sl
from app.services.send_limiter import get_send_limiter


# ─── fixtures / helpers ──────────────────────────────────────────────────────


class FakeRedis:
    """The atomicity that matters, without a server. See test_send_limit_race."""

    def __init__(self):
        self.store: dict[str, int] = {}
        self.ttl_store: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = int(value)
        if ex is not None:
            self.ttl_store[key] = ex
        return True

    async def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def decr(self, key):
        self.store[key] = self.store.get(key, 0) - 1
        return self.store[key]

    async def ttl(self, key):
        return self.ttl_store.get(key, 3600)

    async def expire(self, key, seconds):
        self.ttl_store[key] = seconds
        return True

    def count(self) -> int:
        return sum(self.store.values())


@pytest.fixture
def fake_redis(monkeypatch):
    client = FakeRedis()

    async def _get():
        return client

    monkeypatch.setattr(sl, "_get_redis", _get)
    return client


@pytest.fixture
def daily_limit(monkeypatch):
    """Set the org's daily cap for a test. Returns a setter."""

    def _set(limit: int):
        async def _limit(self, db):
            return limit

        monkeypatch.setattr(sl.SendLimiter, "_get_daily_limit_db", _limit)

    return _set


async def _mk_account(db: AsyncSession, **overrides) -> GmailAccount:
    fields = {
        "id": uuid.uuid4(), "email": "rep@apprend.co", "google_sub": str(uuid.uuid4()),
        "display_name": "Rep", "refresh_token_encrypted": "x", "status": "connected",
        "sending_enabled": True, "connected_at": datetime.now(timezone.utc),
        **overrides,
    }
    account = GmailAccount(**fields)
    db.add(account)
    await db.flush()
    return account


async def _mk_warmup_plan(db: AsyncSession, account, *, limit: int, day: int = 1):
    plan = SenderWarmupPlan(
        account_id=account.id, account_type="gmail", account_email=account.email,
        start_date=date.today(), current_day=day, daily_limit=limit, status="warming",
    )
    db.add(plan)
    await db.flush()
    return plan


class _SpySender:
    def __init__(self, status: str = "sent"):
        self.calls = []
        self.status = status

    def __call__(self, account, **kwargs):
        self.calls.append(kwargs)
        return {"status": self.status, "gmail_message_id": "m-1", "gmail_thread_id": "t-1"}


# ─── the cap's seed reads both send tables ───────────────────────────────────


@pytest.mark.asyncio
async def test_todays_count_includes_direct_sends_not_just_sequence_events(db_session):
    """An ``EmailSend`` is a send. The seed used to count only ``OutreachEvent``.

    This is the "sent mail lives in two tables" split showing up in the one
    place where undercounting is granting: the number the Redis counter is
    seeded from. Half a day's volume reported as none of it hands the cap back
    allowance the mailbox already spent.
    """
    now = datetime.now(timezone.utc)
    sequence = OutreachSequence(name="seq", is_active=True)
    company = Company(name="Acme", domain="acme.test", source="test")
    db_session.add_all([sequence, company])
    await db_session.flush()
    contact = Contact(email="a@example.com", first_name="A", company_id=company.id)
    db_session.add(contact)
    await db_session.flush()
    enrollment = OutreachEnrollment(
        sequence_id=sequence.id, contact_id=contact.id, status="active", current_step=1
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        enrollment_id=enrollment.id, step_number=1, event_type="sent", occurred_at=now
    ))
    db_session.add(EmailSend(
        to_email="b@example.com", subject="hi", status="sent", sent_at=now
    ))
    # Not a send: a skipped row must not spend the day.
    db_session.add(EmailSend(
        to_email="c@example.com", subject="hi", status="skipped", sent_at=now
    ))
    await db_session.flush()

    assert await get_send_limiter()._db_sent_today(db_session) == 2


@pytest.mark.asyncio
async def test_stats_report_both_tables(db_session):
    """The operator-facing tile divides by the same number the cap does."""
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(to_email="b@x.com", subject="hi", status="sent", sent_at=now))
    await db_session.flush()

    stats = await get_send_limiter().get_send_stats(db_session)
    assert stats["today_sent"] == 1
    assert stats["monthly_sent"] == 1


# ─── consume: counted, never refused ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_counts_past_the_limit_instead_of_refusing(
    db_session, fake_redis, daily_limit
):
    """A non-refusable send depletes the day even when the day is already spent.

    The alternative — decline to count what we could not refuse — is what makes
    a cap drift: the mailbox sends, the counter doesn't move, and the next cold
    send is granted allowance that is gone.
    """
    daily_limit(2)
    limiter = get_send_limiter()

    assert (await limiter.reserve_daily_send(db_session))[0] is True
    assert (await limiter.reserve_daily_send(db_session))[0] is True
    assert (await limiter.reserve_daily_send(db_session))[0] is False

    await limiter.consume_daily_send(db_session)
    assert fake_redis.count() == 3


# ─── the dispatcher claims for every caller but the queue ────────────────────


@pytest.mark.asyncio
async def test_dispatcher_refuses_once_the_daily_cap_is_spent(
    db_session, fake_redis, daily_limit, monkeypatch
):
    """A template blast is not exempt from the cap it never used to ask."""
    daily_limit(1)
    account = await _mk_account(db_session)
    sent = []

    class _Transport:
        def send_email(self, **kwargs):
            sent.append(kwargs)
            return {"status": "sent", "transport": "gmail", "sender": account.email}

    monkeypatch.setattr(email_dispatch, "_make_sender", lambda a: _Transport())

    first = await email_dispatch.send_outreach_email(
        db_session, to_email="a@example.com", subject="s", body_html="<p>hi</p>"
    )
    second = await email_dispatch.send_outreach_email(
        db_session, to_email="b@example.com", subject="s", body_html="<p>hi</p>"
    )

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert second["daily_limit_reached"] is True
    assert len(sent) == 1, "the second message must not reach the transport"


@pytest.mark.asyncio
async def test_queue_does_not_pay_twice(db_session, fake_redis, daily_limit, monkeypatch):
    """``daily_slot_reserved`` is the queue saying "already claimed".

    The queue has to reserve earlier than the dispatcher — ahead of the send
    claim and the pacing window, so all three release together. Double-charging
    it would halve the cap for the path that carries most of the volume.
    """
    daily_limit(10)
    account = await _mk_account(db_session)

    class _Transport:
        def send_email(self, **kwargs):
            return {"status": "sent", "transport": "gmail", "sender": account.email}

    monkeypatch.setattr(email_dispatch, "_make_sender", lambda a: _Transport())

    await email_dispatch.send_outreach_email(
        db_session, to_email="a@example.com", subject="s", body_html="<p>hi</p>",
        daily_slot_reserved=True,
    )
    assert fake_redis.count() == 0


@pytest.mark.asyncio
async def test_a_send_that_never_lands_hands_its_slot_back(
    db_session, fake_redis, daily_limit, monkeypatch
):
    daily_limit(5)
    account = await _mk_account(db_session)

    class _Transport:
        def send_email(self, **kwargs):
            return {"status": "error", "detail": "smtp down"}

    monkeypatch.setattr(email_dispatch, "_make_sender", lambda a: _Transport())

    await email_dispatch.send_outreach_email(
        db_session, to_email="a@example.com", subject="s", body_html="<p>hi</p>"
    )
    assert fake_redis.count() == 0
    assert (account.daily_send_count or 0) == 0


@pytest.mark.asyncio
async def test_a_transport_exception_hands_its_slot_back(
    db_session, fake_redis, daily_limit, monkeypatch
):
    """A flapping host must not eat the day's allowance without sending."""
    daily_limit(5)
    account = await _mk_account(db_session)

    class _Transport:
        def send_email(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(email_dispatch, "_make_sender", lambda a: _Transport())

    with pytest.raises(RuntimeError):
        await email_dispatch.send_outreach_email(
            db_session, to_email="a@example.com", subject="s", body_html="<p>hi</p>"
        )
    assert fake_redis.count() == 0




@pytest.mark.asyncio
async def test_the_ses_fallback_is_capped_too(db_session, fake_redis, daily_limit, monkeypatch):
    """No connected mailbox is not a licence to send without a limit.

    SES is reached only when the org has no Gmail/SMTP account at all, so there
    is no per-account ramp to check — which is exactly why the org-level cap has
    to hold there. Skipping it would leave the one configuration with no
    per-account limits as the one with no limits.
    """
    daily_limit(1)
    sent = []

    class _Ses:
        sender = "ses@apprend.co"

        def send_email(self, **kwargs):
            sent.append(kwargs)
            return {"status": "sent"}

    monkeypatch.setattr(email_dispatch, "EmailSender", _Ses)

    first = await email_dispatch.send_outreach_email(
        db_session, to_email="a@example.com", subject="s", body_html="<p>hi</p>"
    )
    second = await email_dispatch.send_outreach_email(
        db_session, to_email="b@example.com", subject="s", body_html="<p>hi</p>"
    )

    assert first["status"] == "sent"
    assert second["status"] == "skipped" and second["daily_limit_reached"] is True
    assert len(sent) == 1


# ─── the per-account counter ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_account_send_rolls_over_at_the_utc_boundary(db_session):
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    account = await _mk_account(
        db_session, daily_send_count=17, daily_count_reset_at=yesterday
    )

    await email_dispatch.record_account_send(db_session, account, sent=True)

    assert account.daily_send_count == 1, "yesterday's count must not carry over"


@pytest.mark.asyncio
async def test_record_account_send_ignores_an_attempt_that_did_not_land(db_session):
    account = await _mk_account(db_session)
    await email_dispatch.record_account_send(db_session, account, sent=False)
    assert (account.daily_send_count or 0) == 0
    assert account.last_used_at is not None, "rotation still saw an attempt"


# ─── the meeting agent: refusable, and counted ───────────────────────────────


@pytest.mark.asyncio
async def test_meeting_agent_send_counts_against_the_mailbox(
    db_session, fake_redis, daily_limit
):
    """The proposal is autonomous cold volume out of the sequence mailbox."""
    daily_limit(50)
    account = await _mk_account(db_session)
    spy = _SpySender()

    ok, _ = await mba._guarded_send(
        db_session, spy, account,
        to_email="lead@example.com", kind="proposal", subject="s", body_html="<p>hi</p>",
    )

    assert ok is True and len(spy.calls) == 1
    assert account.daily_send_count == 1
    assert fake_redis.count() == 1


@pytest.mark.asyncio
async def test_meeting_agent_records_what_it_sent(db_session, fake_redis, daily_limit):
    """This agent wrote to *neither* send table, which cost three things.

    The unified inbox showed the prospect's "Tuesday works" as a thread with no
    opening message. ``_is_outreach_reply`` — whose three tests all read
    ``EmailSend`` or an enrollment — dropped that reply as inbox noise when the
    proposal opened a fresh thread, so the booking sweep polled ``EmailReply``
    for a row nothing had written: the agent's own loop depended on the record
    it wasn't keeping. And the daily cap's seed, which counts rows, forgot the
    send at the next reseed.
    """
    from sqlalchemy import select as sa_select

    daily_limit(50)
    account = await _mk_account(db_session)
    company = Company(name="Acme", domain="acme.test", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(email="lead@example.com", first_name="Lee", company_id=company.id)
    db_session.add(contact)
    await db_session.flush()
    spy = _SpySender()

    ok, _ = await mba._guarded_send(
        db_session, spy, account,
        to_email="lead@example.com", kind="proposal", contact_id=contact.id,
        subject="Let's find a time", body_html="<p>Tue or Wed?</p>",
    )
    await db_session.flush()

    assert ok is True
    row = (await db_session.execute(sa_select(EmailSend))).scalars().one()
    assert row.to_email == "lead@example.com"
    assert row.contact_id == contact.id
    assert row.subject == "Let's find a time"
    assert row.body_html == "<p>Tue or Wed?</p>"
    assert row.gmail_thread_id == "t-1", "the thread the reply will arrive on"
    assert row.gmail_message_id == "m-1"
    assert row.sender_account_id == account.id
    assert row.status == "sent"


@pytest.mark.asyncio
async def test_meeting_agent_records_nothing_when_the_send_did_not_go(
    db_session, fake_redis, daily_limit
):
    """A row claiming a sent message is worse than no row at all."""
    from sqlalchemy import func, select as sa_select

    daily_limit(1)
    account = await _mk_account(db_session)
    await get_send_limiter().reserve_daily_send(db_session)  # the day is spent

    ok, _ = await mba._guarded_send(
        db_session, _SpySender(), account,
        to_email="lead@example.com", kind="proposal", subject="s", body_html="<p>hi</p>",
    )
    await db_session.flush()

    assert ok is False
    assert (await db_session.execute(sa_select(func.count(EmailSend.id)))).scalar() == 0


@pytest.mark.asyncio
async def test_meeting_agent_is_held_by_the_warmup_ramp(db_session, fake_redis, daily_limit):
    """The exact failure: a day-1 mailbox at its ramp still proposing meetings.

    The queue stops at 5/5 because the ramp is enforced there. This path used to
    keep going — same mailbox, same cold IP, no number moving.
    """
    daily_limit(50)
    account = await _mk_account(db_session, daily_send_count=5,
                                daily_count_reset_at=datetime.now(timezone.utc))
    await _mk_warmup_plan(db_session, account, limit=5)
    spy = _SpySender()

    ok, _ = await mba._guarded_send(
        db_session, spy, account,
        to_email="lead@example.com", kind="proposal", subject="s", body_html="<p>hi</p>",
    )

    assert ok is False
    assert spy.calls == [], "nothing may reach the transport"


@pytest.mark.asyncio
async def test_meeting_agent_is_held_by_the_daily_cap(db_session, fake_redis, daily_limit):
    daily_limit(1)
    account = await _mk_account(db_session)
    await get_send_limiter().reserve_daily_send(db_session)  # the day is spent
    spy = _SpySender()

    ok, _ = await mba._guarded_send(
        db_session, spy, account,
        to_email="lead@example.com", kind="reminder", subject="s", body_html="<p>hi</p>",
    )

    assert ok is False and spy.calls == []


@pytest.mark.asyncio
async def test_a_confirmation_is_counted_but_never_refused(db_session, fake_redis, daily_limit):
    """A prospect who just picked a slot gets told, cap or no cap.

    Suppression binds all four of this agent's sends because it is about
    consent. The volume caps are operational, so they bind cold volume and not
    the second half of a conversation the recipient started — but the wire does
    not care what we called it, so it still counts.
    """
    daily_limit(1)
    account = await _mk_account(db_session)
    await get_send_limiter().reserve_daily_send(db_session)  # the day is spent
    spy = _SpySender()

    ok, _ = await mba._guarded_send(
        db_session, spy, account,
        to_email="lead@example.com", kind="confirmation", refusable=False,
        subject="s", body_html="<p>hi</p>",
    )

    assert ok is True and len(spy.calls) == 1
    assert account.daily_send_count == 1
    assert fake_redis.count() == 2


@pytest.mark.asyncio
async def test_a_refused_proposal_records_no_meeting(db_session, fake_redis, daily_limit):
    """A ``proposed`` row for a message nobody got is polled forever.

    ``propose_meeting`` already refused to record one when the *compliance*
    gate blocked the mail. The volume caps decline routinely rather than
    exceptionally, so the same has to hold for them — otherwise every capped
    day leaves behind meetings the 15-minute sweep will keep checking for a
    reply to a proposal that was never sent.
    """
    from app.models.meeting_booking import BookedMeeting
    from sqlalchemy import func, select as sa_select

    daily_limit(1)
    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(
        is_active=True, calendar_account_id=account.id, timezone="UTC",
        booking_window_days=7,
    )
    db_session.add(agent)
    await db_session.flush()
    await get_send_limiter().reserve_daily_send(db_session)  # the day is spent

    company = Company(name="Acme", domain="acme.test", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(email="lead@example.com", first_name="Lee", company_id=company.id)
    db_session.add(contact)
    await db_session.flush()

    spy = _SpySender()

    async def _free_busy(account, *, start, end):
        return []

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=contact, reply=None,
        now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        busy_provider=_free_busy, send_email=spy,
    )

    assert meeting is None
    assert spy.calls == []
    rows = (await db_session.execute(sa_select(func.count(BookedMeeting.id)))).scalar()
    assert rows == 0, "no meeting may be left behind for the sweep to poll"


@pytest.mark.asyncio
async def test_meeting_agent_returns_its_slot_when_the_transport_raises(
    db_session, fake_redis, daily_limit
):
    daily_limit(50)
    account = await _mk_account(db_session)

    def _explode(account, **kwargs):
        raise RuntimeError("token expired")

    ok, _ = await mba._guarded_send(
        db_session, _explode, account,
        to_email="lead@example.com", kind="proposal", subject="s", body_html="<p>hi</p>",
    )

    assert ok is False
    assert fake_redis.count() == 0
    assert (account.daily_send_count or 0) == 0


# ─── non-refusable paths: never blocked, always counted ──────────────────────


@pytest.mark.asyncio
async def test_a_human_reply_is_never_refused_by_the_cap(
    db_session, fake_redis, daily_limit, monkeypatch
):
    """The cap protects a domain from cold volume. A reply is not cold volume.

    Holding a person's reply to someone who wrote to *them* because a cold
    sequence spent the day would be a worse failure than the cap prevents — so
    the reply goes, and the day is depleted by it, which is cold volume yielding
    to a live conversation rather than the conversation going unrecorded.
    """
    from app.services.email_history import EmailHistoryService

    daily_limit(1)
    account = await _mk_account(db_session)
    await get_send_limiter().reserve_daily_send(db_session)  # the day is spent
    assert fake_redis.count() == 1

    sent = []

    class _Sender:
        def __init__(self, account):
            pass

        def send_email(self, **kwargs):
            sent.append(kwargs)
            return {"status": "sent", "gmail_message_id": "m-9", "gmail_thread_id": "t-9"}

    monkeypatch.setattr("app.services.email_history.GmailSender", _Sender)

    original = EmailSend(
        to_email="lead@example.com", subject="hello", status="sent",
        sent_at=datetime.now(timezone.utc), gmail_thread_id="t-9",
        gmail_message_id="m-1", sender_account_id=account.id,
    )
    db_session.add(original)
    await db_session.flush()

    result = await EmailHistoryService().send_reply(
        db_session, email_id=original.id,
        body_html="<p>sure</p>", body_text="sure",
    )

    assert result["status"] == "sent", "a human reply must not be capped"
    assert len(sent) == 1
    assert account.daily_send_count == 1, "and it still costs the mailbox"
    assert fake_redis.count() == 2, "and still depletes the day"


@pytest.mark.asyncio
async def test_a_test_send_costs_the_mailbox(db_session, fake_redis, daily_limit, monkeypatch):
    """Two "tests" on a day-1 plan are 40% of the day. They used to be 0%."""
    daily_limit(50)
    account = await _mk_account(db_session)

    slot = await email_dispatch.claim_send_volume(db_session, account, refusable=False)
    assert slot.blocked is None, "a test is never refused"
    await email_dispatch.settle_send_volume(db_session, slot, sent=True)
    await email_dispatch.record_account_send(db_session, account, sent=True)

    assert account.daily_send_count == 1
    assert fake_redis.count() == 1


@pytest.mark.asyncio
async def test_a_transport_error_is_reported_as_a_send_that_did_not_go(
    db_session, fake_redis, daily_limit
):
    """``GmailSender`` **returns** ``{"status": "error"}``; it does not raise.

    An expired refresh token and a 4xx from the Gmail API both come back as an
    error dict, so ``_guarded_send``'s ``except`` never sees the most common way
    a send fails. Reporting that as ok put the whole class back where it
    started: the accounting was right — slot released, no ``EmailSend`` row —
    and the caller was told the mail went, which is the only half
    ``propose_meeting`` reads.
    """
    daily_limit(50)
    account = await _mk_account(db_session)

    ok, result = await mba._guarded_send(
        db_session, _SpySender(status="error"), account,
        to_email="lead@example.com", kind="proposal", subject="s", body_html="<p>hi</p>",
    )

    assert ok is False
    assert result["status"] == "error"
    assert fake_redis.count() == 0, "a failed send must not spend the day"
    assert (account.daily_send_count or 0) == 0


@pytest.mark.asyncio
async def test_a_proposal_the_transport_rejected_records_no_meeting(
    db_session, fake_redis, daily_limit
):
    """The refused-proposal rule has to hold for a *failed* send too.

    Same end state as ``test_a_refused_proposal_records_no_meeting``, reached
    the other way: there the cap declined before the wire, here the wire
    declined. Both leave a ``proposed`` row the 15-minute sweep polls forever
    for a reply to a message nobody received.
    """
    from app.models.meeting_booking import BookedMeeting
    from sqlalchemy import func, select as sa_select

    daily_limit(50)
    account = await _mk_account(db_session)
    agent = MeetingBookingAgent(
        is_active=True, calendar_account_id=account.id, timezone="UTC",
        booking_window_days=7,
    )
    db_session.add(agent)
    company = Company(name="Acme", domain="acme.test", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(email="lead@example.com", first_name="Lee", company_id=company.id)
    db_session.add(contact)
    await db_session.flush()

    spy = _SpySender(status="error")

    async def _free_busy(account, *, start, end):
        return []

    meeting = await mba.propose_meeting(
        db_session, agent=agent, contact=contact, reply=None,
        now=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        busy_provider=_free_busy, send_email=spy,
    )
    await db_session.flush()

    assert spy.calls, "the transport was reached — this is a wire failure, not a refusal"
    assert meeting is None
    assert (
        await db_session.execute(sa_select(func.count(BookedMeeting.id)))
    ).scalar() == 0
    assert (
        await db_session.execute(sa_select(func.count(EmailSend.id)))
    ).scalar() == 0


@pytest.mark.asyncio
async def test_a_non_refusable_send_that_fails_costs_nothing(
    db_session, fake_redis, daily_limit
):
    daily_limit(50)
    account = await _mk_account(db_session)

    slot = await email_dispatch.claim_send_volume(db_session, account, refusable=False)
    await email_dispatch.settle_send_volume(db_session, slot, sent=False)

    assert fake_redis.count() == 0
    assert (account.daily_send_count or 0) == 0
