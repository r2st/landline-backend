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


async def _enrolled(
    db, email="lead@acme.com", *, current_step=2, status="active", sent_steps=None
):
    """An enrollment that has already sent, which is the only kind that bounces.

    ``sent_steps`` defaults to every step before ``current_step`` — the state
    ``send_sequence_step`` leaves behind, since it logs its ``sent`` event and
    *then* increments. Pass ``[]`` for an enrollment that has never sent.
    """
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
    if sent_steps is None:
        sent_steps = list(range(1, max(1, current_step)))
    for step in sent_steps:
        db.add(OutreachEvent(
            enrollment_id=enr.id, step_number=step, event_type="sent",
            event_metadata={},
        ))
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
    """The step that bounced is the step last *sent*, not ``current_step``.

    ``send_sequence_step`` stamps its ``sent`` event and then increments, so an
    enrollment that has just sent step 2 sits at ``current_step == 3``. Booking
    the bounce at 3 files it against a step that has never sent.
    """
    contact, enr = await _enrolled(db_session, current_step=3, sent_steps=[2])

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="no such user"
    )
    await db_session.flush()

    events = await _bounced_events(db_session, enr.id)
    assert events[0].step_number == 2


@pytest.mark.asyncio
async def test_a_misfiled_bounce_makes_both_steps_read_clean(db_session):
    """The per-step funnel is where the off-by-one actually did damage.

    A bounce filed one step to the right lands on a step with no sends, where
    ``bounce_rate`` divides by zero to 0.0 and ``delivered`` clamps at 0 — while
    the step that really bounced reports a flawless 100% delivered. Both halves
    of the pair say "no bounce here", which is the reading
    ``bounce_handler._suppress_contact`` was written to make impossible.
    """
    from app.services.funnel_analytics import compute_funnel

    contact, enr = await _enrolled(db_session, current_step=3)

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="user unknown"
    )
    await db_session.flush()

    funnel = await compute_funnel(db_session, days=30)
    steps = {s["step_number"]: s for s in funnel["sequences"][0]["steps"]}

    # The bounce is on the step that sent it, and that step reports it.
    assert steps[2]["bounced"] == 1
    assert steps[2]["bounce_rate"] == 100.0
    assert steps[2]["delivered"] == 0
    # Nothing is invented on a step the sequence never reached.
    assert 3 not in steps
    assert steps[1]["bounced"] == 0
    assert steps[1]["delivered_rate"] == 100.0


@pytest.mark.asyncio
async def test_the_funnel_now_reports_a_real_bounce_rate(db_session):
    """End to end: the number an operator actually reads.

    Two sends, one of which bounces. Before this fix the funnel returned
    bounce_rate 0.0 / delivered_rate 100.0 for exactly this data.
    """
    from app.services.funnel_analytics import compute_funnel

    contact, enr = await _enrolled(db_session, current_step=1, sent_steps=[1, 1])

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


@pytest.mark.asyncio
async def test_an_enrollment_that_never_sent_books_no_bounce(db_session):
    """``record_bounce`` stops every active enrollment for the address.

    The prospector can enroll a contact into a second sequence that hasn't run
    yet. That enrollment has no send to attribute a bounce to, and a ``bounced``
    event with no ``sent`` partner reads as a synchronous failure — so the
    invented row would land in the denominator as well as the numerator.
    """
    contact, sent_enr = await _enrolled(db_session, current_step=2)
    idle_seq = OutreachSequence(id=uuid.uuid4(), name=f"Idle {uuid.uuid4()}")
    db_session.add(idle_seq)
    await db_session.flush()
    idle = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=idle_seq.id,
        status="active", current_step=1,
    )
    db_session.add(idle)
    await db_session.flush()

    await record_bounce(
        db_session, contact.email, status_code="5.1.1", diagnostic="user unknown"
    )
    await db_session.flush()

    # Both enrollments are stopped — the address is dead either way.
    await db_session.refresh(idle)
    assert idle.status == "bounced"
    # Only the one that actually sent reports a bounce.
    assert len(await _bounced_events(db_session, sent_enr.id)) == 1
    assert await _bounced_events(db_session, idle.id) == []


@pytest.mark.asyncio
async def test_writing_off_a_dead_domain_manufactures_no_bounces(db_session):
    """One DSN must not become twenty.

    ``suppress_dead_domain`` takes every *remaining* colleague out of play on
    the strength of somebody else's bounce. Their mail did not bounce — most of
    it was never sent — so booking one each is a prediction dressed as an
    observation, and `danger_monitor` reads it as twenty attempts that failed
    at a 100% rate. That is enough on its own to push the global bounce rate
    past the threshold and flip the platform-wide kill switch, halting every
    tenant's outreach over one bad domain in one list.
    """
    from app.services.bounce_handler import suppress_dead_domain

    company = Company(id=uuid.uuid4(), name="Dead Co", source="test", country="US")
    db_session.add(company)
    await db_session.flush()

    colleagues = []
    for i in range(5):
        contact = Contact(
            id=uuid.uuid4(), company_id=company.id, email=f"c{i}@deadco.com",
            first_name=f"C{i}", source="test",
        )
        seq = OutreachSequence(id=uuid.uuid4(), name=f"Seq {uuid.uuid4()}")
        db_session.add_all([contact, seq])
        await db_session.flush()
        enr = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
            status="active", current_step=2,
        )
        db_session.add(enr)
        await db_session.flush()
        # Even a colleague we *had* sent to: no DSN came back for them.
        db_session.add(OutreachEvent(
            enrollment_id=enr.id, step_number=1, event_type="sent",
            event_metadata={},
        ))
        await db_session.flush()
        colleagues.append(enr)

    result = await suppress_dead_domain(db_session, "deadco.com")
    await db_session.flush()

    assert result["contacts_suppressed"] == 5
    assert result["enrollments_paused"] == 5
    for enr in colleagues:
        await db_session.refresh(enr)
        assert enr.status == "bounced"
        assert await _bounced_events(db_session, enr.id) == [], (
            "a write-off is an inference, not five observed bounces"
        )
