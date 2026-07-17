"""Tracing a company back to the candidate that put it in the pipeline.

A company detail page showed `source` and nothing else, so "why is this company
here, and who decided?" was unanswerable from the company's own record. This
endpoint carries the fit score, the model's rationale and the run behind it.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.prospecting import ProspectCandidate, ProspectingRun


async def _run(db: AsyncSession, trigger="scheduled", sources=("google_places",)):
    row = ProspectingRun(
        id=uuid.uuid4(),
        trigger=trigger,
        status="completed",
        target_volume=25,
        sources_queried=list(sources),
        companies_found=25,
        companies_qualified=9,
        companies_enrolled=4,
        companies_review_queue=5,
        results={},
        started_at=datetime.now(timezone.utc),
    )
    db.add(row)
    await db.flush()
    return row


async def _candidate(db: AsyncSession, run, company, **kw):
    row = ProspectCandidate(
        id=uuid.uuid4(),
        run_id=run.id,
        company_id=company.id,
        company_name=company.name,
        domain="clevelandprecision.com",
        source=kw.pop("source", "google_places"),
        fit_score=kw.pop("fit_score", 68),
        qualification=kw.pop("qualification", {"fit_score": 68, "excluded": False, "components": {}}),
        reasoning=kw.pop("reasoning", "Contract CNC shop in the Midwest belt."),
        status=kw.pop("status", "enrolled"),
        **kw,
    )
    db.add(row)
    await db.flush()
    return row


@pytest.mark.asyncio
async def test_provenance_explains_an_agent_discovered_company(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    run = await _run(db_session)
    await _candidate(db_session, run, sample_company, reviewed_by="rep@apprend.tech")
    await db_session.commit()

    resp = await client.get(f"/api/v1/prospecting/companies/{sample_company.id}/provenance")
    assert resp.status_code == 200
    body = resp.json()

    assert body["discovered_by_agent"] is True
    assert body["fit_score"] == 68
    assert body["source"] == "google_places"
    # The rationale is the thing that answers "why is this in my pipeline".
    assert "Midwest belt" in body["reasoning"]
    assert body["reviewed_by"] == "rep@apprend.tech"
    assert body["run"]["trigger"] == "scheduled"
    assert body["run"]["sources_queried"] == ["google_places"]


@pytest.mark.asyncio
async def test_provenance_says_not_the_agent_rather_than_404ing(
    client: AsyncClient, sample_company: Company
):
    """A manually-added company is a real answer, not a missing one.

    404 here would make the caller guess between "this lead came in some other
    way" and "the lookup is broken" — and those need opposite reactions.
    """
    resp = await client.get(f"/api/v1/prospecting/companies/{sample_company.id}/provenance")
    assert resp.status_code == 200
    body = resp.json()

    assert body["discovered_by_agent"] is False
    assert body["candidate_id"] is None
    assert body["run"] is None


@pytest.mark.asyncio
async def test_provenance_404s_for_a_company_that_does_not_exist(client: AsyncClient):
    resp = await client.get(f"/api/v1/prospecting/companies/{uuid.uuid4()}/provenance")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_provenance_returns_the_most_recent_decision(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    """A company can be re-surfaced by later runs.

    The newest candidate is the one that explains its current state; showing a
    stale "rejected" over a later "enrolled" would misreport why it's active.
    """
    old_run = await _run(db_session, trigger="scheduled")
    old = await _candidate(
        db_session, old_run, sample_company, status="rejected", fit_score=40,
        reasoning="Too small at the time.",
    )
    old.created_at = datetime.now(timezone.utc) - timedelta(days=30)
    await db_session.flush()

    new_run = await _run(db_session, trigger="manual", sources=("apollo",))
    await _candidate(
        db_session, new_run, sample_company, status="enrolled", fit_score=82,
        reasoning="Grew into the ICP band.",
    )
    await db_session.commit()

    body = (
        await client.get(f"/api/v1/prospecting/companies/{sample_company.id}/provenance")
    ).json()

    assert body["fit_score"] == 82
    assert body["status"] == "enrolled"
    assert body["run"]["trigger"] == "manual"


@pytest.mark.asyncio
async def test_provenance_reports_a_candidate_still_awaiting_review(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    """The queue state is part of the answer.

    "The agent found this and a human hasn't ruled on it yet" is a different
    situation from "a human enrolled it", and the detail page should not blur
    them into an undifferentiated fit score.
    """
    run = await _run(db_session)
    await _candidate(db_session, run, sample_company, status="review", reviewed_by=None)
    await db_session.commit()

    body = (
        await client.get(f"/api/v1/prospecting/companies/{sample_company.id}/provenance")
    ).json()

    assert body["status"] == "review"
    assert body["reviewed_by"] is None
    assert body["discovered_at"] is not None
