"""The danger rail must see Dialtone's own traffic.

The rail existed to restrain an autonomous cold-email agent and could not see a
single thing that agent did. It counted only ``EmailSend``; the sequence path
writes no EmailSend row (``tasks/outreach.py:562``), so both the numerator and
the denominator were zero for every unattended send. 100% of sends could hard
bounce and ``evaluate()`` would return ``healthy``.

``test_total_bounce_catastrophe_trips_the_breaker`` is the acceptance test for
that: it is the exact scenario from DIALTONE_ROADMAP.md P0-1, with the
assertions inverted. It failed before this change and must never fail again.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.danger_monitor import get_danger_monitor


async def _company(db: AsyncSession, name="Doomed Co") -> Company:
    c = Company(name=name, country="US", source="test")
    db.add(c)
    await db.flush()
    return c


async def _contact(db: AsyncSession, company: Company, email: str, bounce_type=None) -> Contact:
    c = Contact(
        company_id=company.id, first_name="D", email=email, source="test",
        last_bounce_type=bounce_type,
    )
    db.add(c)
    await db.flush()
    return c


async def _sequence(db: AsyncSession) -> OutreachSequence:
    s = OutreachSequence(id=uuid.uuid4(), name="Cold", is_active=True)
    db.add(s)
    await db.flush()
    return s


async def _account(db: AsyncSession, email="sender@apprend.tech") -> GmailAccount:
    a = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=f"sub-{uuid.uuid4()}",
        refresh_token_encrypted="x", is_active=True,
    )
    db.add(a)
    await db.flush()
    return a


async def _sequence_send(
    db: AsyncSession, seq, contact, *, bounced: bool, account=None, when=None
) -> OutreachEvent:
    """One Dialtone autonomous send, recorded the way tasks/outreach.py does."""
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
        sender_account_id=account.id if account else None,
    )
    db.add(enr)
    await db.flush()
    ev = OutreachEvent(
        enrollment_id=enr.id,
        step_number=1,
        event_type="bounced" if bounced else "sent",
        occurred_at=when or datetime.now(timezone.utc),
    )
    db.add(ev)
    await db.flush()
    return ev


# --- The acceptance test -------------------------------------------------


@pytest.mark.asyncio
async def test_total_bounce_catastrophe_trips_the_breaker(db_session: AsyncSession):
    """100 autonomous sends, every one a hard bounce → the breaker must trip.

    Verbatim the scenario documented as P0-1, which produced
    ``overall_status=healthy, breach=False`` and all three metrics at 0.0.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(100):
        contact = await _contact(db_session, company, f"dead{i}@gone-corp.com", bounce_type="hard")
        await _sequence_send(db_session, seq, contact, bounced=True)
    await db_session.commit()

    report = await get_danger_monitor().evaluate(db_session)

    assert report.breach is True, "100% hard bounce must trip the breaker"
    assert report.overall_status == "breach"

    by_metric = {m.metric: m for m in report.metrics if m.scope == "global"}
    assert by_metric["bounce_rate"].observed_value == 1.0
    assert by_metric["bounce_rate"].status == "breach"
    assert by_metric["hard_bounce_count"].observed_value == 100
    assert by_metric["hard_bounce_count"].status == "breach"


