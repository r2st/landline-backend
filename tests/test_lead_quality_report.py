"""Tests for lead grading + the lead-quality report."""

import uuid
from datetime import datetime, timezone, timedelta

from app.models.company import Company
from app.models.lead_score import LeadScore
from app.routers.reports import build_lead_quality_report
from app.services.scoring import grade_for_fit


async def _company(db, name, source, fit=None, stage="scored"):
    c = Company(id=uuid.uuid4(), name=name, source=source, country="US", stage=stage)
    db.add(c)
    await db.flush()
    if fit is not None:
        db.add(LeadScore(id=uuid.uuid4(), company_id=c.id,
                         score_type="rule_based", fit_score=fit))
        await db.flush()
    return c


async def test_report_grade_distribution_and_counts(db_session):
    await _company(db_session, "Alpha", "apollo", fit=90)      # A
    await _company(db_session, "Bravo", "apollo", fit=70)      # B
    await _company(db_session, "Charlie", "thomasnet", fit=55) # C
    await _company(db_session, "Delta", "census", fit=20, stage="disqualified")  # F
    await _company(db_session, "Echo", "manual", fit=None)     # unscored → F

    report = await build_lead_quality_report(db_session, days=30)

    assert report["total_companies"] == 5
    dist = report["grade_distribution"]
    assert dist == {"A": 1, "B": 1, "C": 1, "D": 0, "F": 2}
    assert report["qualified_leads"] == 2          # A + B
    assert report["disqualified_leads"] == 1       # stage == disqualified
    assert report["average_fit_score"] == 58.8     # mean(90,70,55,20)
    assert report["new_leads_by_source"] == {"apollo": 2, "thomasnet": 1, "census": 1, "manual": 1}
    assert report["new_leads_in_window"] == 5


async def test_report_uses_latest_score(db_session):
    now = datetime.now(timezone.utc)
    c = Company(id=uuid.uuid4(), name="Foxtrot", source="apollo", country="US", stage="scored")
    db_session.add(c)
    await db_session.flush()
    db_session.add(LeadScore(id=uuid.uuid4(), company_id=c.id, score_type="rule_based",
                             fit_score=40, scored_at=now - timedelta(days=2)))   # older D
    db_session.add(LeadScore(id=uuid.uuid4(), company_id=c.id, score_type="ai",
                             fit_score=88, scored_at=now))                       # newer A
    await db_session.flush()
    report = await build_lead_quality_report(db_session, days=30)
    # Latest score (88 → A) should win over the older 40 (D).
    assert report["grade_distribution"]["A"] == 1
    assert report["grade_distribution"]["D"] == 0


async def test_report_empty(db_session):
    report = await build_lead_quality_report(db_session, days=30)
    assert report["total_companies"] == 0
    assert report["qualified_leads"] == 0
    assert report["average_fit_score"] == 0.0


async def test_lead_quality_endpoint(client, db_session):
    await _company(db_session, "Golf", "apollo", fit=95)
    await db_session.commit()
    r = await client.get("/api/v1/reports/lead-quality?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["grade_distribution"]["A"] >= 1
    assert "last_prospecting_run" in body


# --- grade_for_fit (pure) ---

def test_grade_for_fit_bands():
    assert grade_for_fit(90) == "A"
    assert grade_for_fit(80) == "A"
    assert grade_for_fit(79) == "B"
    assert grade_for_fit(65) == "B"
    assert grade_for_fit(50) == "C"
    assert grade_for_fit(35) == "D"
    assert grade_for_fit(34) == "F"
    assert grade_for_fit(0) == "F"
    assert grade_for_fit(None) == "F"
