"""Tests for feature 3.1 — Autonomous Prospecting Agent."""

import uuid

import pytest

from app.models.company import Company
from app.models.deal import Deal
from app.models.prospecting import ProspectCandidate, ProspectingRun
from app.services.autonomous_prospector import get_prospector

pytestmark = pytest.mark.asyncio


STRONG = {
    "name": "Precision Stamping Corp",
    "domain": "precisionstamp.com",
    "industry": "Metal Stamping",
    "employee_count": 150,
    "country": "US",
    "source": "seed",
}
BORDERLINE = {
    "name": "Woodcraft Interiors",
    "industry": "Woodworking",
    "employee_count": 150,
    "country": "US",
    "source": "seed",
}
EXCLUDED = {
    "name": "MegaCorp Global",
    "industry": "Metal Stamping",
    "employee_count": 6000,  # exceeds ICP exclusion max
    "country": "US",
    "source": "seed",
}
# Also review-bound, but scores below BORDERLINE: 600 employees sits outside the
# default ICP's 50-500 band, so it loses size points BORDERLINE keeps. Two review
# rows with *distinct* fit scores is what makes the sort assertions meaningful.
BORDERLINE_LOWER = {
    "name": "Woodcraft Fixtures",
    "industry": "Woodworking",
    "employee_count": 600,
    "country": "US",
    "source": "seed",
}


async def test_learn_icp_defaults(db_session):
    prospector = get_prospector()
    icp = await prospector.learn_icp(db_session)
    assert icp.is_active is True
    assert icp.size_range["min"] <= icp.size_range["ideal"] <= icp.size_range["max"]
    assert "Metal Stamping" in icp.industry_weights
    assert icp.exclusion_criteria["max_employees"] >= 2000


async def test_learn_icp_from_won_deal(db_session):
    company = Company(
        id=uuid.uuid4(), name="Won Co", industry_segment="Injection Molding",
        employee_count=200, country="US", source="test", stage="new", pipeline_stage="client",
    )
    db_session.add(company)
    await db_session.flush()
    db_session.add(Deal(id=uuid.uuid4(), company_id=company.id, title="Deal", status="won"))
    await db_session.flush()

    icp = await get_prospector().learn_icp(db_session)
    assert icp.learned_from_deal_count == 1
    # The won company's industry should be weighted.
    assert icp.industry_weights.get("Injection Molding", 0) > 0


async def test_qualify_scoring(db_session):
    prospector = get_prospector()
    icp = await prospector.ensure_icp(db_session)

    strong = prospector.qualify(STRONG, icp)
    assert strong["excluded"] is False
    assert strong["fit_score"] >= 75

    borderline = prospector.qualify(BORDERLINE, icp)
    assert 40 <= borderline["fit_score"] < 75

    excluded = prospector.qualify(EXCLUDED, icp)
    assert excluded["excluded"] is True
    assert excluded["fit_score"] == 0


async def test_run_dispositions(db_session):
    prospector = get_prospector()
    run = await prospector.run(
        db_session, seed_candidates=[STRONG, BORDERLINE, EXCLUDED], trigger="manual"
    )
    assert run.status == "completed"
    assert run.companies_found == 3
    assert run.companies_enrolled == 1
    assert run.companies_review_queue == 1

    cands = (
        await db_session.execute(
            ProspectCandidate.__table__.select().where(ProspectCandidate.run_id == run.id)
        )
    ).all()
    statuses = sorted(c.status for c in cands)
    assert statuses == ["enrolled", "rejected", "review"]

    # The enrolled candidate should have created/advanced a Company.
    enrolled = [c for c in cands if c.status == "enrolled"][0]
    assert enrolled.company_id is not None
    company = await db_session.get(Company, enrolled.company_id)
    assert company.pipeline_stage == "outreach"


async def test_run_disabled(db_session, monkeypatch):
    prospector = get_prospector()
    monkeypatch.setattr(prospector.settings, "prospecting_enabled", False)
    run = await prospector.run(db_session, seed_candidates=[STRONG])
    assert run.status == "paused"
    assert run.companies_enrolled == 0


async def test_daily_cap_guardrail(db_session, monkeypatch):
    prospector = get_prospector()
    monkeypatch.setattr(prospector.settings, "prospecting_daily_max", 1)
    seeds = [dict(STRONG, name=f"Strong {i}", domain=f"s{i}.com") for i in range(3)]
    run = await prospector.run(db_session, seed_candidates=seeds)
    # Only one may enroll today; the rest fall through to review or rejected.
    assert run.companies_enrolled == 1


