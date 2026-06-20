"""Tests for the dashboard / analytics endpoints."""

import pytest


async def _make_company(client, name="Widget Works", industry="CNC Machining"):
    resp = await client.post("/api/v1/companies/", json={
        "name": name,
        "source": "test",
        "industry_segment": industry,
        "country": "US",
        "employee_count": 120,
    })
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_overview_includes_new_metrics(client):
    await _make_company(client)
    resp = await client.get("/api/v1/dashboard/overview")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_companies"] >= 1
    assert data["leads_this_week"] >= 1  # just created
    assert "emails_sent" in data
    assert "industry_breakdown" in data
    assert any(i["industry"] == "CNC Machining" for i in data["industry_breakdown"])


@pytest.mark.asyncio
async def test_recent_activity_feed(client):
    company = await _make_company(client, name="Activity Co")
    await client.post("/api/v1/contacts/", json={
        "company_id": company["id"],
        "first_name": "Dana",
        "last_name": "Lopez",
        "title": "COO",
    })
    resp = await client.get("/api/v1/dashboard/recent-activity")
    assert resp.status_code == 200, resp.text
    items = resp.json()
    types = {i["type"] for i in items}
    assert "company_added" in types
    assert "contact_added" in types
    # Sorted newest first.
    dates = [i["created_at"] for i in items]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.asyncio
async def test_top_leads_after_scoring(client):
    company = await _make_company(client, name="Scored Co")
    score_resp = await client.post("/api/v1/leads/score", json={"company_id": company["id"]})
    assert score_resp.status_code == 200, score_resp.text

    resp = await client.get("/api/v1/dashboard/top-leads")
    assert resp.status_code == 200, resp.text
    leads = resp.json()
    match = [l for l in leads if l["id"] == company["id"]]
    assert len(match) == 1
    assert match[0]["score"] is not None
    assert match[0]["industry"] == "CNC Machining"


@pytest.mark.asyncio
async def test_industry_breakdown_endpoint(client):
    await _make_company(client, name="A Co", industry="Injection Molding")
    await _make_company(client, name="B Co", industry="Injection Molding")
    resp = await client.get("/api/v1/dashboard/industry")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    molding = [i for i in data if i["industry"] == "Injection Molding"]
    assert molding and molding[0]["count"] == 2


@pytest.mark.asyncio
async def test_company_read_has_fit_score_and_contact_count(client):
    company = await _make_company(client, name="Detail Co")
    await client.post("/api/v1/contacts/", json={
        "company_id": company["id"], "first_name": "Sam", "last_name": "Reed",
    })
    await client.post("/api/v1/leads/score", json={"company_id": company["id"]})

    resp = await client.get(f"/api/v1/companies/{company['id']}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["contact_count"] == 1
    assert data["fit_score"] is not None


@pytest.mark.asyncio
async def test_email_trends_after_compose(client, sample_contact):
    # First template auto-becomes default.
    await client.post("/api/v1/email-templates", json={
        "name": "T", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(sample_contact.id)],
    })
    resp = await client.get("/api/v1/dashboard/email-trends?days=30")
    assert resp.status_code == 200, resp.text
    points = resp.json()
    assert sum(p["sent"] for p in points) >= 1
