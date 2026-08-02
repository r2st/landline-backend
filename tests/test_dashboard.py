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
async def test_top_leads_uses_icp_fit_for_prospected_companies(client):
    """An autonomously-sourced company ranks on its ICP fit, not on nothing.

    These never get a `lead_scores` row, so ranking on the rule-based score
    alone reported them as score=null and sorted them last — the newly-found
    leads were the ones the widget couldn't show.
    """
    resp = await client.post("/api/v1/prospecting/run", json={
        "seed_candidates": [{
            "name": "Precision Stamping Corp",
            "domain": "precisionstamp.com",
            "industry": "Metal Stamping",
            "employee_count": 150,
            "country": "US",
            "source": "seed",
        }],
    })
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/v1/dashboard/top-leads")
    assert resp.status_code == 200, resp.text
    match = [l for l in resp.json() if l["name"] == "Precision Stamping Corp"]
    assert len(match) == 1
    assert match[0]["score_type"] == "icp"
    assert match[0]["score"] is not None and match[0]["score"] > 0


@pytest.mark.asyncio
async def test_top_leads_ranks_icp_and_rule_scored_together(client):
    """Both scorers feed one ranking, and each lead says which produced it."""
    await _make_company(client, name="Hand Scored Co")
    resp = await client.post("/api/v1/leads/score", json={
        "company_id": (await client.get("/api/v1/companies/")).json()["items"][0]["id"],
    })
    assert resp.status_code == 200, resp.text

    resp = await client.post("/api/v1/prospecting/run", json={
        "seed_candidates": [{
            "name": "Precision Stamping Corp",
            "domain": "precisionstamp.com",
            "industry": "Metal Stamping",
            "employee_count": 150,
            "country": "US",
            "source": "seed",
        }],
    })
    assert resp.status_code == 200, resp.text

    resp = await client.get("/api/v1/dashboard/top-leads", params={"limit": 50})
    assert resp.status_code == 200, resp.text
    leads = resp.json()
    by_name = {l["name"]: l for l in leads}
    assert by_name["Precision Stamping Corp"]["score_type"] == "icp"
    assert by_name["Hand Scored Co"]["score_type"] == "rule_based"
    # Ranked on the effective score regardless of which scorer produced it.
    scores = [l["score"] for l in leads if l["score"] is not None]
    assert scores == sorted(scores, reverse=True)


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
async def test_email_trends_after_compose(client, sample_contact, monkeypatch):
    """The compose has to actually send for the trend to show a send.

    This used to pass with no transport configured at all: SES logged "email
    not sent", the row was written ``skipped``, and ``skipped`` was in the
    dashboard's sent-status set. So the test asserted that a compose which sent
    nothing appeared on the sent trend — the definition it was measuring, not
    the behaviour it was named for. The transport is stubbed instead.
    """
    from app.services import email_sender

    def _sent(self, **kwargs):
        return {"status": "sent", "message_id": "ses-1", "transport": "ses"}

    monkeypatch.setattr(email_sender.EmailSender, "send_email", _sent)

    # First template auto-becomes default.
    await client.post("/api/v1/email-templates", json={
        "name": "T", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(sample_contact.id)],
    })
    assert resp.json()["sent"] == 1, resp.text

    resp = await client.get("/api/v1/dashboard/email-trends?days=30")
    assert resp.status_code == 200, resp.text
    points = resp.json()
    assert sum(p["sent"] for p in points) >= 1


@pytest.mark.asyncio
async def test_delivered_is_derived_not_read_from_an_event_nobody_writes(
    client, db_session, sample_contact
):
    """The dashboard reported 0 delivered next to every send it had ever made.

    Gmail emits no delivery receipt, so nothing in this codebase writes a
    "delivered" event — and reading one straight off `OutreachEvent` is a metric
    that is not merely absent but *wrong*: 0 delivered beside 4 sent reads as a
    transport outage. Derived as sent − bounced, matching
    ``funnel_analytics._with_rates`` rather than adding a second definition.
    """
    import uuid
    from datetime import datetime, timezone

    from app.models.outreach import OutreachEnrollment, OutreachEvent

    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=uuid.uuid4(),
        current_step=1, status="active",
    )
    db_session.add(enrollment)
    await db_session.flush()
    for event_type in ("sent", "sent", "sent", "sent", "bounced"):
        db_session.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type=event_type, occurred_at=datetime.now(timezone.utc),
            event_metadata={},
        ))
    await db_session.commit()

    data = (await client.get("/api/v1/dashboard/outreach-metrics")).json()
    assert data["total_sent"] == 4
    assert data["total_bounced"] == 1
    assert data["total_delivered"] == 3
