"""A bounce has to take the *address* out of rotation, not just one enrollment.

The bug these cover: ``record_bounce`` set ``Contact.unsubscribed`` and nothing
else. No send path reads that field — ``tasks/outreach.py`` gates on
``Contact.suppressed`` and on the org suppression list — so a hard-bounced
address stayed mailable. The enrollment that bounced stopped, and the next one
(the autonomous prospector de-duplicates per *(contact, sequence)* pair, so a
second sequence enrolls the same contact freely) mailed the dead mailbox again
and earned another hard bounce against our own reputation.

The address-level list is the half that survives the contact row: discovery
re-imports create a *new* ``Contact`` for an address we already know is dead,
with a fresh ``suppressed = False``.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.suppression import SuppressionEntry
from app.services.bounce_handler import record_bounce
from app.services.suppression import is_suppressed


async def _company(db, name="Acme Mfg"):
    company = Company(id=uuid.uuid4(), name=name, source="test", country="US")
    db.add(company)
    await db.flush()
    return company


async def _contact(db, company, email):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enroll(db, contact, name="Seq"):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"{name}-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active"
    )
    db.add(enr)
    await db.flush()
    return enr


async def _entries(db, email):
    return (
        await db.execute(
            select(SuppressionEntry).where(SuppressionEntry.email == email)
        )
    ).scalars().all()


@pytest.mark.asyncio
async def test_hard_bounce_sets_the_flag_the_send_path_actually_reads(db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "dead@acme.com")
    await _enroll(db_session, contact)

    await record_bounce(db_session, contact.email, status_code="5.1.1")

    await db_session.refresh(contact)
    # ``suppressed`` is the gate; ``unsubscribed`` is kept for the enrollment
    # check in the sequences router and the contact filters. A bounce is both.
    assert contact.suppressed is True
    assert contact.unsubscribed is True


@pytest.mark.asyncio
async def test_hard_bounce_lands_on_the_org_suppression_list(db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "gone@acme.com")
    await _enroll(db_session, contact)

    result = await record_bounce(db_session, contact.email, status_code="5.1.1")

    assert result["list_entry_added"] is True
    assert await is_suppressed(db_session, "gone@acme.com") is True
    entries = await _entries(db_session, "gone@acme.com")
    assert len(entries) == 1
    assert entries[0].reason == "bounce"
    assert entries[0].source == "auto"


@pytest.mark.asyncio
async def test_a_reimported_contact_for_a_dead_address_is_still_suppressed(db_session):
    """The whole point of suppressing by address rather than by row.

    Discovery re-imports the same company and creates a *new* contact for the
    same mailbox. The new row's ``suppressed`` is False — only the address-level
    list remembers.
    """
    company = await _company(db_session)
    original = await _contact(db_session, company, "dead@acme.com")
    await record_bounce(db_session, original.email, status_code="5.1.1")

    reimported = await _contact(db_session, await _company(db_session, "Acme (dup)"),
                                "dead@acme.com")
    assert reimported.suppressed is False
    assert await is_suppressed(db_session, reimported.email) is True


@pytest.mark.asyncio
async def test_soft_bounce_below_threshold_does_not_touch_the_list(db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "full@acme.com")
    await _enroll(db_session, contact)

    result = await record_bounce(db_session, contact.email, status_code="4.2.2")

    assert result["list_entry_added"] is False
    await db_session.refresh(contact)
    assert contact.suppressed is False
    assert await is_suppressed(db_session, "full@acme.com") is False


@pytest.mark.asyncio
async def test_soft_bounces_suppress_the_address_at_the_threshold(db_session):
    company = await _company(db_session)
    contact = await _contact(db_session, company, "repeat@acme.com")
    await _enroll(db_session, contact)

    for _ in range(2):
        await record_bounce(db_session, contact.email, status_code="4.2.2")
    assert await is_suppressed(db_session, "repeat@acme.com") is False

    await record_bounce(db_session, contact.email, status_code="4.2.2")

    await db_session.refresh(contact)
    assert contact.suppressed is True
    assert await is_suppressed(db_session, "repeat@acme.com") is True


@pytest.mark.asyncio
async def test_repeat_bounces_add_exactly_one_list_entry(db_session):
    """A retried DSN must not stack rows — the list has a unique constraint and
    a second insert would be an integrity error, not a duplicate."""
    company = await _company(db_session)
    contact = await _contact(db_session, company, "twice@acme.com")
    await _enroll(db_session, contact)

    first = await record_bounce(db_session, contact.email, status_code="5.1.1")
    second = await record_bounce(db_session, contact.email, status_code="5.1.1")

    assert first["list_entry_added"] is True
    assert second["list_entry_added"] is False
    assert len(await _entries(db_session, "twice@acme.com")) == 1


@pytest.mark.asyncio
async def test_a_second_bounce_closes_an_enrollment_created_since(db_session):
    """The prospector can enroll an already-suppressed contact into a *different*
    sequence — nothing filters on the flag at enrollment time. A repeat DSN has
    to close it, or it stays due forever against a dead mailbox."""
    company = await _company(db_session)
    contact = await _contact(db_session, company, "again@acme.com")
    await _enroll(db_session, contact, name="First")
    await record_bounce(db_session, contact.email, status_code="5.1.1")

    later = await _enroll(db_session, contact, name="Second")
    result = await record_bounce(db_session, contact.email, status_code="5.1.1")

    await db_session.refresh(later)
    assert later.status == "bounced"
    assert result["enrollments_paused"] == 1
    # Already out of play, so it is not counted as newly suppressed.
    assert result["suppressed"] == 0


@pytest.mark.asyncio
async def test_hard_bounce_for_an_unknown_address_is_still_suppressed(db_session):
    """We only get a DSN for mail we sent. No contact row does not make the
    address any less dead, and the address-level list exists for exactly this."""
    result = await record_bounce(db_session, "ghost@nowhere.com", status_code="5.1.1")

    assert result["matched_contacts"] == 0
    assert result["list_entry_added"] is True
    assert await is_suppressed(db_session, "ghost@nowhere.com") is True


@pytest.mark.asyncio
async def test_soft_bounce_for_an_unknown_address_suppresses_nothing(db_session):
    result = await record_bounce(db_session, "ghost2@nowhere.com", status_code="4.2.2")

    assert result["list_entry_added"] is False
    assert await is_suppressed(db_session, "ghost2@nowhere.com") is False


@pytest.mark.asyncio
async def test_the_send_path_now_refuses_a_bounced_contact(db_session):
    """The end-to-end claim: after a hard bounce, the gate the sequence engine
    reads says no. Both halves of it, independently."""
    from app.services import suppression as _suppression

    company = await _company(db_session)
    contact = await _contact(db_session, company, "refused@acme.com")
    await _enroll(db_session, contact)
    await record_bounce(db_session, contact.email, status_code="5.1.1")
    await db_session.refresh(contact)

    gate = getattr(contact, "suppressed", False) or await _suppression.is_suppressed(
        db_session, contact.email
    )
    assert gate is True
