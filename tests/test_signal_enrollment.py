"""F6 — signal-based send timing.

``surface_prospects_on_signals`` reacts to fresh, relevant company news by
finding un-enrolled emailable contacts and either flagging them (default) or
auto-enrolling them. Covers freshness/relevance gating, the un-enrolled filter,
idempotency via ``actioned``, and the auto-enroll path.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment
from app.services.signal_enrollment import surface_prospects_on_signals


def _news(company_id, *, hours_ago=1, relevance=8, category="funding", actioned=False,
          is_actionable=True, title="Raised $10M Series A"):
    return CompanyNews(
        id=uuid.uuid4(), company_id=company_id, title=title,
        discovered_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        category=category, relevance_score=relevance,
        is_actionable=is_actionable, actioned=actioned,
    )


@pytest.mark.asyncio
async def test_fresh_signal_flags_unenrolled_prospect(db_session, sample_company, sample_contact):
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )

    assert result["companies"] == 1
    assert result["flagged"] == 1
    assert result["enrolled"] == 0
    assert result["signals_actioned"] == 1

    followups = (await db_session.execute(select(FollowUp))).scalars().all()
    assert len(followups) == 1
    assert "PRIORITY" in followups[0].reason
    assert followups[0].contact_id == sample_contact.id


@pytest.mark.asyncio
async def test_stale_signal_is_ignored(db_session, sample_company, sample_contact):
    db_session.add(_news(sample_company.id, hours_ago=200))
    await db_session.flush()
    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_low_relevance_non_actionable_signal_ignored(
    db_session, sample_company, sample_contact
):
    db_session.add(_news(sample_company.id, relevance=2, is_actionable=False))
    await db_session.flush()
    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_already_enrolled_contact_is_not_flagged(
    db_session, sample_company, sample_contact
):
    # Contact already in an active sequence → not an un-enrolled prospect.
    from app.models.sequence import OutreachSequence
    seq = OutreachSequence(id=uuid.uuid4(), name="s")
    db_session.add(seq)
    await db_session.flush()
    db_session.add(OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    ))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0
    assert result["companies"] == 0


@pytest.mark.asyncio
async def test_signal_is_marked_actioned_so_it_runs_once(
    db_session, sample_company, sample_contact
):
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    first = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert first["flagged"] == 1

    # Second sweep: the signal is now actioned → nothing new.
    second = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert second["flagged"] == 0

    news = (await db_session.execute(select(CompanyNews))).scalars().all()
    assert all(n.actioned for n in news)


@pytest.mark.asyncio
async def test_unsubscribed_contact_is_skipped(db_session, sample_company):
    c = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="No",
        email="no@acmestamp.com", source="test", unsubscribed=True,
    )
    db_session.add(c)
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=False
    )
    assert result["flagged"] == 0


@pytest.mark.asyncio
async def test_auto_enroll_path_creates_enrollment(db_session, sample_company, sample_contact):
    # Seed the vertical sequence the company resolves to so auto-enroll has a
    # target. sample_company is 'Metal Stamping' → resolve_vertical picks a
    # vertical; create a matching active sequence tagged with its key.
    from app.models.sequence import OutreachSequence
    from app.services.industry_verticals import resolve_vertical

    vertical = resolve_vertical(
        sample_company.industry_segment or "", sample_company.name or ""
    )
    if vertical is None:
        pytest.skip("no vertical resolved for the sample company")
    db_session.add(OutreachSequence(
        id=uuid.uuid4(), name=vertical.sequence_name, industry=vertical.key,
        is_active=True, status="active",
    ))
    db_session.add(_news(sample_company.id))
    await db_session.flush()

    result = await surface_prospects_on_signals(
        db_session, fresh_hours=48, min_relevance=5, auto_enroll=True
    )
    assert result["enrolled"] == 1
    assert result["flagged"] == 0
    n_enroll = (
        await db_session.execute(select(func.count()).select_from(OutreachEnrollment))
    ).scalar_one()
    assert n_enroll == 1
