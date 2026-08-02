"""Campaign ranking that survives a small sample, and movement over time.

Two defects in the campaign table this pins shut.

**Ranking on the observed rate is ranking on luck.** ``sort_by=reply_rate`` put
1-reply-in-12 (8.3%) above 16-in-400 (4.0%), which is the opposite of the
allocation decision the table exists to make — and the table's whole job is
deciding where a warm-up-capped daily send budget goes.

**A level is not a direction.** A 3% reply rate means something different on the
way up than on the way down, and every number in the response was a level.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.campaign_analytics import wilson_lower as _wilson_lower


async def _campaign(db, name):
    seq = OutreachSequence(
        id=uuid.uuid4(), name=name, is_active=True, status="active"
    )
    db.add(seq)
    await db.flush()
    return seq


async def _enroll(db, sequence, contact):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _events(db, enrollment, event_type, n=1, when=None):
    for _ in range(n):
        db.add(OutreachEvent(
            id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type=event_type,
            occurred_at=when or datetime.now(timezone.utc),
            event_metadata={},
        ))
    await db.flush()


async def _contact(db, company, email):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email=email,
        first_name="Dana", source="test",
    )
    db.add(contact)
    await db.flush()
    return contact


# --- Wilson lower bound ------------------------------------------------------

def test_wilson_lower_is_zero_with_no_trials():
    assert _wilson_lower(0, 0) == 0.0


def test_wilson_lower_never_goes_negative():
    """The textbook normal interval does, exactly where cold outreach lives:
    small samples and rates near zero."""
    for successes, trials in ((0, 5), (0, 500), (1, 1000)):
        assert _wilson_lower(successes, trials) >= 0.0


def test_wilson_lower_punishes_a_small_sample():
    """1-in-12 observes 8.3% and defends almost nothing; 16-in-400 observes 4%
    and defends most of it. This inversion is the whole point."""
    tiny = _wilson_lower(1, 12)
    mature = _wilson_lower(16, 400)
    assert 1 / 12 > 16 / 400          # observed: tiny wins
    assert tiny < mature              # defensible: mature wins


def test_wilson_lower_converges_upward_with_volume():
    """Same rate, more evidence — the bound rises toward the observed rate."""
    small = _wilson_lower(5, 100)
    large = _wilson_lower(50, 1000)
    assert small < large < 0.05


# --- Sample-aware ranking ----------------------------------------------------

@pytest.mark.asyncio
async def test_ci_low_reorders_a_lucky_small_campaign(client, db_session, sample_company):
    lucky = await _campaign(db_session, "Lucky (12 sends)")
    mature = await _campaign(db_session, "Mature (400 sends)")

    lucky_enr = await _enroll(db_session, lucky, await _contact(
        db_session, sample_company, "a@acme.com"))
    await _events(db_session, lucky_enr, "sent", n=12)
    await _events(db_session, lucky_enr, "replied", n=1)

    mature_enr = await _enroll(db_session, mature, await _contact(
        db_session, sample_company, "b@acme.com"))
    await _events(db_session, mature_enr, "sent", n=400)
    await _events(db_session, mature_enr, "replied", n=16)
    await db_session.commit()

    observed = await client.get(
        "/api/v1/dialtone/campaigns/analytics?sort_by=reply_rate"
    )
    assert [c["name"] for c in observed.json()["campaigns"]][0].startswith("Lucky")

    defensible = await client.get(
        "/api/v1/dialtone/campaigns/analytics?sort_by=reply_rate_ci_low"
    )
    assert [c["name"] for c in defensible.json()["campaigns"]][0].startswith("Mature")


@pytest.mark.asyncio
async def test_sample_sufficient_flags_the_thin_rows(client, db_session, sample_company):
    thin = await _campaign(db_session, "Thin")
    enr = await _enroll(db_session, thin, await _contact(
        db_session, sample_company, "c@acme.com"))
    await _events(db_session, enr, "sent", n=10)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    assert resp.json()["campaigns"][0]["sample_sufficient"] is False


@pytest.mark.asyncio
async def test_min_sent_drops_the_noise(client, db_session, sample_company):
    thin = await _campaign(db_session, "Thin")
    thick = await _campaign(db_session, "Thick")
    thin_enr = await _enroll(db_session, thin, await _contact(
        db_session, sample_company, "d@acme.com"))
    await _events(db_session, thin_enr, "sent", n=5)
    thick_enr = await _enroll(db_session, thick, await _contact(
        db_session, sample_company, "e@acme.com"))
    await _events(db_session, thick_enr, "sent", n=100)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics?min_sent=50")
    names = [c["name"] for c in resp.json()["campaigns"]]
    assert names == ["Thick"]


# --- Period-over-period ------------------------------------------------------

@pytest.mark.asyncio
async def test_compare_requires_a_timeframe(client):
    """There is no period before all time, and silently comparing against an
    empty window would report every campaign as newly active."""
    resp = await client.get("/api/v1/dialtone/campaigns/analytics?compare=true")
    assert resp.status_code == 400
    assert "days" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_no_trend_block_unless_asked(client, db_session, sample_company):
    seq = await _campaign(db_session, "Plain")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "f@acme.com"))
    await _events(db_session, enr, "sent", n=40)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics?days=7")
    assert resp.json()["campaigns"][0]["trend"] is None


@pytest.mark.asyncio
async def test_a_declining_campaign_is_named(client, db_session, sample_company):
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Fading")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "g@acme.com"))

    # Previous week: 100 sends, 8 replies (8%).
    await _events(db_session, enr, "sent", n=100, when=now - timedelta(days=10))
    await _events(db_session, enr, "replied", n=8, when=now - timedelta(days=10))
    # This week: 100 sends, 2 replies (2%).
    await _events(db_session, enr, "sent", n=100, when=now - timedelta(days=2))
    await _events(db_session, enr, "replied", n=2, when=now - timedelta(days=2))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    data = resp.json()
    campaign = data["campaigns"][0]
    trend = campaign["trend"]

    assert campaign["sent"] == 100
    assert trend["previous_sent"] == 100
    assert trend["previous_reply_rate"] == pytest.approx(0.08)
    # Absolute difference between the fractions, not a relative change: "down
    # 75%" is the reliably misread number, and unbounded off a zero base.
    assert trend["reply_rate_delta"] == pytest.approx(-0.06)
    assert trend["direction"] == "declining"
    assert data["declining"] == ["Fading"]
    assert data["improving"] == []


@pytest.mark.asyncio
async def test_an_improving_campaign_is_named(client, db_session, sample_company):
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Rising")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "h@acme.com"))
    await _events(db_session, enr, "sent", n=100, when=now - timedelta(days=10))
    await _events(db_session, enr, "replied", n=1, when=now - timedelta(days=10))
    await _events(db_session, enr, "sent", n=100, when=now - timedelta(days=2))
    await _events(db_session, enr, "replied", n=9, when=now - timedelta(days=2))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    data = resp.json()
    assert data["campaigns"][0]["trend"]["direction"] == "improving"
    assert data["improving"] == ["Rising"]


@pytest.mark.asyncio
async def test_two_tiny_periods_are_not_a_trend(client, db_session, sample_company):
    """4 sends last week, 6 this week is not "up 50%" — it is nothing."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Trickle")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "i@acme.com"))
    await _events(db_session, enr, "sent", n=4, when=now - timedelta(days=10))
    await _events(db_session, enr, "sent", n=6, when=now - timedelta(days=2))
    await _events(db_session, enr, "replied", n=1, when=now - timedelta(days=2))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    data = resp.json()
    assert data["campaigns"][0]["trend"]["direction"] == "insufficient_data"
    assert data["improving"] == []
    assert data["declining"] == []


