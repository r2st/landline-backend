"""Engagement-ranked leads — who to call this morning.

The properties that matter:
  * a reply outranks a click outranks an open;
  * recent beats stale, on a continuous curve;
  * both send tables count (the sequence engine writes no EmailSend row);
  * an unsubscribed contact never surfaces, whatever they clicked first.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import engagement_scoring as es


# ─── Pure scoring ────────────────────────────────────────────────────────────


def test_recency_decay_halves_each_half_life():
    now = datetime.now(timezone.utc)
    assert es.recency_factor(now, now) == pytest.approx(1.0)
    assert es.recency_factor(
        now - timedelta(days=es.HALF_LIFE_DAYS), now
    ) == pytest.approx(0.5, abs=0.01)
    assert es.recency_factor(
        now - timedelta(days=2 * es.HALF_LIFE_DAYS), now
    ) == pytest.approx(0.25, abs=0.01)


def test_future_timestamps_clamp_rather_than_amplify():
    """Clock skew between the app and the tracker must not mint a top lead."""
    now = datetime.now(timezone.utc)
    assert es.recency_factor(now + timedelta(days=3), now) == 1.0


def test_naive_timestamps_are_treated_as_utc():
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(days=es.HALF_LIFE_DAYS)).replace(tzinfo=None)
    assert es.recency_factor(naive, now) == pytest.approx(0.5, abs=0.01)


def test_a_click_is_worth_far_more_than_an_open():
    """Apple MPP pre-fetches images; nobody accidentally clicks a link."""
    assert es.EVENT_WEIGHTS["replied"] > es.EVENT_WEIGHTS["clicked"]
    assert es.EVENT_WEIGHTS["clicked"] >= 4 * es.EVENT_WEIGHTS["opened"]


def test_normalize_preserves_order_and_never_saturates():
    scores = [es.normalize_score(r) for r in (1, 10, 60, 300, 10_000)]
    assert scores == sorted(scores)
    assert scores[-1] < 100
    assert es.normalize_score(0) == 0.0
    assert es.normalize_score(-50) == 0.0


def test_tiers_band_the_score():
    assert es.tier_for(95) == "hot"
    assert es.tier_for(50) == "warm"
    assert es.tier_for(20) == "cool"
    assert es.tier_for(0) == "cold"


# ─── Ranking against the database ────────────────────────────────────────────


async def _contact(db, company, email, first="A", last="", **kw):
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name=first, last_name=last,
        email=email, source="test", **kw,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enroll(db, contact):
    seq = (
        await db.execute(__import__("sqlalchemy").select(OutreachSequence).limit(1))
    ).scalars().first()
    if seq is None:
        seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
        db.add(seq)
        await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _event(db, enrollment, event_type, when, step=1):
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=step,
        event_type=event_type, occurred_at=when,
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_empty_system_ranks_nothing(db_session):
    assert await es.rank_contacts(db_session) == []


@pytest.mark.asyncio
async def test_replier_outranks_clicker_outranks_opener(db_session, sample_company):
    now = datetime.now(timezone.utc)
    people = {}
    for name, event in (("replier", "replied"), ("clicker", "clicked"), ("opener", "opened")):
        contact = await _contact(db_session, sample_company, f"{name}@acme.test", first=name)
        enr = await _enroll(db_session, contact)
        await _event(db_session, enr, "sent", now - timedelta(days=2))
        await _event(db_session, enr, event, now - timedelta(days=1))
        people[name] = contact

    ranked = await es.rank_contacts(db_session)
    order = [r["contact_name"] for r in ranked]
    assert order.index("replier") < order.index("clicker") < order.index("opener")


@pytest.mark.asyncio
async def test_recent_click_beats_a_stale_pile_of_opens(db_session, sample_company):
    """The ordering a rep working a call list actually wants."""
    now = datetime.now(timezone.utc)

    fresh = await _contact(db_session, sample_company, "fresh@acme.test", first="fresh")
    fresh_enr = await _enroll(db_session, fresh)
    await _event(db_session, fresh_enr, "clicked", now - timedelta(days=1))

    stale = await _contact(db_session, sample_company, "stale@acme.test", first="stale")
    stale_enr = await _enroll(db_session, stale)
    for i in range(5):
        await _event(db_session, stale_enr, "opened", now - timedelta(days=90 + i))

    ranked = await es.rank_contacts(db_session)
    assert ranked[0]["contact_name"] == "fresh"


@pytest.mark.asyncio
async def test_sequence_engagement_counts_even_with_no_email_send_row(
    db_session, sample_company, sample_contact
):
    """The sequence engine writes outreach_events and no EmailSend row. A
    ranking built on EmailSend alone is blind to every cold send we make."""
    now = datetime.now(timezone.utc)
    enr = await _enroll(db_session, sample_contact)
    await _event(db_session, enr, "sent", now - timedelta(days=3))
    await _event(db_session, enr, "clicked", now - timedelta(hours=6))

    ranked = await es.rank_contacts(db_session)
    assert len(ranked) == 1
    assert ranked[0]["clicked"] == 1
    assert ranked[0]["score"] > 0


@pytest.mark.asyncio
async def test_manual_sends_count_too(db_session, sample_company, sample_contact):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Manual", status="sent", sent_at=now - timedelta(days=2),
        opened_at=now - timedelta(days=1), replied_at=now - timedelta(hours=2),
    ))
    await db_session.flush()

    ranked = await es.rank_contacts(db_session)
    assert ranked[0]["replied"] == 1
    assert ranked[0]["opened"] == 1
    assert ranked[0]["tier"] in ("hot", "warm")


@pytest.mark.asyncio
async def test_unsubscribed_contacts_never_surface(db_session, sample_company):
    """Someone who asked to be left alone has no place in a call list, whatever
    they clicked beforehand."""
    now = datetime.now(timezone.utc)
    optout = await _contact(
        db_session, sample_company, "optout@acme.test", first="optout", unsubscribed=True
    )
    enr = await _enroll(db_session, optout)
    await _event(db_session, enr, "replied", now - timedelta(hours=1))

    assert await es.rank_contacts(db_session) == []

    with_them = await es.rank_contacts(db_session, include_suppressed=True)
    assert len(with_them) == 1
    assert with_them[0]["score"] == 0.0
    assert "do not contact" in " ".join(with_them[0]["reasons"]).lower()


@pytest.mark.asyncio
async def test_bounces_penalise(db_session, sample_company):
    now = datetime.now(timezone.utc)
    clean = await _contact(db_session, sample_company, "clean@acme.test", first="clean")
    clean_enr = await _enroll(db_session, clean)
    await _event(db_session, clean_enr, "opened", now - timedelta(hours=2))

    bouncy = await _contact(db_session, sample_company, "bouncy@acme.test", first="bouncy")
    bouncy_enr = await _enroll(db_session, bouncy)
    await _event(db_session, bouncy_enr, "opened", now - timedelta(hours=2))
    await _event(db_session, bouncy_enr, "bounced", now - timedelta(hours=1))

    ranked = {r["contact_name"]: r for r in await es.rank_contacts(db_session)}
    assert ranked["clean"]["score"] > ranked["bouncy"]["score"]
    assert ranked["bouncy"]["bounced"] == 1


@pytest.mark.asyncio
async def test_reasons_are_human_readable(db_session, sample_company, sample_contact):
    now = datetime.now(timezone.utc)
    enr = await _enroll(db_session, sample_contact)
    await _event(db_session, enr, "sent", now - timedelta(days=2))
    await _event(db_session, enr, "opened", now - timedelta(days=1))
    await _event(db_session, enr, "opened", now - timedelta(hours=5))

    ranked = await es.rank_contacts(db_session)
    reasons = " ".join(ranked[0]["reasons"])
    assert "Opened 2" in reasons
    assert "never clicked" in reasons


@pytest.mark.asyncio
async def test_min_score_and_limit_filter(db_session, sample_company):
    now = datetime.now(timezone.utc)
    for i in range(4):
        contact = await _contact(db_session, sample_company, f"c{i}@acme.test", first=f"c{i}")
        enr = await _enroll(db_session, contact)
        await _event(db_session, enr, "sent", now - timedelta(days=1))
        if i >= 2:
            await _event(db_session, enr, "replied", now - timedelta(hours=1))

    assert len(await es.rank_contacts(db_session, limit=2)) == 2
    engaged = await es.rank_contacts(db_session, min_score=10)
    assert len(engaged) == 2
    assert all(r["replied"] == 1 for r in engaged)


@pytest.mark.asyncio
async def test_summary_counts_tiers(db_session, sample_company, sample_contact):
    now = datetime.now(timezone.utc)
    enr = await _enroll(db_session, sample_contact)
    await _event(db_session, enr, "replied", now - timedelta(hours=1))

    ranked, counts, total = await es.rank_contacts_with_totals(db_session)
    assert sum(counts.values()) == total == len(ranked)


# ─── The endpoint ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ranked_leads_endpoint(client, db_session, sample_company, sample_contact):
    now = datetime.now(timezone.utc)
    enr = await _enroll(db_session, sample_contact)
    await _event(db_session, enr, "sent", now - timedelta(days=2))
    await _event(db_session, enr, "clicked", now - timedelta(hours=3))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/leads/ranked")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_ranked"] == 1
    lead = data["leads"][0]
    assert lead["email"] == sample_contact.email
    assert lead["clicked"] == 1
    assert lead["tier"] in ("hot", "warm", "cool", "cold")
    assert sum(data["tier_counts"].values()) == 1


@pytest.mark.asyncio
async def test_ranked_leads_endpoint_empty_ok(client):
    resp = await client.get("/api/v1/dialtone/leads/ranked")
    assert resp.status_code == 200, resp.text
    assert resp.json()["leads"] == []


@pytest.mark.asyncio
async def test_totals_describe_the_population_not_the_page(
    client, db_session, sample_company
):
    """`total_ranked: 50` on a limit of 50 is the page measuring itself.

    The header above this list is meant to say how many prospects are engaged
    and how they break down by tier. Counting the truncated page instead made
    an org with five hundred warm leads indistinguishable from one with
    fifty-one — and the tier histogram a histogram of the visible rows.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for i in range(5):
        contact = await _contact(db_session, sample_company, f"lead{i}@example.com")
        enrollment = await _enroll(db_session, contact)
        await _event(db_session, enrollment, "replied", now)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/leads/ranked?limit=2")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert len(data["leads"]) == 2, "the page is still a page"
    assert data["total_ranked"] == 5
    assert sum(data["tier_counts"].values()) == 5
