"""Tests for the sales-tactics (playbook) endpoints.

The AI provider and Apollo are monkeypatched so these run offline and assert the
routing, validation, and response-shaping logic — not the model output.
"""

import uuid

import pytest
from httpx import AsyncClient

from app.services import ai_service as ai_module
from app.services import enrichment as enrichment_module


@pytest.fixture(autouse=True)
def _stub_ai(monkeypatch):
    """Replace each AIService tactics method with a deterministic stub."""
    async def linkedin_messages(self, **kwargs):
        return {"connection_note": "hi", "follow_up_message": "msg", "comment_draft": "nice post"}

    async def phone_script(self, **kwargs):
        return {
            "opener": "hey", "value_proposition": "we save you time",
            "discovery_questions": ["q1", "q2"],
            "objection_responses": {"no budget": "understood"},
            "close": "15 min?", "voicemail_script": "call me back",
        }

    async def referral_request(self, **kwargs):
        return {"referral_email": "intro?", "forwardable_blurb": "blurb"}

    async def event_outreach(self, **kwargs):
        return {"pre_event_email": "a", "at_event_message": "b", "post_event_followup": "c"}

    async def multi_touch_plan(self, **kwargs):
        return {"summary": "plan", "steps": [{"channel": "email", "timing": "Day 1", "action": "send"}]}

    monkeypatch.setattr(ai_module.AIService, "linkedin_messages", linkedin_messages)
    monkeypatch.setattr(ai_module.AIService, "phone_script", phone_script)
    monkeypatch.setattr(ai_module.AIService, "referral_request", referral_request)
    monkeypatch.setattr(ai_module.AIService, "event_outreach", event_outreach)
    monkeypatch.setattr(ai_module.AIService, "multi_touch_plan", multi_touch_plan)


@pytest.mark.asyncio
async def test_linkedin_draft(client: AsyncClient):
    resp = await client.post("/api/v1/tactics/linkedin-draft", json={
        "company_name": "Acme", "contact_name": "Jane Doe", "contact_title": "CTO",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["connection_note"] == "hi"
    assert "follow_up_message" in body and "comment_draft" in body


@pytest.mark.asyncio
async def test_phone_script(client: AsyncClient):
    resp = await client.post("/api/v1/tactics/phone-script", json={
        "company_name": "Acme", "contact_name": "Jane Doe",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["opener"] == "hey"
    assert isinstance(body["discovery_questions"], list)
    assert isinstance(body["objection_responses"], dict)


@pytest.mark.asyncio
async def test_referral_and_event(client: AsyncClient):
    r1 = await client.post("/api/v1/tactics/referral-draft", json={
        "company_name": "Acme", "referrer_name": "Bob", "target_name": "Jane",
    })
    assert r1.status_code == 200, r1.text
    assert r1.json()["referral_email"] == "intro?"

    r2 = await client.post("/api/v1/tactics/event-outreach", json={
        "company_name": "Acme", "contact_name": "Jane", "event_name": "IMTS",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["post_event_followup"] == "c"


@pytest.mark.asyncio
async def test_linkedin_draft_validation(client: AsyncClient):
    # Missing required contact_name -> 422
    resp = await client.post("/api/v1/tactics/linkedin-draft", json={"company_name": "Acme"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_multi_touch_plan(client: AsyncClient, sample_company):
    resp = await client.post(f"/api/v1/tactics/multi-touch-plan/{sample_company.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "plan"
    assert body["steps"][0]["channel"] == "email"


@pytest.mark.asyncio
async def test_multi_touch_plan_404(client: AsyncClient):
    resp = await client.post(f"/api/v1/tactics/multi-touch-plan/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_discover_executives_creates_deduped_contacts(client: AsyncClient, monkeypatch, sample_company):
    """discover-executives returns discovered execs and reports the count."""
    async def fake_discover(self, db, company, titles=None):
        from app.models.contact import Contact
        c = Contact(
            company_id=company.id, first_name="New", last_name="Exec",
            title="VP Ops", email="new.exec@acmestamp.com", source="apollo",
        )
        db.add(c)
        await db.flush()
        return [{
            "first_name": "New", "last_name": "Exec", "title": "VP Ops",
            "email": "new.exec@acmestamp.com", "linkedin_url": None, "seniority": "vp",
        }]

    monkeypatch.setattr(
        enrichment_module.ApolloEnrichmentService, "discover_executives", fake_discover
    )

    resp = await client.post(f"/api/v1/tactics/discover-executives/{sample_company.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["executives_found"] == 1
    assert body["contacts_created"] == 1
    assert body["executives"][0]["first_name"] == "New"


@pytest.mark.asyncio
async def test_discover_executives_empty_message(client: AsyncClient, monkeypatch, sample_company):
    async def fake_discover(self, db, company, titles=None):
        return []

    monkeypatch.setattr(
        enrichment_module.ApolloEnrichmentService, "discover_executives", fake_discover
    )
    resp = await client.post(f"/api/v1/tactics/discover-executives/{sample_company.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"]
