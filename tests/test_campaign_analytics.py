"""Cross-campaign analytics — which campaign deserves tomorrow's sends.

Also covers the two columns that keep the rest honest: the AI-composed share and
the mean personalization depth. A campaign's reply rate is uninterpretable
without them, because a dark LLM provider chain silently turns a personalized
campaign into a mail-merge campaign with an identical name and send count.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.contact import Contact
from app.models.deal import Deal
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence


async def _campaign(db, name, industry=None):
    seq = OutreachSequence(
        id=uuid.uuid4(), name=name, industry=industry, is_active=True, status="active"
    )
    db.add(seq)
    await db.flush()
    return seq


async def _enrollment(db, sequence, contact, status="active"):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
        current_step=1, status=status,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _event(db, enrollment, event_type, when=None, metadata=None, step=1):
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=step,
        event_type=event_type, occurred_at=when or datetime.now(timezone.utc),
        event_metadata=metadata or {},
    ))
    await db.flush()


@pytest.mark.asyncio
async def test_empty_ok(client):
    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["campaigns"] == []
    assert data["totals"] is None


@pytest.mark.asyncio
async def test_rates_and_totals(client, db_session, sample_company, sample_contact):
    seq = await _campaign(db_session, "CNC — Deal Cracker", industry="cnc_machining")
    enr = await _enrollment(db_session, seq, sample_contact)
    # Four steps sent; two of them opened, one of those clicked, one replied.
    # The opens sit on different steps because migration 117 makes
    # `(enrollment, step, 'opened')` unique — two opens is two steps opened,
    # which is what the 50% open rate below has always meant.
    for step in range(1, 5):
        await _event(db_session, enr, "sent", step=step)
    await _event(db_session, enr, "opened", step=1)
    await _event(db_session, enr, "opened", step=2)
    await _event(db_session, enr, "clicked", step=1)
    await _event(db_session, enr, "replied", step=1)
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    row = data["campaigns"][0]
    assert row["name"] == "CNC — Deal Cracker"
    assert row["industry"] == "cnc_machining"
    assert row["sent"] == 4
    assert row["open_rate"] == 0.5
    assert row["click_rate"] == 0.25
    assert row["reply_rate"] == 0.25
    assert row["bounce_rate"] == 0.0
    assert row["last_send_at"] is not None

    totals = data["totals"]
    assert totals["sent"] == 4
    assert totals["reply_rate"] == 0.25
    assert totals["name"] == "All campaigns"


@pytest.mark.asyncio
async def test_campaigns_sort_by_the_requested_metric(
    client, db_session, sample_company, sample_contact
):
    """The whole reason the screen exists: rank campaigns against each other."""
    weak = await _campaign(db_session, "Weak")
    strong = await _campaign(db_session, "Strong")

    weak_enr = await _enrollment(db_session, weak, sample_contact)
    for _ in range(10):
        await _event(db_session, weak_enr, "sent")
    await _event(db_session, weak_enr, "opened")

    other = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Pat",
        email="pat@acme.test", source="test",
    )
    db_session.add(other)
    await db_session.flush()
    strong_enr = await _enrollment(db_session, strong, other)
    for _ in range(4):
        await _event(db_session, strong_enr, "sent")
    await _event(db_session, strong_enr, "replied")
    await _event(db_session, strong_enr, "replied")
    await db_session.commit()

    by_reply = await client.get(
        "/api/v1/dialtone/campaigns/analytics", params={"sort_by": "reply_rate"}
    )
    assert [c["name"] for c in by_reply.json()["campaigns"]] == ["Strong", "Weak"]

    by_volume = await client.get(
        "/api/v1/dialtone/campaigns/analytics", params={"sort_by": "sent"}
    )
    assert [c["name"] for c in by_volume.json()["campaigns"]] == ["Weak", "Strong"]


@pytest.mark.asyncio
async def test_bad_sort_key_is_rejected(client):
    """A user-supplied string reaching a sort gets a whitelist, not a getattr."""
    resp = await client.get(
        "/api/v1/dialtone/campaigns/analytics", params={"sort_by": "__class__"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_timeframe_scopes_engagement(client, db_session, sample_company, sample_contact):
    seq = await _campaign(db_session, "Old and new")
    enr = await _enrollment(db_session, seq, sample_contact)
    now = datetime.now(timezone.utc)
    await _event(db_session, enr, "sent", when=now - timedelta(days=60))
    await _event(db_session, enr, "sent", when=now - timedelta(days=2))
    await _event(db_session, enr, "replied", when=now - timedelta(days=1))
    await db_session.commit()

    all_time = await client.get("/api/v1/dialtone/campaigns/analytics")
    assert all_time.json()["campaigns"][0]["sent"] == 2

    recent = await client.get("/api/v1/dialtone/campaigns/analytics", params={"days": 7})
    row = recent.json()["campaigns"][0]
    assert row["sent"] == 1
    assert row["reply_rate"] == 1.0


@pytest.mark.asyncio
async def test_silent_campaigns_are_hidden_unless_asked_for(
    client, db_session, sample_company, sample_contact
):
    await _campaign(db_session, "Never sent")
    live = await _campaign(db_session, "Live")
    enr = await _enrollment(db_session, live, sample_contact)
    await _event(db_session, enr, "sent")
    await db_session.commit()

    default = await client.get("/api/v1/dialtone/campaigns/analytics")
    assert [c["name"] for c in default.json()["campaigns"]] == ["Live"]

    everything = await client.get(
        "/api/v1/dialtone/campaigns/analytics", params={"include_empty": True}
    )
    assert len(everything.json()["campaigns"]) == 2


@pytest.mark.asyncio
async def test_ai_composed_share_and_personalization_depth(
    client, db_session, sample_company, sample_contact
):
    """The column that distinguishes "personalization didn't help" from "the
    provider chain was down and every send was mail-merge"."""
    seq = await _campaign(db_session, "Composed")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", metadata={
        "ai_generated": True,
        "personalization": {"points": ["company_news", "seniority", "location"], "depth": 3},
    })
    await _event(db_session, enr, "sent", metadata={
        "ai_generated": True,
        "personalization": {"points": ["seniority"], "depth": 1},
    })
    await _event(db_session, enr, "sent", metadata={
        "ai_generated": False, "compose_reason": "no API key",
    })
    await _event(db_session, enr, "sent", metadata={
        "ai_generated": False, "compose_reason": "no API key",
    })
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    row = resp.json()["campaigns"][0]
    assert row["sent"] == 4
    assert row["ai_composed"] == 2
    assert row["ai_composed_rate"] == 0.5
    assert row["avg_personalization_depth"] == 2.0  # (3 + 1) / 2


