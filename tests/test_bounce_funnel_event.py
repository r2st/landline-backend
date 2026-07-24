"""A bounce must reach the funnel, not just the enrollment.

``funnel_analytics`` derives BOTH ``bounce_rate`` and ``delivered``
(= sent − bounced) from a ``bounced`` OutreachEvent. Nothing in the codebase
ever wrote one: ``bounce_handler`` flipped the enrollment's status to "bounced"
and stopped there. The funnel therefore reported ``bounce_rate: 0.0`` and
``delivered_rate: 100.0`` no matter how many addresses were actually dead —
measured in production 2026-07-24 against a real hard bounce.

That failure direction matters: a deliverability metric pinned at "perfect" is
worse than a missing one, because it is the number an operator checks *before*
concluding the problem must be the copy.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.bounce_handler import record_bounce


async def _enrolled(db, email="lead@acme.com", *, current_step=2, status="active"):
    company = Company(id=uuid.uuid4(), name="Acme Mfg", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    seq = OutreachSequence(id=uuid.uuid4(), name=f"Seq {uuid.uuid4()}")
    db.add_all([contact, seq])
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status=status, current_step=current_step,
    )
    db.add(enr)
    await db.flush()
    return contact, enr


async def _bounced_events(db, enrollment_id):
    return (
        await db.execute(
            select(OutreachEvent).where(
                OutreachEvent.enrollment_id == enrollment_id,
                OutreachEvent.event_type == "bounced",
            )
        )
    ).scalars().all()


@pytest.mark.asyncio
async def test_hard_bounce_writes_a_funnel_event(db_session):
    contact, enr = await _enrolled(db_session)

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="user unknown"
    )
    await db_session.flush()

    events = await _bounced_events(db_session, enr.id)
    assert len(events) == 1, "bounce never reached the funnel"
    assert events[0].event_metadata["source"] == "bounce_handler"


@pytest.mark.asyncio
async def test_event_is_booked_against_the_step_that_bounced(db_session):
    """The send that failed is the step the enrollment was on."""
    contact, enr = await _enrolled(db_session, current_step=3)

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="no such user"
    )
    await db_session.flush()

    events = await _bounced_events(db_session, enr.id)
    assert events[0].step_number == 3


@pytest.mark.asyncio
async def test_the_funnel_now_reports_a_real_bounce_rate(db_session):
    """End to end: the number an operator actually reads.

    Two sends, one of which bounces. Before this fix the funnel returned
    bounce_rate 0.0 / delivered_rate 100.0 for exactly this data.
    """
    from app.services.funnel_analytics import compute_funnel

    contact, enr = await _enrolled(db_session, current_step=1)
    for _ in range(2):
        db_session.add(
            OutreachEvent(
                enrollment_id=enr.id, step_number=1, event_type="sent",
                event_metadata={},
            )
        )
    await db_session.flush()

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="user unknown"
    )
    await db_session.flush()

    funnel = await compute_funnel(db_session, days=30)
    totals = funnel["totals"]
    assert totals["sent"] == 2
    assert totals["bounced"] == 1
    assert totals["bounce_rate"] == 50.0
    assert totals["delivered"] == 1, "delivered is derived as sent - bounced"
    assert totals["delivered_rate"] == 50.0


@pytest.mark.asyncio
async def test_a_second_dsn_does_not_double_count(db_session):
    """Mailer-daemon retries. The funnel must not book two bounces for one send.

    No explicit dedup is needed: the first call flips the enrollment out of
    "active", and only active enrollments are walked.
    """
    contact, enr = await _enrolled(db_session)

    for _ in range(2):
        await record_bounce(
            db_session, contact.email, status_code="5.1.1", diagnostic="user unknown"
        )
        await db_session.flush()

    assert len(await _bounced_events(db_session, enr.id)) == 1


@pytest.mark.asyncio
async def test_soft_bounce_below_threshold_writes_no_event(db_session):
    """A transient failure is not a delivery failure; it must not skew the rate.

    A soft bounce leaves the enrollment active (it retries), so no funnel event
    is written until the soft-bounce threshold actually suppresses the contact.
    """
    contact, enr = await _enrolled(db_session)

    await record_bounce(
        db_session, contact.email, status_code="4.2.2", diagnostic="mailbox full"
    )
    await db_session.flush()

    await db_session.refresh(enr)
    assert enr.status == "active"
    assert await _bounced_events(db_session, enr.id) == []
