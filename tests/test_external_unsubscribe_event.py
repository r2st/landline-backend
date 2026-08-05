"""``POST /outreach/events`` with ``unsubscribed`` was the do-not-contact writer
that only stopped one enrollment.

Six writers take an address out of play — the footer link, the RFC 8058
one-click POST, a reply saying "unsubscribe", a spam complaint, a hard bounce,
an erasure request — and every one of them sets ``contact.suppressed``, writes
the revoked ``ConsentRecord``, stops **every** enrollment in
``ACTIONABLE_STATUSES``, and adds the address to the org suppression list. That
last part is the durable half: flags live on a contact row and the scraper
creates rows.

The seventh writer did none of it. ``record_event`` set
``enrollment.status = 'unsubscribed'`` on the one enrollment the event named and
returned. What follows is quiet in all three directions:

* the contact's other live sequences kept their schedule, so a recipient who
  told the ESP to stop went on receiving our mail from a different campaign;
* ``contact.suppressed`` stayed False, so every gate that reads the row — the
  enrollment gates, ``check_send_allowed`` — waved the address through;
* nothing address-keyed was written, so a re-scrape next month arrives with
  clean flags and the cadence starts over.

The endpoint is reachable by an ESP with an HMAC signature and by a logged-in
operator, and the omission is identical on both.

The second finding here is a consequence of adding a writer. ``opt_out.
attribute_to_campaign`` books exactly one ``unsubscribed`` OutreachEvent per org
per opt-out (the portfolio totals sum the campaign rows, so two would report
more unsubscribes than people). Its idempotency check asked whether *one*
enrollment already carried such an event — sound while it was the only writer of
them. With this handler writing one against the enrollment the ESP names, a
contact in two campaigns would get a second booking on whichever enrollment held
the most recent send. The check is asked of the contact now.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry


async def _mk_contact(db: AsyncSession, company, email: str | None) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Noor",
        last_name="Haddad",
        email=email,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _mk_enrollment(
    db: AsyncSession, contact, *, status: str = "active"
) -> OutreachEnrollment:
    seq = OutreachSequence(
        id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active"
    )
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status=status,
        current_step=2,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _mk_sent_event(db: AsyncSession, enrollment, *, when: datetime) -> OutreachEvent:
    ev = OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=1,
        event_type="sent",
        occurred_at=when,
        event_metadata={},
    )
    db.add(ev)
    await db.flush()
    return ev


async def _unsubscribed_events(db: AsyncSession, contact_id) -> list[OutreachEvent]:
    return list(
        (
            await db.execute(
                select(OutreachEvent)
                .join(
                    OutreachEnrollment,
                    OutreachEvent.enrollment_id == OutreachEnrollment.id,
                )
                .where(
                    OutreachEnrollment.contact_id == contact_id,
                    OutreachEvent.event_type == "unsubscribed",
                )
            )
        ).scalars().all()
    )


@pytest.mark.asyncio
async def test_an_esp_unsubscribe_suppresses_the_address_everywhere(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The whole finding. Every assertion below failed before the fix — not with
    an error, with silence and a 201."""
    contact = await _mk_contact(db_session, sample_company, "noor@acme.com")
    named = await _mk_enrollment(db_session, contact)
    other = await _mk_enrollment(db_session, contact)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(named.id),
            "step_number": 2,
            "event_type": "unsubscribed",
        },
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(contact)
    await db_session.refresh(named)
    await db_session.refresh(other)

    assert named.status == "unsubscribed"
    assert other.status == "unsubscribed", (
        "the recipient opted out of us, not of one campaign — every live "
        "enrollment has to stop"
    )
    assert contact.suppressed is True
    assert contact.unsubscribed is True

    entry = (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == "noor@acme.com")
        )
    ).scalars().first()
    assert entry is not None, "the durable half — the list outlives the contact row"
    assert entry.reason == "unsubscribe"

    records = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.contact_id == contact.id)
        )
    ).scalars().all()
    assert any(r.revoked_at is not None for r in records), "the audit trail"


