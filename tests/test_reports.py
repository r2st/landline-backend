"""Tests for client-management reports."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.task import Task

NOW = datetime.now(timezone.utc)


@pytest.mark.asyncio
async def test_client_retention(client: AsyncClient, db_session: AsyncSession):
    # 2 clients, 1 churned.
    for name, stage in [("C1", "client"), ("C2", "client"), ("C3", "churned")]:
        db_session.add(Company(id=_uuid.uuid4(), name=name, country="US",
                               source="test", pipeline_stage=stage))
    c = Company(id=_uuid.uuid4(), name="DealCo", country="US", source="test",
                pipeline_stage="client")
    db_session.add(c)
    await db_session.flush()
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c.id, title="W", value=10000,
                        status="won", close_date=NOW - timedelta(days=5)))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c.id, title="L", value=4000,
                        status="lost", close_date=NOW - timedelta(days=5)))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/client-retention")).json()
    assert data["total_clients"] == 3
    assert data["total_churned"] == 1
    # 3 / (3+1) = 75%
    assert data["retention_rate"] == pytest.approx(75.0, abs=0.1)
    assert data["won_deals_in_period"] == 1
    assert data["won_revenue_in_period"] == 10000
    assert data["lost_deals_in_period"] == 1


@pytest.mark.asyncio
async def test_client_retention_date_filter(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    # Won deal 100 days ago — outside a recent window.
    db_session.add(Deal(id=_uuid.uuid4(), company_id=sample_company.id, title="Old",
                        value=9999, status="won", close_date=NOW - timedelta(days=100)))
    await db_session.commit()

    start = (NOW - timedelta(days=30)).isoformat()
    data = (await client.get("/api/v1/reports/client-retention",
                             params={"start_date": start})).json()
    assert data["won_deals_in_period"] == 0  # excluded by start_date


@pytest.mark.asyncio
async def test_revenue_pipeline(client: AsyncClient, db_session: AsyncSession,
                                sample_company: Company):
    db_session.add_all([
        Deal(id=_uuid.uuid4(), company_id=sample_company.id, title="W1", value=10000, status="won"),
        Deal(id=_uuid.uuid4(), company_id=sample_company.id, title="W2", value=30000, status="won"),
        Deal(id=_uuid.uuid4(), company_id=sample_company.id, title="P1", value=5000, status="pending"),
        Deal(id=_uuid.uuid4(), company_id=sample_company.id, title="L1", value=2000, status="lost"),
    ])
    await db_session.commit()

    data = (await client.get("/api/v1/reports/revenue-pipeline")).json()
    assert data["total_deals"] == 4
    assert data["won_revenue"] == 40000
    assert data["pipeline_value"] == 5000
    assert data["lost_value"] == 2000
    assert data["average_won_deal_size"] == 20000
    statuses = {b["status"]: b for b in data["by_status"]}
    assert statuses["won"]["count"] == 2


@pytest.mark.asyncio
async def test_activity_summary(client: AsyncClient, db_session: AsyncSession,
                                sample_company: Company, sample_contact: Contact):
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email="x@y.com",
        subject="hi", status="replied", sent_at=NOW - timedelta(days=1),
        replied_at=NOW - timedelta(hours=2),
    ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email="z@y.com",
        subject="hi2", status="sent", sent_at=NOW - timedelta(days=1),
    ))
    db_session.add(Interaction(
        id=_uuid.uuid4(), company_id=sample_company.id, interaction_type="meeting",
        subject="Sync", occurred_at=NOW - timedelta(days=2),
    ))
    db_session.add(Interaction(
        id=_uuid.uuid4(), company_id=sample_company.id, interaction_type="call",
        subject="Ring", occurred_at=NOW - timedelta(days=2),
    ))
    db_session.add(Task(
        id=_uuid.uuid4(), company_id=sample_company.id, title="Done task",
        status="completed",
    ))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-summary")).json()
    assert data["emails_sent"] == 2
    assert data["replies"] == 1
    assert data["response_rate"] == 0.5
    assert data["interactions_total"] == 2
    assert data["interactions_by_type"]["meeting"] == 1
    assert data["meetings"] == 1
    assert data["tasks_completed"] >= 1


@pytest.mark.asyncio
async def test_activity_summary_empty(client: AsyncClient):
    data = (await client.get("/api/v1/reports/activity-summary")).json()
    assert data["emails_sent"] == 0
    assert data["response_rate"] == 0.0
    assert data["interactions_by_type"] == {}


# ─── Frontend-facing report series ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_revenue_funnel(client: AsyncClient, db_session: AsyncSession):
    c1 = Company(id=_uuid.uuid4(), name="StageA", country="US", source="test",
                 stage="customer")
    c2 = Company(id=_uuid.uuid4(), name="StageB", country="US", source="test",
                 stage="customer")
    c3 = Company(id=_uuid.uuid4(), name="StageC", country="US", source="test",
                 stage="contacted")
    db_session.add_all([c1, c2, c3])
    await db_session.flush()
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c1.id, title="D1", value=1000, status="won"))
    db_session.add(Deal(id=_uuid.uuid4(), company_id=c2.id, title="D2", value=500, status="pending"))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/revenue-funnel")).json()
    assert isinstance(data, list)
    by_stage = {row["stage"]: row for row in data}
    # Every valid lifecycle stage is present (zero-filled).
    assert "customer" in by_stage and "contacted" in by_stage and "new" in by_stage
    assert by_stage["customer"]["count"] == 2
    assert by_stage["customer"]["value"] == 1500
    assert by_stage["contacted"]["count"] == 1
    assert by_stage["contacted"]["value"] == 0


@pytest.mark.asyncio
async def test_activity_heatmap(client: AsyncClient, db_session: AsyncSession,
                                sample_company: Company, sample_contact: Contact):
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email="x@y.com",
        subject="hi", status="sent", sent_at=NOW - timedelta(days=1),
    ))
    db_session.add(Interaction(
        id=_uuid.uuid4(), company_id=sample_company.id, interaction_type="call",
        subject="Ring", occurred_at=NOW - timedelta(days=1),
    ))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/activity-heatmap")).json()
    assert isinstance(data, list)
    target = (NOW - timedelta(days=1)).date().isoformat()
    point = next((p for p in data if p["date"] == target), None)
    assert point is not None
    assert point["count"] == 2


@pytest.mark.asyncio
async def test_top_clients(client: AsyncClient, db_session: AsyncSession):
    big = Company(id=_uuid.uuid4(), name="BigCo", country="US", source="test")
    small = Company(id=_uuid.uuid4(), name="SmallCo", country="US", source="test")
    db_session.add_all([big, small])
    await db_session.flush()
    db_session.add_all([
        Deal(id=_uuid.uuid4(), company_id=big.id, title="W1", value=50000, status="won"),
        Deal(id=_uuid.uuid4(), company_id=big.id, title="W2", value=20000, status="won"),
        Deal(id=_uuid.uuid4(), company_id=small.id, title="W3", value=5000, status="won"),
        Deal(id=_uuid.uuid4(), company_id=small.id, title="P1", value=9999, status="pending"),
    ])
    await db_session.commit()

    data = (await client.get("/api/v1/reports/top-clients")).json()
    assert isinstance(data, list) and len(data) == 2
    # Ranked by won revenue desc; pending deal excluded.
    assert data[0]["name"] == "BigCo"
    assert data[0]["revenue"] == 70000
    assert data[0]["deals"] == 2
    assert data[1]["revenue"] == 5000


@pytest.mark.asyncio
async def test_retention_series(client: AsyncClient, db_session: AsyncSession,
                                sample_company: Company):
    from app.models.client_health import ClientHealthSnapshot
    # Three snapshots in the same month: 2 retained, 1 churned -> 66.7%.
    base = NOW.replace(day=10)
    for status in ("healthy", "at_risk", "churned"):
        db_session.add(ClientHealthSnapshot(
            id=_uuid.uuid4(), company_id=sample_company.id, score=50,
            health_status=status, recorded_at=base,
        ))
    await db_session.commit()

    data = (await client.get("/api/v1/reports/retention")).json()
    assert isinstance(data, list) and len(data) == 1
    assert data[0]["rate"] == pytest.approx(66.7, abs=0.1)


@pytest.mark.asyncio
async def test_retention_empty(client: AsyncClient):
    data = (await client.get("/api/v1/reports/retention")).json()
    assert data == []