@pytest.mark.asyncio
async def test_conversion_uses_attributed_deals(
    client, db_session, sample_company, sample_contact
):
    seq = await _campaign(db_session, "Converting")
    for _ in range(2):
        enr = await _enrollment(db_session, seq, sample_contact)
        await _event(db_session, enr, "sent")
    db_session.add(Deal(
        id=uuid.uuid4(), company_id=sample_company.id, title="Pilot",
        source_sequence_id=seq.id,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    row = resp.json()["campaigns"][0]
    assert row["deals_attributed"] == 1
    assert row["total_enrollments"] == 2
    assert row["conversion_rate"] == 0.5


@pytest.mark.asyncio
async def test_totals_weight_depth_by_volume(client, db_session, sample_company, sample_contact):
    """An unweighted mean would let a 1-send campaign outvote a 3-send one on the
    headline personalization number."""
    big = await _campaign(db_session, "Big")
    small = await _campaign(db_session, "Small")

    big_enr = await _enrollment(db_session, big, sample_contact)
    for _ in range(3):
        await _event(db_session, big_enr, "sent", metadata={
            "ai_generated": True, "personalization": {"points": ["a", "b"], "depth": 2},
        })

    other = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Sam",
        email="sam@acme.test", source="test",
    )
    db_session.add(other)
    await db_session.flush()
    small_enr = await _enrollment(db_session, small, other)
    await _event(db_session, small_enr, "sent", metadata={
        "ai_generated": True,
        "personalization": {"points": ["a"] * 10, "depth": 10},
    })
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/campaigns/analytics")
    totals = resp.json()["totals"]
    # (2*3 + 10*1) / 4 = 4.0, not the unweighted (2 + 10) / 2 = 6.0
    assert totals["avg_personalization_depth"] == 4.0