@pytest.mark.asyncio
async def test_healthy_sequence_traffic_does_not_trip(db_session: AsyncSession):
    """The mirror image: the rail must not cry wolf on a clean run."""
    company = await _company(db_session, "Good Co")
    seq = await _sequence(db_session)
    for i in range(100):
        contact = await _contact(db_session, company, f"live{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    await db_session.commit()

    report = await get_danger_monitor().evaluate(db_session)

    assert report.breach is False
    assert report.overall_status == "healthy"
    by_metric = {m.metric: m for m in report.metrics if m.scope == "global"}
    assert by_metric["bounce_rate"].observed_value == 0.0
    assert by_metric["bounce_rate"].sample_size == 100


# --- The populations ------------------------------------------------------


@pytest.mark.asyncio
async def test_sequence_sends_count_toward_the_denominator(db_session: AsyncSession):
    """The agent's sends were invisible, so every rate divided by zero-ish.

    A rate needs a denominator: without this the sample never reaches
    min_sample_size and `_rate_status` returns pass unconditionally.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(60):
        contact = await _contact(db_session, company, f"c{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 60
    assert m["sources"]["sent_sequence"] == 60
    assert m["sources"]["sent_email"] == 0


@pytest.mark.asyncio
async def test_async_dsn_bounces_count(db_session: AsyncSession):
    """Gmail accepts, then reports the failure later.

    Most real hard bounces arrive this way, never as a synchronous send
    failure — so a rail that only counted OutreachEvent('bounced') would still
    miss the majority of them.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(100):
        contact = await _contact(db_session, company, f"acc{i}@acme.com", bounce_type="hard")
        await _sequence_send(db_session, seq, contact, bounced=False)  # accepted
    # ...then 10 DSNs land in the mailbox and the sweep records them.
    for i in range(10):
        db_session.add(BounceRecord(
            gmail_message_id=f"dsn-{i}", recipient_email=f"acc{i}@acme.com",
            bounce_kind="hard", status_code="5.1.1", diagnostic="no such user",
        ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 100
    assert m["bounces"] == 10
    assert m["sources"]["bounces_dsn"] == 10
    assert m["bounce_rate"] == 0.1
    assert m["hard_bounces"] == 10


@pytest.mark.asyncio
async def test_manual_campaign_sends_still_counted(db_session: AsyncSession):
    """Watching only Dialtone would re-blind the rail to manual campaigns.

    A bounce burns the sending domain identically whoever queued it.
    """
    company = await _company(db_session)
    now = datetime.now(timezone.utc)
    for i in range(50):
        contact = await _contact(db_session, company, f"m{i}@acme.com")
        db_session.add(EmailSend(
            contact_id=contact.id, to_email=contact.email, subject="hi",
            status="sent", sent_at=now,
        ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 50
    assert m["sources"]["sent_email"] == 50


@pytest.mark.asyncio
async def test_both_populations_are_unioned(db_session: AsyncSession):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    now = datetime.now(timezone.utc)
    for i in range(30):
        contact = await _contact(db_session, company, f"s{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    for i in range(20):
        contact = await _contact(db_session, company, f"e{i}@acme.com")
        db_session.add(EmailSend(
            contact_id=contact.id, to_email=contact.email, subject="hi",
            status="sent", sent_at=now,
        ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 50
    assert m["sources"] == {
        "sent_sequence": 30, "sent_email": 20,
        "bounces_sequence": 0, "bounces_email": 0, "bounces_dsn": 0,
        "complaints_email": 0, "complaints_fbl": 0,
    }


@pytest.mark.asyncio
async def test_a_dsn_bounce_is_not_double_counted_with_a_send_failure(db_session: AsyncSession):
    """The two bounce kinds are mutually exclusive per send.

    A synchronous failure means the message was never accepted, so no DSN can
    follow it. Guards the union against inflating the rate — a rail that
    over-counts pauses a healthy agent.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(98):
        contact = await _contact(db_session, company, f"ok{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    # One synchronous failure (never accepted → no DSN).
    sync_contact = await _contact(db_session, company, "sync@acme.com", bounce_type="hard")
    await _sequence_send(db_session, seq, sync_contact, bounced=True)
    # One accepted send that later bounced via DSN.
    async_contact = await _contact(db_session, company, "async@acme.com", bounce_type="hard")
    await _sequence_send(db_session, seq, async_contact, bounced=False)
    db_session.add(BounceRecord(
        gmail_message_id="dsn-solo", recipient_email="async@acme.com",
        bounce_kind="hard", status_code="5.1.1", diagnostic="no such user",
    ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    # 100 attempts: 98 clean + 1 rejected at SMTP time + 1 accepted-then-DSN.
    # The denominator counts attempts, so the synchronous failure is in it —
    # it was an email we tried to send.
    assert m["sent"] == 100
    assert m["bounces"] == 2  # exactly one of each kind, counted once each
    assert m["sources"]["bounces_sequence"] == 1
    assert m["sources"]["bounces_dsn"] == 1
    assert m["bounce_rate"] == 0.02


@pytest.mark.asyncio
async def test_an_async_bounce_is_not_a_second_attempt(db_session: AsyncSession):
    """The DSN adds a row to a send that already logged one; not a new attempt.

    ``bounce_handler._suppress_contact`` books its ``bounced`` OutreachEvent
    against the step that sent — the same ``(enrollment_id, step_number)`` the
    ``sent`` event carries. Summing the two event types therefore counted the
    same message twice, and the denominator grew by exactly the numerator: ten
    sends with one bounce read 1/11 = 9.1%. Against a 10% threshold that is the
    rail declining to pause a domain that has already crossed it, which is the
    one direction a brake must not err in.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    bounced_event = None
    for i in range(10):
        contact = await _contact(db_session, company, f"lead{i}@acme.com")
        ev = await _sequence_send(db_session, seq, contact, bounced=False)
        if i == 0:
            bounced_event = ev
    # The DSN lands hours later, on the step that sent.
    db_session.add(OutreachEvent(
        enrollment_id=bounced_event.enrollment_id,
        step_number=bounced_event.step_number,
        event_type="bounced",
        occurred_at=datetime.now(timezone.utc),
        event_metadata={"source": "bounce_handler"},
    ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 10, "the bounce is the tenth send, not an eleventh"
    assert m["bounces"] == 1
    assert m["bounce_rate"] == 0.1


@pytest.mark.asyncio
async def test_a_redelivered_dsn_is_still_one_bounce(db_session: AsyncSession):
    """Mailer-daemons retry. Two rows about one failed send are one bounce."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    first = None
    for i in range(10):
        contact = await _contact(db_session, company, f"dup{i}@acme.com")
        ev = await _sequence_send(db_session, seq, contact, bounced=False)
        if i == 0:
            first = ev
    for _ in range(3):
        db_session.add(OutreachEvent(
            enrollment_id=first.enrollment_id,
            step_number=first.step_number,
            event_type="bounced",
            occurred_at=datetime.now(timezone.utc),
            event_metadata={"source": "bounce_handler"},
        ))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["sent"] == 10
    assert m["bounces"] == 1
    assert m["bounce_rate"] == 0.1


@pytest.mark.asyncio
async def test_the_window_excludes_old_traffic(db_session: AsyncSession):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    old = datetime.now(timezone.utc) - timedelta(hours=48)
    for i in range(10):
        contact = await _contact(db_session, company, f"old{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=True, when=old)
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session, hours=24)
    assert m["sent"] == 0
    assert m["bounces"] == 0


@pytest.mark.asyncio
async def test_per_account_scoping_isolates_a_bad_mailbox(db_session: AsyncSession):
    """One burning mailbox must be attributable to itself.

    The rail pauses per-account as well as globally; a bad sender hidden inside
    a healthy global average is exactly what per-account scope is for.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    good = await _account(db_session, "good@apprend.tech")
    bad = await _account(db_session, "bad@apprend.tech")

    for i in range(50):
        contact = await _contact(db_session, company, f"g{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False, account=good)
    for i in range(50):
        contact = await _contact(db_session, company, f"b{i}@acme.com", bounce_type="hard")
        await _sequence_send(db_session, seq, contact, bounced=True, account=bad)
    await db_session.commit()

    good_m = await get_danger_monitor()._window_metrics(db_session, account=good)
    bad_m = await get_danger_monitor()._window_metrics(db_session, account=bad)

    assert good_m["bounce_rate"] == 0.0
    assert bad_m["bounce_rate"] == 1.0
    # And the global view sees both.
    all_m = await get_danger_monitor()._window_metrics(db_session)
    assert all_m["sent"] == 100
    assert all_m["bounce_rate"] == 0.5


@pytest.mark.asyncio
async def test_dsn_bounces_scope_to_their_account(db_session: AsyncSession):
    company = await _company(db_session)
    seq = await _sequence(db_session)
    good = await _account(db_session, "good2@apprend.tech")
    bad = await _account(db_session, "bad2@apprend.tech")
    for i in range(50):
        contact = await _contact(db_session, company, f"gg{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False, account=good)
    for i in range(50):
        contact = await _contact(db_session, company, f"bb{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False, account=bad)
    for i in range(25):
        db_session.add(BounceRecord(
            gmail_message_id=f"dsn-bad-{i}", gmail_account_id=bad.id,
            recipient_email=f"bb{i}@acme.com", bounce_kind="hard",
        ))
    await db_session.commit()

    assert (await get_danger_monitor()._window_metrics(db_session, account=good))["bounces"] == 0
    assert (await get_danger_monitor()._window_metrics(db_session, account=bad))["bounces"] == 25


@pytest.mark.asyncio
async def test_a_small_sample_still_never_trips_on_rate(db_session: AsyncSession):
    """Regression guard on the min-sample rule.

    Now that the denominator is real, the sample gate finally does something —
    it must still hold, or the first bounce of a fresh warm-up pauses everything.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(3):
        contact = await _contact(db_session, company, f"tiny{i}@acme.com", bounce_type="hard")
        await _sequence_send(db_session, seq, contact, bounced=True)
    await db_session.commit()

    report = await get_danger_monitor().evaluate(db_session)
    by_metric = {m.metric: m for m in report.metrics if m.scope == "global"}
    # 100% bounce rate, but on 3 sends — too noisy to act on.
    assert by_metric["bounce_rate"].observed_value == 1.0
    assert by_metric["bounce_rate"].status == "pass"


# ---------------------------------------------------------------------------
# The complaint half of the rail.
#
# `bounce_rate` and `hard_bounce_count` were fixed when the populations were
# unioned. `complaint_rate` was left structurally 0.0 — `_COMPLAINT_STATUSES`
# names `EmailSend.status` values nothing writes, so the numerator was a
# constant and the metric passed unconditionally.
#
# What makes it worth a test rather than a note: the ARF/FBL sweep has been
# ingesting the real reports into `complaint_records` on a schedule since, and
# nothing read that table. The rail was blind *next to the data*, which is the
# failure that survives longest — reports arrive at one end, the dashboard has a
# complaint row at the other, and only the number joining them is fake.
#
# It is also the threshold that matters most: 0.1%, where bounce trips at 5%,
# because a complaint rate is what gets a sending domain blocklisted, and a
# blocklisting is not something pausing afterwards undoes.
# ---------------------------------------------------------------------------


async def _complaint(db: AsyncSession, i: int, *, account=None, when=None):
    from app.models.complaint_record import ComplaintRecord

    row = ComplaintRecord(
        gmail_message_id=f"fbl-{uuid.uuid4()}",
        gmail_account_id=account.id if account else None,
        recipient_email=f"angry{i}@acme.com",
        feedback_type="abuse",
    )
    if when is not None:
        row.processed_at = when
    db.add(row)
    return row


@pytest.mark.asyncio
async def test_ingested_complaints_trip_the_rail(db_session: AsyncSession):
    """100 clean sends and 3 people reporting us as spam is a 3% complaint rate.

    Thirty times the breach threshold. Before this, the same scenario reported
    `complaint_rate 0.0 — pass`, because the only numerator the rail knew about
    was a column with no writer.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(100):
        contact = await _contact(db_session, company, f"c{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    for i in range(3):
        await _complaint(db_session, i)
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["complaints"] == 3
    assert m["complaint_rate"] == 0.03
    assert m["sources"]["complaints_fbl"] == 3

    report = await get_danger_monitor().evaluate(db_session)
    by_metric = {x.metric: x for x in report.metrics if x.scope == "global"}
    assert by_metric["complaint_rate"].status == "breach"
    assert report.breach is True


@pytest.mark.asyncio
async def test_a_clean_sender_still_reads_clean(db_session: AsyncSession):
    """The other half: no complaints must still mean no complaints.

    A numerator that finally moves is only an improvement if it can also stay
    still — otherwise the rail has swapped a metric that never fires for one
    that always does, and an operator learns to ignore it either way.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    for i in range(100):
        contact = await _contact(db_session, company, f"ok{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    await db_session.commit()

    report = await get_danger_monitor().evaluate(db_session)
    by_metric = {x.metric: x for x in report.metrics if x.scope == "global"}
    assert by_metric["complaint_rate"].observed_value == 0.0
    assert by_metric["complaint_rate"].status == "pass"
    assert report.breach is False


@pytest.mark.asyncio
async def test_complaints_scope_to_their_account(db_session: AsyncSession):
    """A per-account pause must land on the account that earned it.

    Pausing the wrong sender leaves the complaining one running and takes a
    healthy one offline — strictly worse than not pausing at all.
    """
    company = await _company(db_session)
    seq = await _sequence(db_session)
    good = await _account(db_session, "clean@apprend.tech")
    bad = await _account(db_session, "reported@apprend.tech")
    for label, account in (("g", good), ("b", bad)):
        for i in range(50):
            contact = await _contact(db_session, company, f"{label}c{i}@acme.com")
            await _sequence_send(db_session, seq, contact, bounced=False, account=account)
    for i in range(5):
        await _complaint(db_session, i, account=bad)
    await db_session.commit()

    monitor = get_danger_monitor()
    assert (await monitor._window_metrics(db_session, account=good))["complaints"] == 0
    assert (await monitor._window_metrics(db_session, account=bad))["complaints"] == 5


@pytest.mark.asyncio
async def test_complaints_outside_the_window_are_not_counted(db_session: AsyncSession):
    """The rail is a 24h view; last month's complaints are not today's rate."""
    company = await _company(db_session)
    seq = await _sequence(db_session)
    now = datetime.now(timezone.utc)
    for i in range(100):
        contact = await _contact(db_session, company, f"w{i}@acme.com")
        await _sequence_send(db_session, seq, contact, bounced=False)
    for i in range(9):
        await _complaint(db_session, i, when=now - timedelta(days=30))
    await db_session.commit()

    m = await get_danger_monitor()._window_metrics(db_session)
    assert m["complaints"] == 0
