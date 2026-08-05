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

def _now() -> datetime:
    """One clock reading per test, shared by the seed and the day asserted.

    The endpoints window a day in ``daily_summary_timezone`` (Asia/Kolkata,
    UTC+5:30), so from 18:30 UTC the report-local date is already tomorrow.
    That was known and half-corrected: the *target date* was resolved at call
    time while the seeded events were stamped from a module-level ``NOW``
    captured at **import** time — which in a full suite run is a quarter of an
    hour earlier. Cross 18:30 UTC in between and the events land in one
    report-local day while the assertion asks about the next, so the two
    endpoint tests failed for the 15 minutes after the boundary and passed the
    rest of the day.

    Two clock readings is the bug, not the timezone. Everything a test needs
    about "when" now comes from a single reading taken inside the test, so the
    events and the question are always about the same day. The direct
    ``compute_daily_metrics`` tests pass ``tz_name="UTC"`` and take the UTC date
    off that same reading, which closes the identical (much narrower) hole they
    had at UTC midnight.
    """
    return datetime.now(timezone.utc)


def _report_today(now: datetime):
    """*now* as a date in the timezone the endpoints reckon a day in."""
    from app.config import get_settings

    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo(get_settings().daily_summary_timezone)).date()
    except Exception:
        return now.date()


async def _seed_day_activity(db: AsyncSession, now: datetime) -> None:
    """Create a company, sequence, enrollment, and a spread of *now*'s events."""
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
    contact.last_bounced_at = now
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
                         event_type="sent", occurred_at=now))
    db.add(OutreachEvent(id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=2,
                         event_type="sent", occurred_at=now))
    db.add(OutreachEvent(id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                         event_type="replied", occurred_at=now))

    # A lead score + follow-up + prospecting run, all today.
    db.add(LeadScore(id=_uuid.uuid4(), company_id=company.id, score_type="ai",
                     fit_score=82, scored_at=now))
    db.add(FollowUp(id=_uuid.uuid4(), company_id=company.id, contact_id=contact.id,
                    due_date=now + timedelta(days=1), reason="stale", status="pending",
                    created_at=now))
    db.add(ProspectingRun(id=_uuid.uuid4(), trigger="scheduled", status="completed",
                          companies_found=10, companies_qualified=4, companies_enrolled=2,
                          companies_review_queue=1, started_at=now))
    await db.commit()


@pytest.mark.asyncio
async def test_compute_daily_metrics(db_session: AsyncSession):
    now = _now()
    await _seed_day_activity(db_session, now)
    metrics = await compute_daily_metrics(db_session, now.date(), tz_name="UTC")

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
    now = _now()
    metrics = await compute_daily_metrics(db_session, now.date(), tz_name="UTC")
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
    now = _now()
    await _seed_day_activity(db_session, now)
    metrics = await compute_daily_metrics(db_session, now.date(), tz_name="UTC")
    narrative = build_narrative(metrics)
    assert "Dialtone was active" in narrative
    # The old generic phrasing should be gone.
    assert "the autonomous agent" not in narrative


