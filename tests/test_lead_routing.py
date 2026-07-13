"""Tests for intelligent lead prioritization & routing (Feature 1.3)."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.lead_score import LeadScore
from app.models.rep_profile import RepProfile
from app.models.user import User, UserRole
from app.security import hash_password
from app.services import lead_router


async def _company(db, **kw) -> Company:
    c = Company(
        id=_uuid.uuid4(), name=kw.pop("name", "Route Co"), country="US", source="test",
        naics_code=kw.pop("naics_code", "3327"), industry_segment=kw.pop("industry_segment", "CNC Machining"),
        pipeline_stage=kw.pop("pipeline_stage", "outreach"), **kw,
    )
    db.add(c)
    await db.flush()
    return c


@pytest.mark.asyncio
async def test_compute_propensity_recent_signals(db_session: AsyncSession):
    company = await _company(db_session)
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="A", email="a@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    # Two very recent signals (a click and a reply) => high propensity + velocity.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="a@x.com", status="replied",
        sent_at=now - timedelta(hours=3), opened_at=now - timedelta(hours=2),
        clicked_at=now - timedelta(hours=2), replied_at=now - timedelta(hours=1),
    ))
    await db_session.commit()

    prop = await lead_router.compute_propensity(db_session, company, now)
    assert prop["propensity_score"] > 30
    assert prop["last_signal_at"] is not None
    assert prop["signal_velocity"] >= 0


@pytest.mark.asyncio
async def test_dormant_lead_low_propensity(db_session: AsyncSession):
    company = await _company(db_session)
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="B", email="b@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    # An old open, nothing recent.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="b@x.com", status="opened",
        sent_at=now - timedelta(days=20), opened_at=now - timedelta(days=19),
    ))
    await db_session.commit()

    prop = await lead_router.compute_propensity(db_session, company, now)
    assert prop["propensity_score"] < 20


@pytest.mark.asyncio
async def test_score_and_route_persists_fields(db_session: AsyncSession):
    company = await _company(db_session)
    await db_session.commit()

    ls = await lead_router.score_and_route(db_session, company)
    await db_session.commit()

    assert ls.propensity_score is not None
    assert "propensity" in ls.factors
    assert "routing" in ls.factors
    # Latest score row carries both fit and propensity.
    fetched = (
        await db_session.execute(select(LeadScore).where(LeadScore.company_id == company.id))
    ).scalars().first()
    assert fetched.fit_score >= 0


@pytest.mark.asyncio
async def test_rep_matching_prefers_segment_expertise(db_session: AsyncSession):
    company = await _company(db_session, industry_segment="CNC Machining")
    expert = User(id=_uuid.uuid4(), email="cnc@x.com", name="CNC Expert",
                  role=UserRole.AGENT.value, password_hash=hash_password("x"), is_active=True)
    generalist = User(id=_uuid.uuid4(), email="gen@x.com", name="Generalist",
                      role=UserRole.AGENT.value, password_hash=hash_password("x"), is_active=True)
    db_session.add_all([expert, generalist])
    await db_session.flush()
    db_session.add(RepProfile(
        id=_uuid.uuid4(), user_id=expert.id, industries=["cnc machining"],
        win_rate_by_segment={"cnc machining": {"won": 8, "lost": 2, "rate": 0.8}},
        overall_win_rate=0.8, total_won=8, total_lost=2,
    ))
    db_session.add(RepProfile(
        id=_uuid.uuid4(), user_id=generalist.id, industries=["food beverage"],
        win_rate_by_segment={}, overall_win_rate=0.2, total_won=2, total_lost=8,
    ))
    await db_session.commit()

    rep_id, reason = await lead_router.recommend_rep(db_session, company)
    assert rep_id == expert.id
    assert reason["segment_win_rate"] == 0.8


@pytest.mark.asyncio
async def test_recompute_rep_profile_from_deals(db_session: AsyncSession):
    rep = User(id=_uuid.uuid4(), email="rep@x.com", name="Rep",
               role=UserRole.AGENT.value, password_hash=hash_password("x"), is_active=True)
    db_session.add(rep)
    await db_session.flush()
    company = await _company(db_session, industry_segment="Metal Stamping")
    contact = Contact(id=_uuid.uuid4(), company_id=company.id, first_name="C", email="c@x.com", source="test")
    db_session.add(contact)
    await db_session.flush()
    # The rep emailed this company (their book), and the deal was won.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=contact.id, to_email="c@x.com", status="sent",
        user_id=rep.id, sent_at=datetime.now(timezone.utc) - timedelta(days=30),
    ))
    db_session.add(Deal(
        id=_uuid.uuid4(), company_id=company.id, title="Won deal", value=10000,
        status="won", deal_stage="closing",
        created_at=datetime.now(timezone.utc) - timedelta(days=20),
        close_date=datetime.now(timezone.utc) - timedelta(days=5),
    ))
    profile = RepProfile(id=_uuid.uuid4(), user_id=rep.id)
    db_session.add(profile)
    await db_session.commit()

    await lead_router.recompute_rep_profile(db_session, profile)
    assert profile.total_won == 1
    assert profile.overall_win_rate == 1.0
    assert "metal stamping" in profile.win_rate_by_segment
    assert profile.avg_deal_velocity_days is not None


@pytest.mark.asyncio
async def test_prioritized_endpoint(client: AsyncClient, db_session: AsyncSession):
    company = await _company(db_session, name="Prio Co")
    await db_session.commit()
    # Score it via the endpoint.
    r = await client.post(f"/api/v1/routing/company/{company.id}/score")
    assert r.status_code == 200, r.text

    resp = await client.get("/api/v1/routing/prioritized", params={"active_only": True})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert any(l["company_id"] == str(company.id) for l in data["leads"])


@pytest.mark.asyncio
async def test_rep_profile_upsert_endpoint(client: AsyncClient, db_session: AsyncSession):
    user = User(id=_uuid.uuid4(), email="upsert@x.com", name="Upsert Rep",
                role=UserRole.AGENT.value, password_hash=hash_password("x"), is_active=True)
    db_session.add(user)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/routing/reps/{user.id}",
        json={"industries": ["cnc machining"], "max_capacity": 40},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["industries"] == ["cnc machining"]
    assert data["max_capacity"] == 40
    assert data["user_name"] == "Upsert Rep"

    listing = await client.get("/api/v1/routing/reps")
    assert listing.status_code == 200
    assert any(p["user_id"] == str(user.id) for p in listing.json())
