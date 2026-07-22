"""Unified reply-rate funnel analytics (P1 fix).

Covers the per-sequence / per-step funnel built off OutreachEvent: stage counts,
the positive breakdown read off replied-event metadata, funnel rates, the
sequence filter, and the trailing-window filter.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.funnel_analytics import compute_funnel


async def _contact(db):
    company = Company(id=uuid.uuid4(), name="Corp", source="test", country="US")
    db.add(company)
    await db.flush()
    c = Contact(id=uuid.uuid4(), company_id=company.id,
                email=f"{uuid.uuid4().hex[:8]}@corp.io", first_name="Lead", source="test")
    db.add(c)
    await db.flush()
    return c


async def _seq(db, name):
    seq = OutreachSequence(id=uuid.uuid4(), name=name)
    db.add(seq)
    await db.flush()
    return seq


async def _enrollment(db, contact, seq):
    enr = OutreachEnrollment(id=uuid.uuid4(), contact_id=contact.id,
                             sequence_id=seq.id, current_step=1, status="active")
    db.add(enr)
    await db.flush()
    return enr


def _ev(enr, step, etype, *, positive=None, occurred_at=None):
    meta = {}
    if positive is not None:
        meta["positive"] = positive
    return OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=step,
        event_type=etype, event_metadata=meta,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )


async def test_funnel_counts_and_rates(db_session):
    seq = await _seq(db_session, "Alpha")
    # Two enrollments in one sequence.
    e1 = await _enrollment(db_session, await _contact(db_session), seq)
    e2 = await _enrollment(db_session, await _contact(db_session), seq)

    db_session.add_all([
        # e1: sent → delivered → opened → replied(positive) at step 1
        _ev(e1, 1, "sent"),
        _ev(e1, 1, "delivered"),
        _ev(e1, 1, "opened"),
        _ev(e1, 1, "replied", positive=True),
        # e2: sent → delivered → opened, replied(NOT positive) at step 1
        _ev(e2, 1, "sent"),
        _ev(e2, 1, "delivered"),
        _ev(e2, 1, "opened"),
        _ev(e2, 1, "replied", positive=False),
    ])
    await db_session.commit()

    funnel = await compute_funnel(db_session)
    totals = funnel["totals"]
    assert totals["sent"] == 2
    assert totals["delivered"] == 2
    assert totals["opened"] == 2
    assert totals["replied"] == 2
    assert totals["positive"] == 1  # only e1's reply was flagged positive
    assert totals["reply_rate"] == 100.0
    assert totals["open_rate"] == 100.0
    assert totals["positive_rate"] == 50.0
    assert totals["positive_rate_of_replies"] == 50.0

    assert len(funnel["sequences"]) == 1
    s = funnel["sequences"][0]
    assert s["sequence_name"] == "Alpha"
    assert s["totals"]["positive"] == 1
    assert len(s["steps"]) == 1
    assert s["steps"][0]["step_number"] == 1


async def test_funnel_per_step_breakdown(db_session):
    seq = await _seq(db_session, "Beta")
    enr = await _enrollment(db_session, await _contact(db_session), seq)
    db_session.add_all([
        _ev(enr, 1, "sent"),
        _ev(enr, 1, "opened"),
        _ev(enr, 2, "sent"),
        _ev(enr, 2, "replied", positive=True),
    ])
    await db_session.commit()

    funnel = await compute_funnel(db_session, sequence_id=seq.id)
    s = funnel["sequences"][0]
    steps = {st["step_number"]: st for st in s["steps"]}
    assert steps[1]["sent"] == 1 and steps[1]["opened"] == 1 and steps[1]["replied"] == 0
    assert steps[2]["sent"] == 1 and steps[2]["replied"] == 1 and steps[2]["positive"] == 1


async def test_funnel_sequence_filter_isolates(db_session):
    a = await _seq(db_session, "A")
    b = await _seq(db_session, "B")
    ea = await _enrollment(db_session, await _contact(db_session), a)
    eb = await _enrollment(db_session, await _contact(db_session), b)
    db_session.add_all([_ev(ea, 1, "sent"), _ev(eb, 1, "sent"), _ev(eb, 1, "opened")])
    await db_session.commit()

    only_b = await compute_funnel(db_session, sequence_id=b.id)
    assert len(only_b["sequences"]) == 1
    assert only_b["sequences"][0]["sequence_name"] == "B"
    assert only_b["totals"]["sent"] == 1
    assert only_b["totals"]["opened"] == 1


async def test_funnel_window_filter_excludes_old_events(db_session):
    seq = await _seq(db_session, "Windowed")
    enr = await _enrollment(db_session, await _contact(db_session), seq)
    old = datetime.now(timezone.utc) - timedelta(days=40)
    db_session.add_all([
        _ev(enr, 1, "sent", occurred_at=old),          # outside a 7-day window
        _ev(enr, 1, "sent"),                            # inside
        _ev(enr, 1, "replied", positive=True),          # inside
    ])
    await db_session.commit()

    windowed = await compute_funnel(db_session, days=7)
    assert windowed["totals"]["sent"] == 1  # the 40-day-old send is excluded
    assert windowed["totals"]["positive"] == 1


async def test_funnel_counts_bounces_and_derives_delivered(db_session):
    """Bounced events feed bounce_rate; delivered is derived as sent − bounced."""
    seq = await _seq(db_session, "Bouncy")
    e1 = await _enrollment(db_session, await _contact(db_session), seq)
    e2 = await _enrollment(db_session, await _contact(db_session), seq)
    e3 = await _enrollment(db_session, await _contact(db_session), seq)
    e4 = await _enrollment(db_session, await _contact(db_session), seq)
    db_session.add_all([
        _ev(e1, 1, "sent"),
        _ev(e2, 1, "sent"),
        _ev(e3, 1, "sent"),
        _ev(e4, 1, "sent"),
        # one of the four sends bounced
        _ev(e4, 1, "bounced"),
        _ev(e1, 1, "opened"),
    ])
    await db_session.commit()

    funnel = await compute_funnel(db_session)
    t = funnel["totals"]
    assert t["sent"] == 4
    assert t["bounced"] == 1
    assert t["bounce_rate"] == 25.0
    assert t["delivered"] == 3          # 4 sent − 1 bounced
    assert t["delivered_rate"] == 75.0
    # Per-step carries the same figures.
    step = funnel["sequences"][0]["steps"][0]
    assert step["bounced"] == 1 and step["delivered"] == 3 and step["bounce_rate"] == 25.0


async def test_funnel_empty_is_well_formed(db_session):
    funnel = await compute_funnel(db_session)
    assert funnel["sequences"] == []
    assert funnel["totals"]["sent"] == 0
    assert funnel["totals"]["reply_rate"] == 0.0


async def test_funnel_endpoint_is_wired(client):
    """The /dialtone/analytics/funnel endpoint is registered and org-scoped."""
    resp = await client.get("/api/v1/dialtone/analytics/funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert "totals" in body and "sequences" in body
    assert body["totals"]["sent"] == 0  # empty org
    # Query params accepted.
    resp2 = await client.get("/api/v1/dialtone/analytics/funnel?days=7")
    assert resp2.status_code == 200
    assert resp2.json()["window_days"] == 7
