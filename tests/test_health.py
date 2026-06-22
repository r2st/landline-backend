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


@pytest.mark.asyncio
async def test_health_snapshot_404(client: AsyncClient):
    resp = await client.post(f"/api/v1/companies/{_uuid.uuid4()}/health/snapshot")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_health_snapshot_persists_computed_metrics(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id,
        to_email=sample_contact.email, subject="Hi", status="replied",
        sent_at=now - timedelta(days=2), replied_at=now - timedelta(days=1),
    ))
    db_session.add(Interaction(
        id=_uuid.uuid4(), company_id=sample_company.id, contact_id=sample_contact.id,
        interaction_type="meeting", subject="Kickoff", occurred_at=now - timedelta(days=3),
    ))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/companies/{sample_company.id}/health/snapshot",
        json={"satisfaction_score": 88.5},
    )
    assert resp.status_code == 201, resp.text
    snap = resp.json()
    assert snap["company_id"] == str(sample_company.id)
    assert snap["health_status"] == "healthy"
    assert snap["score"] >= 70
    assert snap["response_rate"] == 1.0
    assert snap["meeting_frequency"] == 1
    assert snap["satisfaction_score"] == 88.5
    assert snap["last_contact_date"] is not None
    assert "recorded_at" in snap


@pytest.mark.asyncio
async def test_health_snapshot_without_body(
    client: AsyncClient, sample_company: Company
):
    """Snapshot works with no body; satisfaction_score defaults to null."""
    resp = await client.post(f"/api/v1/companies/{sample_company.id}/health/snapshot")
    assert resp.status_code == 201, resp.text
    snap = resp.json()
    assert snap["satisfaction_score"] is None
    assert snap["health_status"] == "churned"
    assert snap["score"] == 0


@pytest.mark.asyncio
async def test_health_snapshot_rejects_out_of_range_satisfaction(
    client: AsyncClient, sample_company: Company
):
    resp = await client.post(
        f"/api/v1/companies/{sample_company.id}/health/snapshot",
        json={"satisfaction_score": 150},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_health_history_returns_snapshots_newest_first(
    client: AsyncClient, sample_company: Company
):
    for sat in (10, 20, 30):
        resp = await client.post(
            f"/api/v1/companies/{sample_company.id}/health/snapshot",
            json={"satisfaction_score": sat},
        )
        assert resp.status_code == 201

    history = (
        await client.get(f"/api/v1/companies/{sample_company.id}/health/history")
    ).json()
    assert len(history) == 3
    # Most recent first; recorded_at is monotonically non-increasing.
    recorded = [h["recorded_at"] for h in history]
    assert recorded == sorted(recorded, reverse=True)


@pytest.mark.asyncio
async def test_health_history_empty_for_company_without_snapshots(
    client: AsyncClient, sample_company: Company
):
    history = (
        await client.get(f"/api/v1/companies/{sample_company.id}/health/history")
    ).json()
    assert history == []