@pytest.mark.asyncio
async def test_the_opt_out_is_booked_once_for_a_contact_in_two_campaigns(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """One event per org per opt-out — the rule the portfolio totals depend on.

    The ESP names campaign A; the most recent send is campaign B. Before the
    idempotency check was asked of the contact, ``attribute_to_campaign`` saw no
    ``unsubscribed`` event on B and wrote a second one, so the org reported two
    unsubscribes for one person.
    """
    now = datetime.now(timezone.utc)
    contact = await _mk_contact(db_session, sample_company, "twice@acme.com")
    campaign_a = await _mk_enrollment(db_session, contact)
    campaign_b = await _mk_enrollment(db_session, contact)
    await _mk_sent_event(db_session, campaign_a, when=now - timedelta(days=5))
    await _mk_sent_event(db_session, campaign_b, when=now - timedelta(days=1))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(campaign_a.id),
            "step_number": 1,
            "event_type": "unsubscribed",
        },
    )
    assert resp.status_code == 201, resp.text

    events = await _unsubscribed_events(db_session, contact.id)
    assert len(events) == 1, (
        "one human opting out must not read as two unsubscribes on the "
        f"portfolio totals (got {[str(e.enrollment_id) for e in events]})"
    )


@pytest.mark.asyncio
async def test_a_redelivered_unsubscribe_changes_nothing(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """Webhook delivery is at-least-once. The second POST returns the recorded
    event, and must not book a second opt-out or a second consent record."""
    contact = await _mk_contact(db_session, sample_company, "again@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    await _mk_sent_event(db_session, enr, when=datetime.now(timezone.utc))
    await db_session.commit()

    body = {
        "enrollment_id": str(enr.id),
        "step_number": 1,
        "event_type": "unsubscribed",
    }
    first = await client.post("/api/v1/outreach/events", json=body)
    second = await client.post("/api/v1/outreach/events", json=body)
    assert first.status_code == 201 and second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]

    assert len(await _unsubscribed_events(db_session, contact.id)) == 1

    records = (
        await db_session.execute(
            select(ConsentRecord).where(ConsentRecord.contact_id == contact.id)
        )
    ).scalars().all()
    assert len(records) == 1, "a redelivery is not a second decision"


@pytest.mark.asyncio
async def test_a_bounced_event_does_not_suppress(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The control, and a deliberate boundary.

    An ESP reports soft and hard failures with one ``bounced`` event type, and
    which one it is decides whether the address is suppressed. That call belongs
    to ``bounce_handler``; a transient full mailbox must not take a live prospect
    out of play here.
    """
    contact = await _mk_contact(db_session, sample_company, "bouncy@acme.com")
    enr = await _mk_enrollment(db_session, contact)
    other = await _mk_enrollment(db_session, contact)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enr.id),
            "step_number": 1,
            "event_type": "bounced",
        },
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(contact)
    await db_session.refresh(other)
    assert enr is not None
    assert contact.suppressed is False
    assert other.status == "active"
    assert (
        await db_session.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == "bouncy@acme.com")
        )
    ).scalars().first() is None


@pytest.mark.asyncio
async def test_an_unsubscribe_for_an_addressless_contact_still_returns_the_event(
    client: AsyncClient, db_session: AsyncSession, sample_company
):
    """The receipt is what the ESP is waiting for.

    ``contacts.email`` is nullable — an enrichment run that found a person and
    not their address leaves one — so there is a real enrollment here with
    nothing to suppress. Answering a delivered webhook with a 500 makes it
    redeliver the same body forever, so the opt-out is best-effort and the
    enrollment still stops.
    """
    contact = await _mk_contact(db_session, sample_company, None)
    enr = await _mk_enrollment(db_session, contact)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": str(enr.id),
            "step_number": 1,
            "event_type": "unsubscribed",
        },
    )
    assert resp.status_code == 201, resp.text

    await db_session.refresh(enr)
    assert enr.status == "unsubscribed"