@pytest.mark.asyncio
async def test_a_first_period_of_activity_is_newly_active_not_infinite_growth(
    client, db_session, sample_company
):
    seq = await _campaign(db_session, "Fresh")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "j@acme.com"))
    await _events(db_session, enr, "sent", n=50)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    trend = resp.json()["campaigns"][0]["trend"]
    assert trend["newly_active"] is True
    assert trend["previous_sent"] == 0
    assert trend["direction"] == "insufficient_data"


@pytest.mark.asyncio
async def test_a_flat_campaign_is_flat(client, db_session, sample_company):
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Steady")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "k@acme.com"))
    for offset in (10, 2):
        await _events(db_session, enr, "sent", n=100,
                      when=now - timedelta(days=offset))
        await _events(db_session, enr, "replied", n=4,
                      when=now - timedelta(days=offset))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    data = resp.json()
    assert data["campaigns"][0]["trend"]["direction"] == "flat"
    assert data["improving"] == [] and data["declining"] == []


@pytest.mark.asyncio
async def test_the_period_boundary_is_not_double_counted(client, db_session, sample_company):
    """An event exactly on the boundary belongs to the current period only.
    Two inclusive windows would count it twice and the totals would not add up."""
    now = datetime.now(timezone.utc)
    seq = await _campaign(db_session, "Boundary")
    enr = await _enroll(db_session, seq, await _contact(
        db_session, sample_company, "l@acme.com"))
    # Just inside the current window, near its start.
    await _events(db_session, enr, "sent", n=40,
                  when=now - timedelta(days=7) + timedelta(seconds=30))
    await _events(db_session, enr, "sent", n=40, when=now - timedelta(days=10))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics?days=7&compare=true"
    )
    campaign = resp.json()["campaigns"][0]
    assert campaign["sent"] == 40
    assert campaign["trend"]["previous_sent"] == 40
    assert campaign["trend"]["sent_delta"] == 0


@pytest.mark.asyncio
async def test_an_unknown_sort_key_is_rejected(client):
    resp = await client.get("/api/v1/dialtone/campaigns/analytics?sort_by=drop_table")
    assert resp.status_code == 400
