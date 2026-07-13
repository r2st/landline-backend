"""Tests for feature 3.2 — Predictive Churn Prevention & Expansion Intelligence."""

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.models.client_health import ClientHealthSnapshot
from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.intent_signal import IntentSignal
from app.services.churn_predictor import get_churn_predictor
from app.services.expansion_detector import get_expansion_detector
from app.tasks.client_health import assess_and_record

pytestmark = pytest.mark.asyncio


async def _client(db_session, name="Client Co", stage="client"):
    company = Company(
        id=uuid.uuid4(), name=name, industry_segment="CNC Machining",
        employee_count=120, country="US", source="test", stage="new", pipeline_stage=stage,
    )
    db_session.add(company)
    await db_session.flush()
    return company


async def test_churn_predict_never_contacted(db_session):
    company = await _client(db_session)
    result = await get_churn_predictor().predict(db_session, company, use_ai=False)
    assert 0.0 <= result["probability"] <= 1.0
    assert result["risk_level"] in ("low", "medium", "high")
    # A never-contacted client should surface a contact-gap risk factor.
    factors = {f["factor"] for f in result["risk_factors"]}
    assert "contact_gap" in factors
    assert result["probability"] > 0


async def test_churn_declining_trend_raises_probability(db_session):
    company = await _client(db_session)
    now = datetime.now(timezone.utc)
    # Older healthy snapshot, then a much lower recent one.
    for score, days_ago in [(90, 40), (70, 20), (35, 2)]:
        db_session.add(ClientHealthSnapshot(
            id=uuid.uuid4(), company_id=company.id, score=score,
            health_status="healthy" if score >= 70 else "churned",
            response_rate=0.2, meeting_frequency=0,
            recorded_at=now - timedelta(days=days_ago),
        ))
    await db_session.flush()
    result = await get_churn_predictor().predict(db_session, company, use_ai=False)
    factors = {f["factor"] for f in result["risk_factors"]}
    assert "declining_health_trend" in factors


async def test_expansion_detects_signals(db_session):
    company = await _client(db_session)
    # A won deal (proven buyer) + a fresh contact (new stakeholder).
    db_session.add(Deal(id=uuid.uuid4(), company_id=company.id, title="D", status="won", value=50000))
    db_session.add(Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="New", last_name="Buyer",
        email="new@client.co", source="test",
    ))
    # An active expansion intent signal.
    db_session.add(IntentSignal(
        id=uuid.uuid4(), company_id=company.id, signal_type="expansion",
        title="Opening a second plant", relevance_score=80, urgency_score=60,
    ))
    await db_session.flush()

    result = await get_expansion_detector().detect(db_session, company, use_ai=False)
    types = {s["type"] for s in result["signals"]}
    assert "proven_buyer" in types
    assert "new_stakeholders" in types
    assert "growth_intent_signal" in types
    assert result["expansion_score"] > 0
    assert result["recommended_play"]


async def test_assess_and_record_persists_snapshot(db_session):
    company = await _client(db_session)
    snap = await assess_and_record(db_session, company, use_ai=False)
    assert snap.predicted_churn_probability is not None
    assert snap.churn_risk_factors is not None
    assert "expansion_score" in snap.expansion_signals


async def _create_client_company(client, name):
    r = await client.post("/api/v1/companies/", json={
        "name": name, "industry_segment": "CNC Machining", "country": "US", "source": "test",
    })
    assert r.status_code in (200, 201), r.text
    cid = r.json()["id"]
    r = await client.patch(f"/api/v1/companies/{cid}", json={"pipeline_stage": "client"})
    assert r.status_code == 200, r.text
    return cid


async def test_churn_risk_endpoint(client):
    await _create_client_company(client, "Endpoint Client")

    r = await client.get("/api/v1/clients/churn-risk")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)
    assert any(c["company_name"] == "Endpoint Client" for c in data)
    assert all("predicted_churn_probability" in c for c in data)


async def test_expansion_endpoint(client):
    await _create_client_company(client, "Expansion Client")
    r = await client.get("/api/v1/clients/expansion-opportunities?min_score=0")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
