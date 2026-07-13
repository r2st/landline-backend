"""Tests for the enhanced smart-suggestions engine (Feature 1.6)."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.suggestion_outcome import SuggestionOutcome
from app.services import smart_suggestions


async def _company(db, **kw) -> Company:
    c = Company(
        id=_uuid.uuid4(), name=kw.pop("name", "Sugg Co"), country="US", source="test",
        pipeline_stage=kw.pop("pipeline_stage", "outreach"), **kw,
    )
    db.add(c)
    await db.flush()
    return c


@pytest.mark.asyncio
async def test_gather_signals_and_deterministic(db_session: AsyncSession):
    company = await _company(db_session, name="Signals Co")
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A", email="a@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    # Clicked but never replied → should yield an "engaged_no_reply" action.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="a@x.com", status="clicked",
        sent_at=now - timedelta(days=1), opened_at=now - timedelta(hours=10),
        clicked_at=now - timedelta(hours=9),
    ))
    await db_session.commit()

    bundles = await smart_suggestions.gather_lead_signals(db_session, now=now)
    assert any(b["company_id"] == str(company.id) for b in bundles)

    result = await smart_suggestions.generate_ai_suggestions(db_session, use_ai=False, now=now)
    assert result["source"] == "rules"
    types = {s["type"] for s in result["suggestions"]}
    assert "engaged_no_reply" in types


@pytest.mark.asyncio
async def test_news_trigger_suggestion(db_session: AsyncSession):
    company = await _company(db_session, name="News Co")
    db_session.add(CompanyNews(
        id=_uuid.uuid4(), company_id=company.id, title="News Co opens second plant",
        category="expansion", is_actionable=True, actioned=False,
        published_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    await db_session.commit()

    result = await smart_suggestions.generate_ai_suggestions(db_session, use_ai=False)
    sugg = [s for s in result["suggestions"] if s["company_id"] == str(company.id)]
    assert any(s["type"] == "news_trigger" for s in sugg)


@pytest.mark.asyncio
async def test_calibration_weights(db_session: AsyncSession):
    # 4 dismissed 'news_trigger' → suppressed (<1.0); 4 accepted 'hot_reply' → boosted (>1.0).
    for _ in range(4):
        db_session.add(SuggestionOutcome(
            id=_uuid.uuid4(), suggestion_key="k", suggestion_type="news_trigger", status="dismissed",
        ))
        db_session.add(SuggestionOutcome(
            id=_uuid.uuid4(), suggestion_key="k", suggestion_type="hot_reply", status="accepted",
        ))
    await db_session.commit()

    weights = await smart_suggestions.calibration_weights(db_session)
    assert weights["news_trigger"] < 1.0
    assert weights["hot_reply"] > 1.0


@pytest.mark.asyncio
async def test_ai_endpoint_fallback(client: AsyncClient, db_session: AsyncSession):
    # No ANTHROPIC_API_KEY in tests → endpoint uses the deterministic ranker.
    company = await _company(db_session, name="AIEndpoint Co")
    db_session.add(CompanyNews(
        id=_uuid.uuid4(), company_id=company.id, title="Big expansion", is_actionable=True,
        actioned=False, published_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/suggestions/ai")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source"] == "rules"
    assert data["total"] >= 1
    assert "generated_at" in data


@pytest.mark.asyncio
async def test_record_and_list_outcome(client: AsyncClient, db_session: AsyncSession):
    company = await _company(db_session, name="Outcome Co")
    await db_session.commit()

    resp = await client.post("/api/v1/suggestions/outcome", json={
        "suggestion_key": "ai:hot_reply:123",
        "suggestion_type": "hot_reply",
        "status": "accepted",
        "action": "schedule_meeting",
        "company_id": str(company.id),
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "accepted"
    assert resp.json()["acted_at"] is not None

    listing = await client.get("/api/v1/suggestions/outcomes", params={"suggestion_type": "hot_reply"})
    assert listing.status_code == 200
    assert len(listing.json()) == 1


@pytest.mark.asyncio
async def test_daily_suggestions_task(db_session: AsyncSession):
    from app.tasks.smart_suggestions_task import generate_daily_suggestions_for_session

    company = await _company(db_session, name="Daily Co")
    db_session.add(CompanyNews(
        id=_uuid.uuid4(), company_id=company.id, title="Timely news", is_actionable=True,
        actioned=False, published_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    result = await generate_daily_suggestions_for_session(db_session, use_ai=False)
    await db_session.commit()
    assert result["created"] >= 1
    rows = (await db_session.execute(select(SuggestionOutcome).where(SuggestionOutcome.status == "suggested"))).scalars().all()
    assert len(rows) >= 1