async def test_prospecting_api_flow(client):
    payload = {"seed_candidates": [STRONG, BORDERLINE, EXCLUDED]}
    r = await client.post("/api/v1/prospecting/run", json=payload)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["companies_enrolled"] == 1
    assert run["companies_review_queue"] == 1

    r = await client.get("/api/v1/prospecting/runs")
    assert r.status_code == 200
    assert len(r.json()) >= 1

    r = await client.get("/api/v1/prospecting/review-queue")
    assert r.status_code == 200
    queue = r.json()
    assert len(queue) == 1
    cand_id = queue[0]["id"]

    r = await client.post(f"/api/v1/prospecting/candidates/{cand_id}/approve?reviewer=jane")
    assert r.status_code == 200
    assert r.json()["status"] == "enrolled"
    assert r.json()["reviewed_by"] == "jane"


async def test_review_queue_exposes_sourced_context(client):
    """The queue carries what a reviewer needs to actually decide.

    Name + score alone can't be judged: `raw_data` is the sourced record (for a
    Places candidate, `industry` is the search query that surfaced it), and
    `has_email` says whether there's anyone to enrol.
    """
    r = await client.post("/api/v1/prospecting/run", json={"seed_candidates": [BORDERLINE]})
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/prospecting/review-queue")
    assert r.status_code == 200, r.text
    queue = r.json()
    assert len(queue) == 1
    cand = queue[0]
    assert cand["raw_data"]["industry"] == "Woodworking"
    assert cand["raw_data"]["country"] == "US"
    assert cand["raw_data"]["employee_count"] == 150
    # Seeded with no contact → nobody to email yet.
    assert cand["has_email"] is False

    r = await client.post("/api/v1/contacts/", json={
        "company_id": cand["company_id"],
        "first_name": "Dale",
        "last_name": "Reed",
        "email": "dale@woodcraft.example",
    })
    assert r.status_code == 201, r.text

    r = await client.get("/api/v1/prospecting/review-queue")
    assert r.json()[0]["has_email"] is True


async def test_review_queue_filters_by_status(client):
    """One run produces all three dispositions; each is reachable by status."""
    r = await client.post(
        "/api/v1/prospecting/run", json={"seed_candidates": [STRONG, BORDERLINE, EXCLUDED]}
    )
    assert r.status_code == 200, r.text

    # Default stays the pending queue — the borderline one.
    r = await client.get("/api/v1/prospecting/review-queue")
    assert [c["company_name"] for c in r.json()] == ["Woodcraft Interiors"]

    # Approved candidates land in 'enrolled', not 'approved'.
    r = await client.get("/api/v1/prospecting/review-queue", params={"status": "enrolled"})
    assert r.status_code == 200, r.text
    assert [c["company_name"] for c in r.json()] == ["Precision Stamping Corp"]

    r = await client.get("/api/v1/prospecting/review-queue", params={"status": "rejected"})
    assert r.status_code == 200, r.text
    assert [c["company_name"] for c in r.json()] == ["MegaCorp Global"]

    r = await client.get("/api/v1/prospecting/review-queue", params={"status": "bogus"})
    assert r.status_code == 422


async def test_review_queue_sorting(client):
    r = await client.post(
        "/api/v1/prospecting/run", json={"seed_candidates": [BORDERLINE, BORDERLINE_LOWER]}
    )
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/prospecting/review-queue")
    assert r.status_code == 200, r.text
    desc_scores = [c["fit_score"] for c in r.json()]
    assert len(desc_scores) == 2
    assert desc_scores == sorted(desc_scores, reverse=True)

    r = await client.get("/api/v1/prospecting/review-queue", params={"order": "asc"})
    assert [c["fit_score"] for c in r.json()] == sorted(desc_scores)

    # Date sort is a different ranking key, not a different result set.
    r = await client.get(
        "/api/v1/prospecting/review-queue", params={"sort": "created_at", "order": "desc"}
    )
    assert r.status_code == 200, r.text
    assert sorted(c["company_name"] for c in r.json()) == [
        "Woodcraft Fixtures",
        "Woodcraft Interiors",
    ]

    r = await client.get("/api/v1/prospecting/review-queue", params={"sort": "bogus"})
    assert r.status_code == 422


async def test_icp_api(client):
    r = await client.post("/api/v1/prospecting/icp/learn")
    assert r.status_code == 200
    assert r.json()["is_active"] is True

    r = await client.get("/api/v1/prospecting/icp")
    assert r.status_code == 200
    assert r.json()["industry_weights"]
