"""Tests for the client-management overview."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.task import Task


async def _client_company(db_session: AsyncSession, name: str) -> Company:
    company = Company(
        id=_uuid.uuid4(), name=name, country="US", source="test",
        pipeline_stage="client",
    )
    db_session.add(company)
    await db_session.flush()
    return company


@pytest.mark.asyncio
async def test_overview_empty(client: AsyncClient):
    resp = await client.get("/api/v1/clients/overview")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["client_count"] == 0
    assert data["total_won_revenue"] == 0
    assert data["won_deal_count"] == 0
    assert data["at_risk_clients"] == []


@pytest.mark.asyncio
async def test_overview_counts_clients_and_revenue(
    client: AsyncClient, db_session: AsyncSession,
):
    c1 = await _client_company(db_session, "Client One")
    c2 = await _client_company(db_session, "Client Two")
    # A non-client company should NOT count.
    db_session.add(Company(
        id=_uuid.uuid4(), name="Just a lead", country="US", source="test",
        pipeline_stage="outreach",
    ))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c1.id, title="Won deal",
                        value=20000, status="won"))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c2.id, title="Pending deal",
                        value=5000, status="pending"))
    await db_session.commit()

    data = (await client.get("/api/v1/clients/overview")).json()
    assert data["client_count"] == 2
    assert data["total_won_revenue"] == 20000
    assert data["won_deal_count"] == 1
    # No activity -> both churned in the distribution.
    assert sum(data["health_distribution"].values()) == 2


@pytest.mark.asyncio
async def test_overview_at_risk_list_and_tasks(
    client: AsyncClient, db_session: AsyncSession,
):
    now = datetime.now(timezone.utc)
    c1 = await _client_company(db_session, "AtRisk Client")
    # Contact + an old replied email -> at_risk (recency low, responsiveness high).
    contact = Contact(id=_uuid.uuid4(), company_id=c1.id, first_name="Jo",
                      email="jo@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    from app.models.email_template import EmailSend
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="jo@x.com", subject="x",
        status="replied", sent_at=now - timedelta(days=45),
        replied_at=now - timedelta(days=44),
    ))
    # An upcoming task for the client.
    db_session.add(Task(
        id=_uuid.uuid4(), company_id=c1.id, title="Renewal call",
        due_date=now + timedelta(days=2), status="pending", task_type="call",
    ))
    await db_session.commit()

    data = (await client.get("/api/v1/clients/overview", params={"upcoming_days": 7})).json()
    assert data["health_distribution"]["at_risk"] >= 1
    assert any(c["company_id"] == str(c1.id) for c in data["at_risk_clients"])
    assert data["upcoming_task_count"] >= 1
    assert any(t["title"] == "Renewal call" for t in data["upcoming_tasks"])
    # avg_health_score is the mean computed score across clients.
    assert data["avg_health_score"] is not None


@pytest.mark.asyncio
async def test_list_clients_empty(client: AsyncClient):
    data = (await client.get("/api/v1/clients")).json()
    assert data == []


@pytest.mark.asyncio
async def test_list_clients_enriched(client: AsyncClient, db_session: AsyncSession):
    c1 = await _client_company(db_session, "Enriched Client")
    # A non-client company should not appear.
    db_session.add(Company(id=_uuid.uuid4(), name="Lead", country="US",
                           source="test", pipeline_stage="outreach"))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c1.id, title="Won",
                        value=12000, status="won"))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c1.id, title="Lost",
                        value=3000, status="lost"))
    db_session.add(Task(id=_uuid.uuid4(), company_id=c1.id, title="Call",
                        status="pending"))
    await db_session.commit()

    data = (await client.get("/api/v1/clients")).json()
    assert isinstance(data, list) and len(data) == 1
    row = data[0]
    assert row["company_id"] == str(c1.id)
    assert row["name"] == "Enriched Client"
    # Only the won deal counts toward account value.
    assert row["deal_value"] == 12000
    assert row["upcoming_tasks"] == 1
    assert "health_score" in row
    # health_status is intentionally omitted (frontend derives it from score).
    assert "health_status" not in row
