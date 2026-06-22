"""Tests for the client-health endpoint."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.interaction import Interaction


@pytest.mark.asyncio
async def test_health_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/companies/{_uuid.uuid4()}/health")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_health_no_activity_is_churned(
    client: AsyncClient, sample_company: Company
):
    resp = await client.get(f"/api/v1/companies/{sample_company.id}/health")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["score"] == 0
    assert data["health_status"] == "churned"
    assert data["signals"]["last_contact_at"] is None
    assert data["signals"]["emails_sent"] == 0


@pytest.mark.asyncio
async def test_health_active_client_is_healthy(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    # Recent email that was replied to.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email, subject="Hi", status="replied",
        sent_at=now - timedelta(days=2), replied_at=now - timedelta(days=1),
    ))
    # Recent meeting.
    db_session.add(Interaction(
        id=_uuid.uuid4(), company_id=sample_company.id, contact_id=sample_contact.id,
        interaction_type="meeting", subject="Kickoff", occurred_at=now - timedelta(days=3),
    ))
    await db_session.commit()

    data = (await client.get(f"/api/v1/companies/{sample_company.id}/health")).json()
    # recency(40, <=7d) + responsiveness(30, 1 reply/1 send) + engagement(10, 1 meeting)
    assert data["score"] >= 70
    assert data["health_status"] == "healthy"
    assert data["signals"]["response_rate"] == 1.0
    assert data["signals"]["meetings_last_90d"] == 1
    assert data["signals"]["days_since_last_contact"] <= 3


@pytest.mark.asyncio
async def test_health_stale_client_is_at_risk(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    # Contact 45 days ago, replied, but no recent meetings.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email, subject="Old", status="replied",
        sent_at=now - timedelta(days=45), replied_at=now - timedelta(days=44),
    ))
    await db_session.commit()

    data = (await client.get(f"/api/v1/companies/{sample_company.id}/health")).json()
    # recency ~16 (45d) + responsiveness 30 + engagement 0 = ~46 -> at_risk
    assert data["health_status"] == "at_risk"
    assert 40 <= data["score"] < 70