@pytest.mark.asyncio
async def test_generate_and_store_and_upsert(db_session: AsyncSession):
    now = _now()
    await _seed_day_activity(db_session, now)
    summary = await generate_and_store_summary(db_session, target_date=now.date(), tz_name="UTC")
    await db_session.commit()

    assert summary.summary_date == now.date()
    assert summary.emails_sent == 2
    assert summary.replies_received == 1
    assert summary.narrative and "email" in summary.narrative.lower()

    # Re-running the same day must UPSERT (one row), not duplicate.
    again = await generate_and_store_summary(db_session, target_date=now.date(), tz_name="UTC")
    await db_session.commit()
    assert again.id == summary.id

    rows = list(
        (await db_session.execute(select(AgentDailySummary))).scalars().all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_daily_summary_latest_endpoint(client: AsyncClient, db_session: AsyncSession):
    now = _now()
    await _seed_day_activity(db_session, now)
    resp = await client.get("/api/v1/reports/daily-summary?days=7")
    assert resp.status_code == 200
    data = resp.json()
    assert data["latest"] is not None
    assert data["latest"]["emails_sent"] == 2
    assert isinstance(data["history"], list) and len(data["history"]) >= 1
    assert "metrics" in data["latest"]


@pytest.mark.asyncio
async def test_daily_summary_by_date_endpoint(client: AsyncClient, db_session: AsyncSession):
    now = _now()
    await _seed_day_activity(db_session, now)
    target = _report_today(now)
    resp = await client.get(f"/api/v1/reports/daily-summary/{target.isoformat()}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["date"] == target.isoformat()
    assert data["emails_sent"] == 2


@pytest.mark.parametrize("tz_name", ["Pacific/Kiritimati", "Pacific/Midway"])
@pytest.mark.asyncio
async def test_the_seeded_day_and_the_reported_day_agree_in_any_timezone(
    client: AsyncClient, db_session: AsyncSession, tz_name: str
):
    """Wherever the report-local day boundary sits, the two are one day.

    Kiritimati is UTC+14 and Midway UTC−11 — between them the report-local date
    is ahead of the UTC date for most of the day at one end and behind it for
    most of the day at the other. So a scenario that stamps its events off one
    clock and asks its question off another fails here at essentially any hour,
    rather than only in the minutes after 18:30 UTC when Asia/Kolkata rolls.

    That is the whole point: the flake this replaces was not a timezone the code
    got wrong, it was two readings of the clock, and a test that only exercises
    the shipped zone can only catch it during that zone's own boundary window.
    """
    from app.config import get_settings

    settings = get_settings()
    previous = settings.daily_summary_timezone
    settings.daily_summary_timezone = tz_name
    try:
        now = _now()
        await _seed_day_activity(db_session, now)
        target = _report_today(now)

        resp = await client.get(f"/api/v1/reports/daily-summary/{target.isoformat()}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["emails_sent"] == 2, resp.text

        latest = await client.get("/api/v1/reports/daily-summary?days=7")
        assert latest.status_code == 200
        assert latest.json()["latest"]["emails_sent"] == 2
    finally:
        settings.daily_summary_timezone = previous


@pytest.mark.asyncio
async def test_a_minute_before_local_midnight_still_belongs_to_that_day(
    client: AsyncClient, db_session: AsyncSession
):
    """The boundary itself, on a fixed instant — no clock, no window to miss.

    The tests above take a real reading and so can only be *right*, never
    *proven*; this one names the edge. An event a minute before the report
    timezone's midnight is that day's and not the next day's, whichever side of
    the UTC date it happens to fall on.
    """
    from app.config import get_settings

    settings = get_settings()
    previous = settings.daily_summary_timezone
    settings.daily_summary_timezone = "UTC"
    try:
        edge = datetime(2026, 8, 1, 23, 59, tzinfo=timezone.utc)
        await _seed_day_activity(db_session, edge)

        same_day = await client.get("/api/v1/reports/daily-summary/2026-08-01")
        assert same_day.status_code == 200, same_day.text
        assert same_day.json()["emails_sent"] == 2, same_day.text

        next_day = await client.get("/api/v1/reports/daily-summary/2026-08-02")
        assert next_day.status_code == 200, next_day.text
        assert next_day.json()["emails_sent"] == 0, next_day.text
    finally:
        settings.daily_summary_timezone = previous


@pytest.mark.asyncio
async def test_daily_summary_bad_date(client: AsyncClient):
    resp = await client.get("/api/v1/reports/daily-summary/not-a-date")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_daily_summary_future_date(client: AsyncClient):
    now = _now()
    future = (now.date() + timedelta(days=5)).isoformat()
    resp = await client.get(f"/api/v1/reports/daily-summary/{future}")
    assert resp.status_code == 404
