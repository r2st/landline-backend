"""One DSN, two populations, one sum — the throttle counted it twice.

``bounce_ingestion`` writes a ``BounceRecord`` and then calls ``record_bounce``,
which books a ``bounced`` ``OutreachEvent`` against the enrollment that sent. So
an ingested DSN that reached a live sequence — the ordinary case for an
autonomous outreach agent — exists in **both** places by design, and
``BounceRecord.funnel_events`` is the field that says so.

``danger_monitor._window_metrics`` already filters on it and its comment
explains exactly what happens without one: "every ingested DSN that hit a live
sequence was worth two bounces, and the effective auto-pause threshold was half
the configured one".

``deliverability_optimizer._account_metrics`` summed the same two populations
with no such filter. Same overlap, one module over, and the consequence is
harsher than a wrong badge: doubling the numerator doubles ``bounce_rate_7d``,
which ``_score`` compares against ``sender_bounce_rate_throttle`` and which
``throttle_factor`` multiplies straight into the daily cap in
``send_limiter.account_capacity``.

The bias is the part worth remembering. Only DSNs that *hit a live sequence* are
double-counted, so the overcount scales with how much sequence mail the mailbox
sends — the reputation system cut hardest at the sender doing the most of the
work it exists to protect, and the symptom ("why is Dialtone barely sending?")
points nowhere near sender health.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.deliverability_optimizer import DeliverabilityOptimizer


async def _fixtures(db: AsyncSession):
    company = Company(name="Prospect Co", country="US", source="test")
    db.add(company)
    sequence = OutreachSequence(id=uuid.uuid4(), name="Cold", is_active=True)
    db.add(sequence)
    account = GmailAccount(
        id=uuid.uuid4(), email="agent@apprend.tech",
        google_sub=f"sub-{uuid.uuid4()}", refresh_token_encrypted="x",
        status="connected",
    )
    db.add(account)
    await db.flush()
    return company, sequence, account


async def _sequence_send(
    db, sequence, company, account, email, *, bounced=False, opened=False
):
    """One autonomous send, recorded the way ``tasks/outreach.py`` records it."""
    contact = Contact(company_id=company.id, first_name="P", email=email, source="test")
    db.add(contact)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        status="active", sender_account_id=account.id,
    )
    db.add(enrollment)
    await db.flush()
    db.add(OutreachEvent(
        enrollment_id=enrollment.id, step_number=1, event_type="sent",
        occurred_at=datetime.now(timezone.utc),
    ))
    if bounced:
        # What ``bounce_handler._suppress_contact`` books on the funnel: a
        # ``bounced`` event on the step that was *already* recorded as sent,
        # hours earlier. Both rows describe one attempt.
        db.add(OutreachEvent(
            enrollment_id=enrollment.id, step_number=1, event_type="bounced",
            occurred_at=datetime.now(timezone.utc),
        ))
    if opened:
        # Kept above the 5% floor ``_score`` treats as a soft inboxing warning,
        # so these tests measure the bounce arithmetic and not that heuristic.
        db.add(OutreachEvent(
            enrollment_id=enrollment.id, step_number=1, event_type="opened",
            occurred_at=datetime.now(timezone.utc),
        ))
    await db.flush()
    return enrollment


def _ingested(account, email, *, funnel_events):
    """The ledger row ``bounce_ingestion`` writes for the same DSN.

    ``funnel_events`` is what ``record_bounce`` returned — non-zero means the
    bounce was also booked against a live enrollment.
    """
    return BounceRecord(
        gmail_message_id=f"dsn-{uuid.uuid4().hex}",
        gmail_account_id=account.id,
        recipient_email=email,
        bounce_kind="hard",
        funnel_events=funnel_events,
    )


@pytest.mark.asyncio
async def test_a_dsn_that_reached_a_sequence_is_counted_once(db_session: AsyncSession):
    """100 sends, 4 real bounces. It measured 8, i.e. 8%, and throttled.

    Every one of these DSNs stopped a live enrollment, so every one of them is
    in both populations. ``sender_bounce_rate_throttle`` sits at 5%: the true
    4% passes and the doubled 8% does not, which is the whole distance between
    "healthy" and a 60% cut to the mailbox's daily cap.
    """
    company, sequence, account = await _fixtures(db_session)
    for i in range(96):
        await _sequence_send(
            db_session, sequence, company, account, f"ok{i}@acme.com", opened=i < 30
        )
    for i in range(4):
        email = f"dead{i}@acme.com"
        await _sequence_send(
            db_session, sequence, company, account, email, bounced=True
        )
        db_session.add(_ingested(account, email, funnel_events=1))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)

    assert m["sent_7d"] == 100
    assert m["bounces_7d"] == 4, "one DSN, one bounce"
    assert m["sources"]["bounces_sequence"] == 4
    assert m["sources"]["bounces_dsn"] == 0, "already held by the funnel population"
    assert m["bounce_rate_7d"] == 0.04

    _rep, throttle, status = opt._score(m)
    assert throttle == 1.0
    assert status == "healthy"


@pytest.mark.asyncio
async def test_a_dsn_that_reached_no_sequence_is_still_counted(db_session: AsyncSession):
    """The half that must keep counting, or the filter becomes a blindfold.

    A bounce on a manual send, or on an address whose enrollments had already
    finished, books no funnel event — ``funnel_events`` stays 0 and the ledger
    row is the only record of it.
    """
    company, sequence, account = await _fixtures(db_session)
    for i in range(100):
        await _sequence_send(
            db_session, sequence, company, account, f"ok{i}@acme.com", opened=i < 30
        )
    for i in range(8):
        db_session.add(_ingested(account, f"gone{i}@acme.com", funnel_events=0))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)

    assert m["bounces_7d"] == 8
    assert m["sources"]["bounces_dsn"] == 8
    assert m["sources"]["bounces_sequence"] == 0
    assert m["bounce_rate_7d"] == 0.08

    _rep, throttle, _status = opt._score(m)
    assert throttle < 1.0, "8% really is over the throttle threshold"


@pytest.mark.asyncio
async def test_a_genuinely_bad_sender_is_still_caught(db_session: AsyncSession):
    """De-duplicating must not become an alibi for a mailbox that is failing."""
    company, sequence, account = await _fixtures(db_session)
    for i in range(70):
        await _sequence_send(
            db_session, sequence, company, account, f"ok{i}@acme.com", opened=i < 30
        )
    for i in range(30):
        email = f"dead{i}@acme.com"
        await _sequence_send(
            db_session, sequence, company, account, email, bounced=True
        )
        db_session.add(_ingested(account, email, funnel_events=1))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)

    assert m["bounces_7d"] == 30
    assert m["bounce_rate_7d"] == 0.3
    _rep, throttle, status = opt._score(m)
    assert throttle <= 0.4
    assert status in ("throttled", "critical")


@pytest.mark.asyncio
async def test_the_dedup_does_not_reach_outside_the_window(db_session: AsyncSession):
    """Window handling is unchanged — the filter is on the population, not the date."""
    company, sequence, account = await _fixtures(db_session)
    for i in range(100):
        await _sequence_send(
            db_session, sequence, company, account, f"ok{i}@acme.com", opened=i < 30
        )
    stale = _ingested(account, "old@acme.com", funnel_events=0)
    stale.processed_at = datetime.now(timezone.utc) - timedelta(days=30)
    db_session.add(stale)
    db_session.add(_ingested(account, "new@acme.com", funnel_events=0))
    await db_session.commit()

    opt = DeliverabilityOptimizer()
    m = await opt._account_metrics(db_session, account)

    assert m["sources"]["bounces_dsn"] == 1
