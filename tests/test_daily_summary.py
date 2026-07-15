"""Tests for the daily autonomous-activity summary feature."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_daily_summary import AgentDailySummary
from app.models.company import Company
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectingRun
from app.models.sequence import OutreachSequence
from app.services.daily_summary import (
    build_narrative,
    compute_daily_metrics,
    generate_and_store_summary,
)

NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


async def _seed_day_activity(db: AsyncSession) -> None:
    """Create a company, sequence, enrollment, and a spread of today's events."""
    company = Company(id=_uuid.uuid4(), name="MoldCo", country="US", source="prospecting")
    db.add(company)
    await db.flush()

    contact = Contact(
        id=_uuid.uuid4(), company_id=company.id, first_name="Pat", last_name="Lee",
        email="pat@moldco.com",
    )
    # A hard bounce recorded "now" so it lands in today's window.
    contact.bounce_count = 1
    contact.last_bounce_type = "hard"
    contact.last_bounced_at = NOW
    db.add(contact)

    seq = OutreachSequence(id=_uuid.uuid4(), name="Deal Cracker", status="active")
    db.add(seq)
    await db.flush()

    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db.add(enrollment)
    await db.flush()

    # Two sent events (steps 1 & 2) + one reply, all today.
    db.add(OutreachEvent(id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                         event_type="sent", occurred_at=NOW))
    db.add(OutreachEvent(id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=2,
                         event_type="sent", occurred_at=NOW))
    db.add(OutreachEvent(id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                         event_type="replied", occurred_at=NOW))

    # A lead score + follow-up + prospecting run, all today.
    db.add(LeadScore(id=_uuid.uuid4(), company_id=company.id, score_type="ai",
                     fit_score=82, scored_at=NOW))
    db.add(FollowUp(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    due_date=NOW + timedelta(days=1), reason="stale", status="pending",
                    created_at=NOW))
    db.add(ProspectingRun(id=_uuid.uuid4(), trigger="scheduled", status="completed",
                          companies_found=10, companies_qualified=4, companies_enrolled=2,
                          companies_review_queue=1, started_at=NOW))
    await db.commit()


@pytest.mark.asyncio
async def test_compute_daily_metrics(db_session: AsyncSession):
    await _seed_day_activity(db_session)
    metrics = await compute_daily_metrics(db_session, TODAY, tz_name="UTC")

    assert metrics["emails"]["sent"] == 2
    assert metrics["emails"]["by_stage"] == {"1": 1, "2": 1}
    assert metrics["emails"]["by_sequence"] == {"Deal Cracker": 2}
    assert metrics["replies"]["received"] == 1
    assert metrics["bounces"]["total"] == 1
    assert metrics["bounces"]["hard"] == 1
    assert metrics["leads"]["discovered"] == 1
    assert metrics["leads"]["enriched"] == 1
    assert metrics["lead_scores"]["grade_distribution"]  # non-empty
    assert metrics["follow_ups"]["created"] == 1
    assert metrics["prospecting"]["candidates_qualified"] == 4
    assert metrics["qualification"]["qualified"] == 4
    # Deliverability derived rates.
    assert metrics["deliverability"]["bounce_rate_today"] == pytest.approx(0.5)
    assert metrics["deliverability"]["reply_rate_today"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_compute_metrics_empty_day(db_session: AsyncSession):
    """A day with no activity yields zeroed metrics, not an error."""
    metrics = await compute_daily_metrics(db_session, TODAY, tz_name="UTC")
    assert metrics["emails"]["sent"] == 0
    assert metrics["bounces"]["total"] == 0
    assert metrics["deliverability"]["bounce_rate_today"] == 0.0
    narrative = build_narrative(metrics)
    assert "Quiet day" in narrative
    # Even on a quiet day the agent is named "Dialtone", not "the agent".
    assert "Dialtone" in narrative


@pytest.mark.asyncio
async def test_narrative_brands_agent_as_dialtone(db_session: AsyncSession):
    """The active-day narrative refers to the autonomous agent as "Dialtone"."""
    await _seed_day_activity(db_session)
    metrics = await compute_daily_metrics(db_session, TODAY, tz_name="UTC")
    narrative = build_narrative(metrics)
    assert "Dialtone was active" in narrative
    # The old generic phrasing should be gone.
    assert "the autonomous agent" not in narrative


@pytest.mark.asyncio
async def test_generate_and_store_and_upsert(db_session: AsyncSession):
    await _seed_day_activity(db_session)
    summary = await generate_and_store_summary(db_session, target_date=TODAY, tz_name="UTC")
    await db_session.commit()

    assert summary.summary_date == TODAY
    assert summary.emails_sent == 2
    assert summary.replies_received == 1
    assert summary.narrative and "email" in summary.narrative.lower()

    # Re-running the same day must UPSERT (one row), not duplicate.
    again = await generate_and_store_summary(db_session, target_date=TODAY, tz_name="UTC")
    await db_session.commit()
    assert again.id == summary.id

    rows = list(
        (await db_session.execute(select(AgentDailySummary))).scalars().all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_daily_summary_latest_endpoint(client: AsyncClient, db_session: AsyncSession):
    await _seed_day_activity(db_session)
    resp = await client.get("/api/v1/reports/daily-summary?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["latest"] is not None
    assert data["latest"]["emails_sent"] == 2
    assert isinstance(data["history"], list) and len(data["history"]) >= 1
    assert "metrics" in data["latest"]


@pytest.mark.asyncio
async def test_daily_summary_by_date_endpoint(client: AsyncClient, db_session: AsyncSession):
    await _seed_day_activity(db_session)
    resp = await client.get(f"/api/v1/reports/daily-summary/{TODAY.isoformat()}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == TODAY.isoformat()
    assert data["emails_sent"] == 2


@pytest.mark.asyncio
async def test_daily_summary_bad_date(client: AsyncClient):
    resp = await client.get("/api/v1/reports/daily-summary/not-a-date")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_daily_summary_future_date(client: AsyncClient):
    future = (TODAY + timedelta(days=5)).isoformat()
    resp = await client.get(f"/api/v1/reports/daily-summary/{future}")
    assert resp.status_code == 404
