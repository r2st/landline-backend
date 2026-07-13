"""Tests for Smart Reply objection handling:

- classify_objection: price / timing / referral detection.
- handle_objection: ROI draft, 30-day follow-up, referral task.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.services.smart_reply import (
    classify_objection,
    handle_objection,
    generate_roi_response,
    _extract_referral_name,
    NOT_NOW_FOLLOW_UP_DAYS,
)


# ---------------------------------------------------------------------------
# classify_objection
# ---------------------------------------------------------------------------


class TestClassifyObjection:
    def test_price_objection(self):
        obj = classify_objection("Honestly this is too expensive for us right now.")
        assert obj["type"] == "price"

    def test_price_via_budget(self):
        obj = classify_objection("We have no budget for this.")
        assert obj["type"] == "price"

    def test_timing_objection_keyword(self):
        obj = classify_objection("Not now — maybe circle back next quarter.")
        assert obj["type"] == "timing"

    def test_timing_via_intent(self):
        obj = classify_objection("We'll pass for the moment.", {"intent": "not_now"})
        assert obj["type"] == "timing"

    def test_referral_extracts_name(self):
        obj = classify_objection("You should talk to Karen about this.")
        assert obj["type"] == "referral"
        assert obj["referral_name"] == "Karen"

    def test_referral_via_intent(self):
        obj = classify_objection("That's a purchasing decision.", {"intent": "referral"})
        assert obj["type"] == "referral"

    def test_price_beats_timing(self):
        # A reply that mentions both cost and timing → price wins (more actionable).
        obj = classify_objection("Too expensive and honestly not now.")
        assert obj["type"] == "price"

    def test_no_objection(self):
        obj = classify_objection("Sounds great, let's set up a call.", {"intent": "interested"})
        assert obj is None


class TestExtractReferralName:
    def test_talk_to(self):
        assert _extract_referral_name("please talk to Bob") == "Bob"

    def test_reach_out_to_full_name(self):
        assert _extract_referral_name("reach out to Jane Smith instead") == "Jane Smith"

    def test_none_when_absent(self):
        assert _extract_referral_name("no thanks") is None


# ---------------------------------------------------------------------------
# handle_objection — actions
# ---------------------------------------------------------------------------


class _StubAI:
    def __init__(self, result):
        self._result = result

    async def _complete_json(self, prompt, max_tokens=400):
        return self._result


@pytest.mark.asyncio
async def test_handle_price_returns_roi_draft(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    ai = _StubAI({"subject": "Re: ROI", "body": "here is the payback math - Suman"})
    result = await handle_objection(
        db_session,
        {"type": "price"},
        contact=sample_contact,
        company=sample_company,
        reply_text="too expensive",
        contact_name="John Smith",
        company_name="Acme",
        ai=ai,
    )
    assert result["action"] == "roi_response"
    assert result["draft"]["body"]
    # No follow-up row is created for a price objection (it's a draft, not a task).
    rows = (await db_session.execute(select(FollowUp))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_handle_price_falls_back_without_ai_body(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    ai = _StubAI({"error": "boom"})
    result = await handle_objection(
        db_session, {"type": "price"},
        contact=sample_contact, company=sample_company,
        reply_text="way too pricey", contact_name="John Smith",
        company_name="Acme", ai=ai,
    )
    assert result["action"] == "roi_response"
    assert "Suman" in result["draft"]["body"]


@pytest.mark.asyncio
async def test_handle_timing_schedules_30_day_follow_up(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    result = await handle_objection(
        db_session, {"type": "timing"},
        contact=sample_contact, company=sample_company,
        reply_text="not now, try later", now=now,
    )
    assert result["action"] == "scheduled_follow_up"

    fu = (await db_session.execute(select(FollowUp))).scalar_one()
    assert fu.contact_id == sample_contact.id
    assert fu.company_id == sample_company.id
    assert fu.status == "pending"
    # due_date is 30 days out (compare via the returned ISO string, since SQLite
    # drops tz info on the stored column).
    due = datetime.fromisoformat(result["due_date"])
    assert (due - now).days == NOT_NOW_FOLLOW_UP_DAYS


@pytest.mark.asyncio
async def test_handle_referral_creates_task(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    result = await handle_objection(
        db_session,
        {"type": "referral", "referral_name": "Karen Lee"},
        contact=sample_contact, company=sample_company,
        reply_text="talk to Karen Lee in ops", company_name="Acme",
    )
    assert result["action"] == "referral_task"
    assert result["referral_name"] == "Karen Lee"

    fu = (await db_session.execute(select(FollowUp))).scalar_one()
    assert "Karen Lee" in fu.reason
    assert fu.status == "pending"


@pytest.mark.asyncio
async def test_generate_roi_response_fallback():
    ai = _StubAI({})  # no body → fallback
    draft = await generate_roi_response(
        ai, contact_name="Dana Jones", company_name="Widgets Inc", reply_text="too much money"
    )
    assert draft["subject"]
    assert "Dana" in draft["body"]
    assert "Widgets Inc" in draft["body"]
