"""Tests for the autonomous follow-up + re-scoring beat task logic.

Exercises the testable *_for_session helpers directly (the Celery wrappers just
add a session + commit + error guard around these)."""

from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.models.lead_score import LeadScore
from app.tasks.followups import (
    generate_stale_lead_followups_for_session,
    rescore_active_leads_for_session,
)


async def _contact(db, company, email, sent_days_ago=None, unsubscribed=False):
    c = Contact(company_id=company.id, first_name="X", email=email,
                source="test", unsubscribed=unsubscribed)
    db.add(c)
    await db.flush()
    if sent_days_ago is not None:
        db.add(EmailSend(
            contact_id=c.id, to_email=email, subject="hi", status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(days=sent_days_ago),
        ))
        await db.flush()
    return c


@pytest.mark.asyncio
async def test_generate_stale_lead_followups(db_session):
    active = Company(name="Active Co", country="US", source="test", stage="contacted")
    inactive = Company(name="New Co", country="US", source="test", stage="new")
    db_session.add_all([active, inactive])
    await db_session.flush()

    stale = await _contact(db_session, active, "stale@a.com", sent_days_ago=30)
    fresh = await _contact(db_session, active, "fresh@a.com", sent_days_ago=1)
    never = await _contact(db_session, active, "never@a.com", sent_days_ago=None)
    unsub = await _contact(db_session, active, "unsub@a.com", sent_days_ago=30, unsubscribed=True)
    # A stale contact at an inactive stage should be ignored.
    await _contact(db_session, inactive, "ignore@n.com", sent_days_ago=60)
    await db_session.flush()

    result = await generate_stale_lead_followups_for_session(db_session)
    await db_session.flush()

    # stale + never-emailed qualify; fresh, unsubscribed, and inactive-stage don't.
    assert result["created"] == 2
    fu = await db_session.execute(select(FollowUp.contact_id))
    flagged = {r[0] for r in fu.all()}
    assert stale.id in flagged
    assert never.id in flagged
    assert fresh.id not in flagged
    assert unsub.id not in flagged


@pytest.mark.asyncio
async def test_stale_followups_skip_existing_open_reminder(db_session):
    active = Company(name="Active2", country="US", source="test", stage="engaged")
    db_session.add(active)
    await db_session.flush()
    c = await _contact(db_session, active, "has@a.com", sent_days_ago=30)
    db_session.add(FollowUp(
        contact_id=c.id, company_id=active.id,
        due_date=datetime.now(timezone.utc), status="pending",
    ))
    await db_session.flush()

    result = await generate_stale_lead_followups_for_session(db_session)
    assert result["created"] == 0


@pytest.mark.asyncio
async def test_rescore_active_leads(db_session):
    active = Company(name="Rescore Co", country="US", source="test", stage="qualified",
                     naics_code="3321", employee_count=150)
    new = Company(name="Untouched", country="US", source="test", stage="new")
    db_session.add_all([active, new])
    await db_session.flush()

    result = await rescore_active_leads_for_session(db_session)
    await db_session.flush()

    assert result["rescored"] == 1
    scores = await db_session.execute(
        select(LeadScore).where(LeadScore.company_id == active.id)
    )
    assert len(list(scores.scalars().all())) == 1
    # The inactive company was not scored.
    none = await db_session.execute(
        select(LeadScore).where(LeadScore.company_id == new.id)
    )
    assert list(none.scalars().all()) == []
