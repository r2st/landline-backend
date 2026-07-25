"""The live activity feed — the one you can poll.

``GET /dialtone/activity`` is the archive: it scans up to 5,000 rows per source
and pages through the whole history. That is the right shape for scrolling back
through last month and the wrong shape for a console widget asking "anything
new?" every few seconds. These tests pin the properties that make polling safe:
a bounded lookback, an exclusive ``since`` cursor, newest-first order, and a
server clock the client can render relative times against.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence

pytestmark = pytest.mark.asyncio


async def _contact(db) -> Contact:
    company = Company(id=uuid.uuid4(), name="Acme", domain="acme.com",
                      country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Dana",
        last_name="Reyes", email="dana@acme.com",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _sequence_send(db, contact, *, when, event_type="sent", subject="hi"):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"s-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db.add(enr)
    await db.flush()
    db.add(OutreachEvent(
        enrollment_id=enr.id, step_number=1, event_type=event_type,
        occurred_at=when, event_metadata={"subject": subject, "to_email": contact.email},
    ))
    await db.flush()


async def test_live_feed_returns_newest_first(client, db_session):
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    await _sequence_send(db_session, contact, when=now - timedelta(hours=3), subject="older")
    await _sequence_send(db_session, contact, when=now - timedelta(minutes=5), subject="newer")
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity/live")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert len(body["items"]) == 2
    timestamps = [i["timestamp"] for i in body["items"]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_since_cursor_returns_only_what_is_new(client, db_session):
    """The whole point of polling: the second call is usually empty."""
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    await _sequence_send(db_session, contact, when=now - timedelta(hours=1), subject="first")
    await db_session.commit()

    first = (await client.get("/api/v1/dialtone/activity/live")).json()
    assert len(first["items"]) == 1
    cursor = first["latest_at"]

    # Nothing has happened since.
    quiet = (await client.get(f"/api/v1/dialtone/activity/live?since={cursor}")).json()
    assert quiet["items"] == []
    # A quiet poll leaves the client's cursor alone rather than resetting it.
    assert quiet["latest_at"] is None

    await _sequence_send(db_session, contact, when=now, subject="second")
    await db_session.commit()

    fresh = (await client.get(f"/api/v1/dialtone/activity/live?since={cursor}")).json()
    assert len(fresh["items"]) == 1
    assert fresh["items"][0]["metadata"].get("subject") == "second"


async def test_since_is_exclusive_so_polling_cannot_duplicate(client, db_session):
    """Echoing back latest_at must not re-deliver the event it points at."""
    contact = await _contact(db_session)
    await _sequence_send(
        db_session, contact, when=datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    await db_session.commit()

    first = (await client.get("/api/v1/dialtone/activity/live")).json()
    again = (
        await client.get(f"/api/v1/dialtone/activity/live?since={first['latest_at']}")
    ).json()
    assert again["items"] == []


async def test_lookback_window_bounds_the_feed(client, db_session):
    """An old event must not surface in a feed of "now" — that's what the
    archive endpoint is for, and scanning back to it is what makes polling
    expensive."""
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    await _sequence_send(db_session, contact, when=now - timedelta(days=5), subject="ancient")
    await _sequence_send(db_session, contact, when=now - timedelta(minutes=1), subject="recent")
    await db_session.commit()

    default = (await client.get("/api/v1/dialtone/activity/live")).json()
    subjects = [i["metadata"].get("subject") for i in default["items"]]
    assert "recent" in subjects
    assert "ancient" not in subjects
    assert default["window_hours"] == 24

    # Widening the window brings it back, so nothing is lost — only deferred.
    wide = (await client.get("/api/v1/dialtone/activity/live?window_hours=168")).json()
    assert "ancient" in [i["metadata"].get("subject") for i in wide["items"]]


async def test_limit_is_honoured(client, db_session):
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    for i in range(6):
        await _sequence_send(db_session, contact, when=now - timedelta(minutes=i))
    await db_session.commit()

    body = (await client.get("/api/v1/dialtone/activity/live?limit=3")).json()
    assert len(body["items"]) == 3


async def test_type_filter(client, db_session):
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    await _sequence_send(db_session, contact, when=now - timedelta(minutes=2))
    await _sequence_send(
        db_session, contact, when=now - timedelta(minutes=1), event_type="opened"
    )
    await db_session.commit()

    body = (await client.get("/api/v1/dialtone/activity/live?types=email_opened")).json()
    assert all(i["type"] == "email_opened" for i in body["items"])

    bad = await client.get("/api/v1/dialtone/activity/live?types=not_a_type")
    assert bad.status_code == 400


async def test_server_time_is_returned_for_relative_rendering(client):
    """A viewer with a skewed clock otherwise shows sends happening in the future."""
    body = (await client.get("/api/v1/dialtone/activity/live")).json()
    assert body["server_time"]
    datetime.fromisoformat(body["server_time"])


async def test_manual_sends_appear_alongside_sequence_sends(client, db_session):
    """Both send tables feed the console; reading one under-reports the other."""
    contact = await _contact(db_session)
    now = datetime.now(timezone.utc)
    await _sequence_send(db_session, contact, when=now - timedelta(minutes=2), subject="sequence")
    db_session.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, to_email=contact.email,
        subject="manual", status="sent", sent_at=now - timedelta(minutes=1),
    ))
    await db_session.commit()

    body = (await client.get("/api/v1/dialtone/activity/live")).json()
    sources = {i["source"] for i in body["items"]}
    assert "outreach" in sources
    assert "email_send" in sources
