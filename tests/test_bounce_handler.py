"""Tests for bounce classification and contact suppression."""

import uuid

from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services.bounce_handler import classify_bounce, record_bounce


async def _contact_with_enrollment(db, email="lead@acme.com"):
    company = Company(id=uuid.uuid4(), name="Acme Mfg", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(id=uuid.uuid4(), company_id=company.id, email=email,
                      first_name="Dana", source="test")
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add_all([contact, seq])
    await db.flush()
    enr = OutreachEnrollment(id=uuid.uuid4(), contact_id=contact.id,
                             sequence_id=seq.id, status="active")
    db.add(enr)
    await db.flush()
    return contact, enr


# --- classify_bounce ---

def test_classify_by_status_code():
    assert classify_bounce("5.1.1", "") == "hard"
    assert classify_bounce("550", "") == "hard"
    assert classify_bounce("4.2.2", "") == "soft"
    assert classify_bounce("452", "mailbox full") == "soft"


def test_classify_by_diagnostic():
    assert classify_bounce(None, "550 5.1.1 user unknown") == "hard"
    assert classify_bounce(None, "recipient mailbox full, try again later") == "soft"
    assert classify_bounce(None, "No such user here") == "hard"


def test_classify_defaults_soft_when_ambiguous():
    assert classify_bounce(None, "something weird happened") == "soft"
    assert classify_bounce(None, "") == "soft"


# --- record_bounce ---

async def test_hard_bounce_suppresses_and_pauses(db_session):
    contact, enr = await _contact_with_enrollment(db_session)
    result = await record_bounce(db_session, contact.email, status_code="5.1.1",
                                 diagnostic="user unknown")
    assert result["kind"] == "hard"
    assert result["suppressed"] == 1
    assert result["enrollments_paused"] == 1

    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.unsubscribed is True
    assert contact.bounce_count == 1
    assert contact.last_bounce_type == "hard"
    assert contact.last_bounced_at is not None
    assert enr.status == "bounced"


async def test_soft_bounce_below_threshold_not_suppressed(db_session):
    contact, enr = await _contact_with_enrollment(db_session, email="soft@acme.com")
    result = await record_bounce(db_session, contact.email, status_code="4.2.2",
                                 diagnostic="temporarily deferred")
    assert result["kind"] == "soft"
    assert result["suppressed"] == 0
    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.unsubscribed is False
    assert contact.bounce_count == 1
    assert enr.status == "active"


async def test_soft_bounces_suppress_at_threshold(db_session):
    # Default soft_bounce_suppress_threshold is 3.
    contact, enr = await _contact_with_enrollment(db_session, email="repeat@acme.com")
    for _ in range(2):
        await record_bounce(db_session, contact.email, status_code="4.2.2")
    await db_session.refresh(contact)
    assert contact.unsubscribed is False  # 2 soft bounces, not yet

    result = await record_bounce(db_session, contact.email, status_code="4.2.2")
    assert result["suppressed"] == 1
    await db_session.refresh(contact)
    await db_session.refresh(enr)
    assert contact.bounce_count == 3
    assert contact.unsubscribed is True
    assert enr.status == "bounced"


async def test_bounce_unknown_email_is_noop(db_session):
    result = await record_bounce(db_session, "nobody@nowhere.com", status_code="5.1.1")
    assert result["matched_contacts"] == 0
    assert result["suppressed"] == 0


async def test_bounce_endpoint(client, db_session):
    contact, enr = await _contact_with_enrollment(db_session, email="api@acme.com")
    await db_session.commit()
    r = await client.post("/api/v1/deliverability/bounce",
                          json={"email": "api@acme.com", "status_code": "5.1.1",
                                "diagnostic": "user unknown"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "hard" and body["suppressed"] == 1

    refreshed = (await db_session.execute(
        select(Contact).where(Contact.id == contact.id)
    )).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.unsubscribed is True


async def test_bounce_endpoint_requires_email(client):
    r = await client.post("/api/v1/deliverability/bounce", json={"email": ""})
    assert r.status_code == 400
