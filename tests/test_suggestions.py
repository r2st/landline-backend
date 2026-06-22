"""Tests for the smart-suggestions (next-best-action) feed."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.company_news import CompanyNews
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore


@pytest.mark.asyncio
async def test_suggestions_empty(client: AsyncClient):
    resp = await client.get("/api/v1/suggestions")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["suggestions"] == []


@pytest.mark.asyncio
async def test_overdue_follow_up_suggestion(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(FollowUp(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        company_id=sample_company.id,
        due_date=now - timedelta(days=3),
        reason="Second touch",
        status="pending",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions")
    data = resp.json()
    overdue = [s for s in data["suggestions"] if s["type"] == "overdue_follow_up"]
    assert len(overdue) == 1
    assert overdue[0]["priority"] == "high"
    assert overdue[0]["metadata"]["days_overdue"] >= 3
    assert overdue[0]["company_id"] == str(sample_company.id)


@pytest.mark.asyncio
async def test_stale_lead_suggestion(
    client: AsyncClient, db_session: AsyncSession,
):
    # Active-pipeline company never emailed -> stale.
    company = Company(
        id=_uuid.uuid4(), name="Stale Co", country="US", source="test",
        pipeline_stage="outreach",
    )
    db_session.add(company)
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions")
    data = resp.json()
    stale = [s for s in data["suggestions"] if s["type"] == "stale_lead"]
    assert any(s["company_id"] == str(company.id) for s in stale)


@pytest.mark.asyncio
async def test_new_lead_not_flagged_stale(
    client: AsyncClient, db_session: AsyncSession,
):
    # A brand-new lead (new_lead stage) should NOT be a stale suggestion.
    company = Company(
        id=_uuid.uuid4(), name="Fresh Co", country="US", source="test",
        pipeline_stage="new_lead",
    )
    db_session.add(company)
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions")
    data = resp.json()
    stale = [s for s in data["suggestions"] if s["type"] == "stale_lead"]
    assert all(s["company_id"] != str(company.id) for s in stale)


@pytest.mark.asyncio
async def test_recent_news_suggestion(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company,
):
    now = datetime.now(timezone.utc)
    db_session.add(CompanyNews(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        title="Acme raises Series B",
        category="funding",
        is_actionable=True,
        actioned=False,
        published_at=now - timedelta(days=2),
    ))
    # An old / non-actionable item should be ignored.
    db_session.add(CompanyNews(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        title="Old news",
        is_actionable=True,
        actioned=False,
        published_at=now - timedelta(days=120),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions", params={"news_days": 14})
    data = resp.json()
    news = [s for s in data["suggestions"] if s["type"] == "recent_news"]
    assert len(news) == 1
    assert "Series B" in news[0]["reason"]


@pytest.mark.asyncio
async def test_high_score_suggestion(
    client: AsyncClient, db_session: AsyncSession,
):
    company = Company(
        id=_uuid.uuid4(), name="HotLead Co", country="US", source="test",
        pipeline_stage="researching",
    )
    db_session.add(company)
    await db_session.flush()
    db_session.add(LeadScore(
        id=_uuid.uuid4(),
        company_id=company.id,
        score_type="rule_based",
        fit_score=90,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions", params={"high_score_threshold": 75})
    data = resp.json()
    high = [s for s in data["suggestions"] if s["type"] == "high_score"]
    assert any(s["company_id"] == str(company.id) for s in high)
    match = next(s for s in high if s["company_id"] == str(company.id))
    assert match["metadata"]["fit_score"] == 90


@pytest.mark.asyncio
async def test_suggestions_ranked_and_limited(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    # One high-priority overdue follow-up and one medium stale lead.
    db_session.add(FollowUp(
        id=_uuid.uuid4(), contact_id=sample_contact.id, company_id=sample_company.id,
        due_date=now - timedelta(days=1), status="pending",
    ))
    stale = Company(
        id=_uuid.uuid4(), name="Another Stale", country="US", source="test",
        pipeline_stage="engaged",
    )
    db_session.add(stale)
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions", params={"limit": 5})
    data = resp.json()
    assert len(data["suggestions"]) <= 5
    # High-priority suggestions sort ahead of medium ones.
    priorities = [s["priority"] for s in data["suggestions"]]
    assert priorities == sorted(priorities, key=lambda p: {"high": 0, "medium": 1, "low": 2}[p])
    assert "counts_by_type" in data
