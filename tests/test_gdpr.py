"""GDPR data-subject export (SAR) + right-to-erasure.

Covers the audit fix: before this, only a CSV contact export existed — no
per-subject access request and no erasure. Both are admin-only.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.user import UserRole
from app.services import gdpr


async def _seed_subject(db, email="jo@subject.co"):
    company = Company(id=uuid.uuid4(), name="Subject Co", country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Jo", last_name="Subject",
        email=email, phone="+15551234", city="Detroit", source="test",
    )
    db.add(contact)
    db.add(EmailSend(
        id=uuid.uuid4(), to_email=email, from_email="rep@apprend.tech",
        subject="Hi Jo", status="sent", sent_at=datetime.now(timezone.utc),
        contact_id=contact.id, body_html="<p>personal note</p>",
    ))
    db.add(EmailReply(
        id=uuid.uuid4(), from_email=email, subject="Re: Hi Jo",
        body_text="my private reply", received_at=datetime.now(timezone.utc),
        contact_id=contact.id,
    ))
    db.add(Interaction(
        id=uuid.uuid4(), contact_id=contact.id, company_id=company.id,
        interaction_type="note", subject="call notes", notes="talked about pricing",
        occurred_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return contact


# --- Service-level -----------------------------------------------------------

@pytest.mark.asyncio
async def test_export_gathers_all_records(db_session):
    await _seed_subject(db_session)
    data = await gdpr.export_subject(db_session, "jo@subject.co")
    assert data["record_counts"]["contacts"] == 1
    assert data["record_counts"]["email_sends"] == 1
    assert data["record_counts"]["email_replies"] == 1
    assert data["record_counts"]["interactions"] == 1
    # Data actually present, not just counted.
    assert data["contacts"][0]["first_name"] == "Jo"
    assert data["email_replies"][0]["body_text"] == "my private reply"


@pytest.mark.asyncio
async def test_export_case_insensitive(db_session):
    await _seed_subject(db_session, email="jo@subject.co")
    data = await gdpr.export_subject(db_session, "JO@Subject.CO")
    assert data["record_counts"]["contacts"] == 1


@pytest.mark.asyncio
async def test_erase_scrubs_pii_everywhere(db_session):
    contact = await _seed_subject(db_session)
    result = await gdpr.erase_subject(db_session, "jo@subject.co")
    assert result["anonymized_contacts"] == 1
    assert result["scrubbed"]["email_sends"] == 1
    assert result["scrubbed"]["email_replies"] == 1
    assert result["scrubbed"]["interactions"] == 1

    await db_session.refresh(contact)
    assert contact.first_name is None
    assert contact.phone is None
    assert contact.city is None
    assert contact.email.endswith("@redacted.invalid")
    assert contact.unsubscribed is True
    assert contact.suppressed is True
    assert contact.anonymized_at is not None

    # Email tables scrubbed too.
    send = (await db_session.execute(
        EmailSend.__table__.select().where(EmailSend.contact_id == contact.id)
    )).first()
    assert send.to_email == "redacted@redacted.invalid"
    reply = (await db_session.execute(
        EmailReply.__table__.select().where(EmailReply.contact_id == contact.id)
    )).first()
    assert reply.from_email == "redacted@redacted.invalid"
    assert reply.body_text == "[redacted]"


@pytest.mark.asyncio
async def test_erase_is_idempotent(db_session):
    await _seed_subject(db_session)
    first = await gdpr.erase_subject(db_session, "jo@subject.co")
    second = await gdpr.erase_subject(db_session, "jo@subject.co")
    assert first["anonymized_contacts"] == 1
    # Contact is already anonymized → the email no longer matches, nothing re-scrubbed.
    assert second["anonymized_contacts"] == 0


# --- Router-level ------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_endpoint(client, db_session):
    await _seed_subject(db_session)
    resp = await client.get("/api/v1/gdpr/export", params={"email": "jo@subject.co"})
    assert resp.status_code == 200
    assert resp.json()["record_counts"]["contacts"] == 1


@pytest.mark.asyncio
async def test_export_unknown_subject_404(client):
    resp = await client.get("/api/v1/gdpr/export", params={"email": "nobody@nowhere.com"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_erase_requires_confirm(client, db_session):
    await _seed_subject(db_session)
    resp = await client.post("/api/v1/gdpr/erase", json={"email": "jo@subject.co"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_erase_endpoint_confirmed(client, db_session):
    contact = await _seed_subject(db_session)
    resp = await client.post(
        "/api/v1/gdpr/erase", json={"email": "jo@subject.co", "confirm": True}
    )
    assert resp.status_code == 200
    assert resp.json()["anonymized_contacts"] == 1
    await db_session.refresh(contact)
    assert contact.anonymized_at is not None


@pytest.mark.asyncio
async def test_gdpr_requires_auth(unauth_client):
    assert (await unauth_client.get(
        "/api/v1/gdpr/export", params={"email": "x@y.com"}
    )).status_code == 401


@pytest.mark.asyncio
async def test_gdpr_requires_admin(client, make_user, auth_headers):
    """A non-admin agent is denied both GDPR routes."""
    agent = await make_user("agent2@alpha.com", role=UserRole.AGENT.value)
    h = auth_headers(agent)
    assert (await client.get(
        "/api/v1/gdpr/export", params={"email": "x@y.com"}, headers=h
    )).status_code == 403
    assert (await client.post(
        "/api/v1/gdpr/erase", json={"email": "x@y.com", "confirm": True}, headers=h
    )).status_code == 403
