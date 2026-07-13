"""Tests for smart meeting scheduler & pre-meeting briefings (feature 2.5).

Covers heuristic briefing generation, time proposal, post-meeting analysis, the
/meetings router, the smart_reply scheduling-link injection, and the interactions
post-meeting hook. No AI key in tests → heuristic paths, which is what these assert.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.meeting import MeetingBrief
from app.services.meeting_intelligence import get_meeting_intelligence
from app.services.smart_reply import apply_scheduling_link


class TestBriefingGeneration:
    @pytest.mark.asyncio
    async def test_briefing_has_sections(self, db_session, sample_company, sample_contact):
        mi = get_meeting_intelligence()
        content = await mi.generate_briefing(
            db_session, sample_company.id, contact_ids=[sample_contact.id], use_ai=False
        )
        for key in ("background", "pain_points", "objection_prep",
                    "competitive_positioning", "recommended_agenda", "talking_points"):
            assert key in content
        assert content["pain_points"]           # from the industry vertical
        assert content["recommended_agenda"]

    @pytest.mark.asyncio
    async def test_build_and_store_brief(self, db_session, sample_company):
        mi = get_meeting_intelligence()
        brief = await mi.build_and_store_brief(db_session, sample_company.id, use_ai=False)
        assert brief.status == "ready"
        rows = (
            await db_session.execute(select(MeetingBrief).where(MeetingBrief.id == brief.id))
        ).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_unknown_company_raises(self, db_session):
        mi = get_meeting_intelligence()
        with pytest.raises(ValueError):
            await mi.generate_briefing(db_session, uuid.uuid4(), use_ai=False)


class TestTimeProposal:
    def test_proposes_business_day_slots(self):
        mi = get_meeting_intelligence()
        result = mi.propose_meeting_times(count=3, now=datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc))
        assert len(result["proposed_slots"]) == 3
        for iso in result["proposed_slots"]:
            dt = datetime.fromisoformat(iso)
            assert dt.weekday() < 5   # Mon-Fri only


class TestPostMeeting:
    @pytest.mark.asyncio
    async def test_heuristic_analysis_positive(self, db_session, sample_company):
        mi = get_meeting_intelligence()
        analysis = await mi.analyze_post_meeting(
            db_session, company_id=sample_company.id,
            notes="Great call — they want to run a pilot on line 3 next week.",
            use_ai=False,
        )
        assert analysis["sentiment"] == "positive"
        assert any("pilot" in a.lower() for a in analysis["action_items"])


class TestSchedulingLinkInjection:
    def test_meeting_interest_detected(self):
        assert apply_scheduling_link({"intent": "interested", "draft_reply": {"body": "sure"}}) is True
        assert apply_scheduling_link({"intent": "not_interested"}) is False

    def test_link_appended_when_configured(self):
        settings = get_settings()
        original = settings.scheduling_link
        settings.scheduling_link = "https://cal.test/suman"
        try:
            analysis = {"intent": "interested", "draft_reply": {"subject": "Re: x", "body": "Happy to chat."}}
            assert apply_scheduling_link(analysis) is True
            assert "https://cal.test/suman" in analysis["draft_reply"]["body"]
        finally:
            settings.scheduling_link = original


class TestMeetingRouter:
    @pytest.mark.asyncio
    async def test_create_and_get_briefing(self, client, db_session, sample_company):
        resp = await client.post(
            "/api/v1/meetings/briefings",
            json={"company_id": str(sample_company.id), "use_ai": False},
        )
        assert resp.status_code == 201
        bid = resp.json()["id"]
        assert resp.json()["briefing_content"]

        resp = await client.get(f"/api/v1/meetings/briefings/{bid}")
        assert resp.status_code == 200

        resp = await client.get(f"/api/v1/meetings/briefings?company_id={sample_company.id}")
        assert any(b["id"] == bid for b in resp.json())

    @pytest.mark.asyncio
    async def test_post_meeting_endpoint(self, client, db_session, sample_company):
        resp = await client.post(
            "/api/v1/meetings/briefings",
            json={"company_id": str(sample_company.id), "use_ai": False},
        )
        bid = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/meetings/briefings/{bid}/post-meeting",
            json={"notes": "They asked for pricing and a follow up.", "use_ai": False},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        assert resp.json()["post_meeting_analysis"]

    @pytest.mark.asyncio
    async def test_propose_times_endpoint(self, client):
        resp = await client.post("/api/v1/meetings/propose-times", json={"count": 2})
        assert resp.status_code == 200
        assert len(resp.json()["proposed_slots"]) == 2

    @pytest.mark.asyncio
    async def test_schedule_without_calendar_falls_back(self, client, sample_company):
        resp = await client.post(
            "/api/v1/meetings/schedule",
            json={
                "company_id": str(sample_company.id),
                "start": datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc).isoformat(),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["scheduled"] is False


class TestInteractionPostMeetingHook:
    @pytest.mark.asyncio
    async def test_logging_meeting_completes_brief(self, client, db_session, sample_company):
        # Create a ready brief for the company.
        resp = await client.post(
            "/api/v1/meetings/briefings",
            json={"company_id": str(sample_company.id), "use_ai": False},
        )
        bid = resp.json()["id"]

        # Log a meeting interaction with notes → hook runs post-meeting analysis.
        resp = await client.post(
            "/api/v1/interactions",
            json={
                "company_id": str(sample_company.id),
                "interaction_type": "meeting",
                "content": "Good meeting, they want a pilot.",
            },
        )
        assert resp.status_code == 201

        resp = await client.get(f"/api/v1/meetings/briefings/{bid}")
        assert resp.json()["status"] == "completed"
        assert resp.json()["post_meeting_analysis"]
