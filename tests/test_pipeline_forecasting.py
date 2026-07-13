"""Tests for predictive pipeline & revenue forecasting (Feature 1.1)."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.services import pipeline_forecaster


async def _company(db: AsyncSession, **kw) -> Company:
    c = Company(id=_uuid.uuid4(), name=kw.pop("name", "Fcast Co"), country="US", source="test", **kw)
    db.add(c)
    await db.flush()
    return c


async def _deal(db: AsyncSession, company, **kw) -> Deal:
    d = Deal(
        id=_uuid.uuid4(),
        company_id=company.id,
        title=kw.pop("title", "Big deal"),
        value=kw.pop("value", 10000),
        status=kw.pop("status", "pending"),
        deal_stage=kw.pop("deal_stage", "proposal"),
        **kw,
    )
    db.add(d)
    await db.flush()
    return d


@pytest.mark.asyncio
async def test_predict_deal_heuristic(db_session: AsyncSession):
    company = await _company(db_session)
    deal = await _deal(db_session, company, deal_stage="negotiation", value=50000)
    await db_session.commit()

    pred = await pipeline_forecaster.predict_deal(db_session, deal, use_ai=False)
    assert pred.model_version == "heuristic_v1"
    assert 0 <= pred.close_probability <= 100
    # Negotiation base rate is high.
    assert pred.close_probability >= 50
    assert pred.predicted_close_date is not None


@pytest.mark.asyncio
async def test_stall_detection_flags_late_stage_no_meeting(db_session: AsyncSession):
    company = await _company(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    deal = await _deal(db_session, company, deal_stage="negotiation")
    deal.updated_at = old  # long time in stage
    await db_session.commit()

    pred = await pipeline_forecaster.predict_deal(db_session, deal, use_ai=False)
    assert "no_meeting_in_late_stage" in pred.risk_flags
    assert pred.risk_level in ("at_risk", "stalling")


@pytest.mark.asyncio
async def test_engagement_raises_probability(db_session: AsyncSession):
    company = await _company(db_session)
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A", email="a@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    # A replied + clicked email is a strong positive signal.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="a@x.com", status="replied",
        sent_at=now - timedelta(days=1), opened_at=now - timedelta(hours=20),
        clicked_at=now - timedelta(hours=19), replied_at=now - timedelta(hours=18),
    ))
    deal = await _deal(db_session, company, deal_stage="discovery")
    await db_session.commit()

    pred = await pipeline_forecaster.predict_deal(db_session, deal, use_ai=False)
    # Discovery base is 25; engagement should push it above the raw base.
    assert pred.model_probability > 25


@pytest.mark.asyncio
async def test_rescore_and_forecast(db_session: AsyncSession):
    company = await _company(db_session)
    await _deal(db_session, company, deal_stage="proposal", value=20000)
    await _deal(db_session, company, deal_stage="closing", value=40000)
    await db_session.commit()

    result = await pipeline_forecaster.rescore_active_deals(db_session, use_ai=False)
    assert result["scored"] == 2
    await db_session.commit()

    forecast = await pipeline_forecaster.build_forecast(db_session)
    assert forecast["active_deal_count"] == 2
    assert forecast["total_pipeline_value"] == 60000.0
    assert forecast["weighted_forecast"] > 0
    assert len(forecast["by_period"]) >= 1


@pytest.mark.asyncio
async def test_forecast_endpoint(client: AsyncClient, db_session: AsyncSession):
    company = await _company(db_session, name="Endpoint Co")
    await _deal(db_session, company, deal_stage="negotiation", value=30000)
    await db_session.commit()

    resp = await client.get("/api/v1/deals/forecast", params={"refresh": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["active_deal_count"] == 1
    assert data["total_pipeline_value"] == 30000.0
    assert "weighted_forecast" in data


@pytest.mark.asyncio
async def test_prediction_endpoint(client: AsyncClient, db_session: AsyncSession):
    company = await _company(db_session, name="Pred Co")
    deal = await _deal(db_session, company, deal_stage="proposal", value=15000)
    await db_session.commit()

    resp = await client.get(f"/api/v1/deals/{deal.id}/prediction")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["deal_id"] == str(deal.id)
    assert data["deal_title"] == "Big deal"
    assert data["company_name"] == "Pred Co"
    assert 0 <= data["close_probability"] <= 100
    assert data["risk_level"] in ("on_track", "watch", "at_risk", "stalling")


@pytest.mark.asyncio
async def test_prediction_endpoint_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/deals/{_uuid.uuid4()}/prediction")
    assert resp.status_code == 404
