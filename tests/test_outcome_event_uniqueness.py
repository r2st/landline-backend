"""A redelivered unsubscribe webhook must not book a second opt-out.

Migration 117 made ``opened`` and ``clicked`` unique per ``(enrollment, step)``
and reasoned explicitly about which types repeat — ``sent`` is written per
attempt, ``replied`` is repeatable, the ``task_*`` types are distinct touches.
``unsubscribed`` went unstated, and it does not repeat: a person either objected
to a given send or did not.

``POST /outreach/events`` is where the duplicates come from. It is reachable by
a signed ESP webhook, delivery there is at-least-once by design, and the handler
knows it — it answers a repeat with the row that already exists rather than a
500 the ESP would retry forever. But it recognises a repeat by catching the
``IntegrityError`` 117's index raises, so for a type 117 did not cover there was
nothing to catch and the redelivery simply inserted again.
``campaign_analytics`` counts these rows with a plain ``count()``, so one
person's opt-out delivered twice reads as two on every surface reporting
``unsubscribe_rate``.

**``bounced`` is deliberately left unconstrained**, and one test below pins that
on purpose. A retrying mailer-daemon makes duplicate DSNs an ordinary fact of
mail, so that rail neutralises them in the *arithmetic* instead —
``outreach_events.count_attempts`` counts distinct ``(enrollment, step)``, which
is what makes a redelivered DSN free rather than a second bounce on the rate
that arms the kill switch. Constraining the table would forbid rows that rail is
built to tolerate and that live databases already hold.

Migration 118 adds the index. These tests are about the behaviour it buys, not
the index: the second delivery is answered with the first event, and the
in-application writer that races the webhook survives losing.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence


async def _mk_contact(db: AsyncSession, company, email: str) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Priya",
        last_name="Raman",
        email=email,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _mk_enrollment(db: AsyncSession, contact) -> OutreachEnrollment:
    seq = OutreachSequence(
        id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active"
    )
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status="active",
        current_step=2,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _mk_event(
    db: AsyncSession, enrollment, *, kind: str, step: int = 1, when: datetime | None = None
) -> OutreachEvent:
    ev = OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=step,
        event_type=kind,
        occurred_at=when or datetime.now(timezone.utc),
    )
    db.add(ev)
    await db.flush()
    return ev


async def _count(db: AsyncSession, enrollment_id, kind: str) -> int:
    return (
        await db.execute(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.enrollment_id == enrollment_id,
                OutreachEvent.event_type == kind,
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_the_esp_is_never_told_to_retry_forever(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The failure mode the 201 exists to avoid.

    A 500 is "not accepted" to every ESP, and it redelivers a body that can
    never be accepted — indefinitely. That is why the duplicate is answered
    rather than raised, and it has to hold for this type too.
    """
    contact = await _mk_contact(db_session, sample_company, "loop@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_event(db_session, enr, kind="sent")
    await db_session.commit()

    body = {
        "enrollment_id": str(enr.id),
        "step_number": 1,
        "event_type": "unsubscribed",
    }
    responses = [
        await client.post("/api/v1/outreach/events", json=body) for _ in range(4)
    ]
    assert [r.status_code for r in responses] == [201, 201, 201, 201], (
        [r.text for r in responses]
    )
    ids = {r.json()["id"] for r in responses}
    assert len(ids) == 1, "every redelivery must be answered with the same event"
    assert await _count(db_session, enr.id, "unsubscribed") == 1


@pytest.mark.asyncio
async def test_an_unsubscribe_on_a_later_step_is_still_recorded(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The boundary. Uniqueness is per *step*, not per enrollment.

    Nothing on this path should be able to fold two different sends' outcomes
    into one row — the campaign surfaces bucket per ``(sequence, step)``, so a
    step that lost its own row reports zero.
    """
    contact = await _mk_contact(db_session, sample_company, "later@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_event(db_session, enr, kind="sent", step=1)
    await _mk_event(db_session, enr, kind="sent", step=3)
    await db_session.commit()

    for step in (1, 3):
        resp = await client.post(
            "/api/v1/outreach/events",
            json={
                "enrollment_id": str(enr.id),
                "step_number": step,
                "event_type": "unsubscribed",
            },
        )
        assert resp.status_code == 201, resp.text

    assert await _count(db_session, enr.id, "unsubscribed") == 2


@pytest.mark.asyncio
async def test_sent_and_replied_still_repeat(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """One reason 118 is a second partial index rather than a widened 117.

    117 enumerated the repeatable types deliberately: ``sent`` is written per
    attempt and ``send_claim.reconcile`` writes another for an attempt whose
    bookkeeping was lost, and a prospect may genuinely reply twice. Widening the
    predicate instead of adding one would have swallowed a real retry.
    """
    contact = await _mk_contact(db_session, sample_company, "chatty@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await db_session.commit()

    for kind in ("sent", "sent", "replied", "replied"):
        resp = await client.post(
            "/api/v1/outreach/events",
            json={
                "enrollment_id": str(enr.id),
                "step_number": 1,
                "event_type": kind,
            },
        )
        assert resp.status_code == 201, resp.text

    assert await _count(db_session, enr.id, "sent") == 2
    assert await _count(db_session, enr.id, "replied") == 2


@pytest.mark.asyncio
async def test_a_duplicate_bounce_is_still_allowed(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The deliberate omission, pinned so it is not "fixed" by symmetry later.

    A mailer-daemon retries, so duplicate DSNs are ordinary and real databases
    hold them. The bounce rail answers that in the arithmetic —
    ``count_attempts`` counts distinct ``(enrollment, step)``, which is what
    makes the redelivery free on the rate that arms the kill switch — and
    ``tests/test_danger_monitor_populations.py`` plants these rows on purpose to
    prove it. A constraint here would forbid the fixture and the history.
    """
    contact = await _mk_contact(db_session, sample_company, "dead@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_event(db_session, enr, kind="sent")
    await db_session.commit()

    body = {"enrollment_id": str(enr.id), "step_number": 1, "event_type": "bounced"}
    for _ in range(2):
        resp = await client.post("/api/v1/outreach/events", json=body)
        assert resp.status_code == 201, resp.text

    assert await _count(db_session, enr.id, "bounced") == 2

    # …and the count the kill switch actually reads is still one.
    from app.services.outreach_events import count_attempts

    _, bounces = await count_attempts(
        db_session,
        where=OutreachEvent.occurred_at >= datetime.now(timezone.utc) - timedelta(days=1),
    )
    assert bounces == 1, (
        "the rail must neutralise the redelivery in the arithmetic, which is "
        "the reason the table is not constrained"
    )


@pytest.mark.asyncio
async def test_the_opt_out_rail_survives_losing_the_race(
    db_session: AsyncSession, sample_company
):
    """``opt_out.attribute_to_campaign`` pre-checks per contact, which is a SELECT.

    One-click POSTed twice in parallel, or a footer click landing beside the
    provider's POST, and both passes read no event. With the index in place the
    loser now hits an ``IntegrityError``, so it has to come back without an
    event and without poisoning the transaction — the opt-out is already booked,
    which is the entire requirement.
    """
    from app.services import opt_out

    contact = await _mk_contact(db_session, sample_company, "twice@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_event(db_session, enr, kind="sent")
    await db_session.commit()

    now = datetime.now(timezone.utc)
    assert await opt_out.attribute_to_campaign(
        db_session, [contact], reason="unsubscribe", now=now
    ) == 1

    written = await opt_out.attribute_to_campaign(
        db_session, [contact], reason="unsubscribe", now=now + timedelta(seconds=1)
    )
    assert written == 0, "one opt-out is one event"
    assert await _count(db_session, enr.id, "unsubscribed") == 1

    # And the session is usable, not stuck in a failed transaction.
    assert (
        await db_session.execute(select(func.count(OutreachEvent.id)))
    ).scalar_one() >= 1


@pytest.mark.asyncio
async def test_the_database_refuses_a_duplicate_unsubscribe_row(
    db_session: AsyncSession, sample_company
):
    """The constraint itself, since both guards above delegate to it.

    The router and ``attribute_to_campaign`` now both treat ``IntegrityError``
    as "someone else booked it". That handling is only correct if the database
    actually raises, so assert the index bites rather than trusting the model
    declaration.
    """
    contact = await _mk_contact(db_session, sample_company, "dupe@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_event(db_session, enr, kind="unsubscribed")
    await db_session.commit()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                OutreachEvent(
                    id=_uuid.uuid4(),
                    enrollment_id=enr.id,
                    step_number=1,
                    event_type="unsubscribed",
                    occurred_at=datetime.now(timezone.utc),
                )
            )
            await db_session.flush()
