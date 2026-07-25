"""Tests for the Dialtone Console — activity feed + console header."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate, ProspectingRun
from app.models.sequence import OutreachSequence
from app.services.timeframes import resolve_timeframe


# ─── /dialtone/activity ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_activity_empty_ok(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1


@pytest.mark.asyncio
async def test_activity_aggregates_email_engagement(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="Intro",
        status="replied",
        sent_at=now - timedelta(hours=5),
        opened_at=now - timedelta(hours=4),
        clicked_at=now - timedelta(hours=3),
        replied_at=now - timedelta(hours=2),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    types = {i["type"] for i in data["items"]}
    for expected in ("email_sent", "email_opened", "email_clicked", "reply"):
        assert expected in types, f"missing {expected}: {types}"

    # Newest-first ordering.
    timestamps = [i["timestamp"] for i in data["items"]]
    assert timestamps == sorted(timestamps, reverse=True)

    # The sent event carries recipient + subject + status metadata.
    sent = next(i for i in data["items"] if i["type"] == "email_sent")
    assert sent["metadata"]["to_email"] == sample_contact.email
    assert sent["metadata"]["subject"] == "Intro"
    assert sent["source"] == "email_send"


@pytest.mark.asyncio
async def test_activity_includes_outreach_and_leads(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)

    seq = OutreachSequence(id=_uuid.uuid4(), name="Cold Seq")
    db_session.add(seq)
    await db_session.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(),
        enrollment_id=enrollment.id,
        step_number=2,
        event_type="sent",
        occurred_at=now - timedelta(hours=1),
        event_metadata={
            "status": "sent",
            "to_email": sample_contact.email,
            "subject": "Following up on your CNC line",
            "body_html": "<p>Hi — quick question about your machining capacity.</p>",
        },
    ))

    run = ProspectingRun(id=_uuid.uuid4(), status="completed")
    db_session.add(run)
    await db_session.flush()
    db_session.add(ProspectCandidate(
        id=_uuid.uuid4(),
        run_id=run.id,
        company_name="Discovered Widgets Inc",
        source="apollo",
        fit_score=77,
        status="qualified",
        created_at=now - timedelta(minutes=30),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    types = {i["type"] for i in items}
    assert "email_sent" in types
    assert "lead_discovered" in types

    outreach_sent = next(
        i for i in items if i["type"] == "email_sent" and i["source"] == "outreach"
    )
    assert outreach_sent["metadata"]["step_number"] == 2
    # The sequence path has no EmailSend row, so the feed is the only place the
    # sent copy is readable — surface the subject + body it recorded.
    assert outreach_sent["description"] == "Following up on your CNC line"
    assert outreach_sent["metadata"]["subject"] == "Following up on your CNC line"
    assert "machining capacity" in outreach_sent["metadata"]["body_html"]

    lead = next(i for i in items if i["type"] == "lead_discovered")
    assert lead["metadata"]["company_name"] == "Discovered Widgets Inc"
    assert lead["metadata"]["fit_score"] == 77


@pytest.mark.asyncio
async def test_activity_error_event_from_failed_send(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="Broken",
        status="error",
        error="SMTP 550 rejected",
        sent_at=now - timedelta(hours=1),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity", params={"types": "error"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["type"] == "error"
    assert items[0]["severity"] == "error"
    assert items[0]["metadata"]["error"] == "SMTP 550 rejected"


@pytest.mark.asyncio
async def test_activity_filter_by_type(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="A",
        status="opened",
        sent_at=now - timedelta(hours=5),
        opened_at=now - timedelta(hours=4),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/activity", params={"types": "email_opened"})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert items and all(i["type"] == "email_opened" for i in items)


@pytest.mark.asyncio
async def test_activity_invalid_type_400(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/activity", params={"types": "nope"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_activity_filter_by_contact(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    other = Contact(
        id=_uuid.uuid4(),
        company_id=sample_company.id,
        first_name="Jane",
        last_name="Doe",
        email="jane@acmestamp.com",
        source="test",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="To John", status="sent", sent_at=now - timedelta(hours=2),
    ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=other.id, to_email=other.email,
        subject="To Jane", status="sent", sent_at=now - timedelta(hours=1),
    ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/activity", params={"contact_id": str(sample_contact.id)}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["metadata"]["to_email"] == sample_contact.email


@pytest.mark.asyncio
async def test_activity_date_range_filter(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Old", status="sent", sent_at=now - timedelta(days=40),
    ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Recent", status="sent", sent_at=now - timedelta(days=1),
    ))
    await db_session.commit()

    start = (now - timedelta(days=7)).date().isoformat()
    resp = await client.get("/api/v1/dialtone/activity", params={"start": start})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    subjects = {i["metadata"].get("subject") for i in items}
    assert "Recent" in subjects
    assert "Old" not in subjects


@pytest.mark.asyncio
async def test_activity_timeframe_filter(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """A stat tile drills into the same window its number counted."""
    tf = resolve_timeframe("this_week")
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="ThisWeek", status="sent", sent_at=tf.start + timedelta(minutes=1),
    ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="LastWeek", status="sent", sent_at=tf.previous_start + timedelta(minutes=1),
    ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/activity", params={"timeframe": "this_week"}
    )
    assert resp.status_code == 200, resp.text
    subjects = {i["metadata"].get("subject") for i in resp.json()["items"]}
    assert "ThisWeek" in subjects
    assert "LastWeek" not in subjects

    # last_week sees the other one, and only the other one.
    resp = await client.get(
        "/api/v1/dialtone/activity", params={"timeframe": "last_week"}
    )
    subjects = {i["metadata"].get("subject") for i in resp.json()["items"]}
    assert subjects == {"LastWeek"}


@pytest.mark.asyncio
async def test_activity_explicit_bounds_beat_timeframe(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Old", status="sent", sent_at=now - timedelta(days=90),
    ))
    await db_session.commit()

    # `start` is given, so the narrow `today` window must not be applied on top.
    resp = await client.get("/api/v1/dialtone/activity", params={
        "timeframe": "today",
        "start": (now - timedelta(days=120)).date().isoformat(),
    })
    assert resp.status_code == 200, resp.text
    subjects = {i["metadata"].get("subject") for i in resp.json()["items"]}
    assert "Old" in subjects


@pytest.mark.asyncio
async def test_activity_rejects_unknown_timeframe(client: AsyncClient):
    resp = await client.get(
        "/api/v1/dialtone/activity", params={"timeframe": "since_forever"}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_activity_pagination(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    for i in range(5):
        db_session.add(EmailSend(
            id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
            subject=f"E{i}", status="sent", sent_at=now - timedelta(hours=i + 1),
        ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/activity", params={"page": 1, "page_size": 2}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["page"] == 1


# ─── /dialtone/console ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_console_shape(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/console")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) >= {"generated_at", "date", "sending", "warmup", "today", "senders"}
    assert data["sending"]["enabled"] in (True, False)
    assert "daily_limit" in data["warmup"]
    for k in ("emails_sent", "opens", "clicks", "replies", "bounces", "errors", "leads_discovered"):
        assert k in data["today"]


@pytest.mark.asyncio
async def test_console_today_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    # A send + engagement stamped "now" should land in today's window.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Today", status="opened", sent_at=now, opened_at=now,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console")
    assert resp.status_code == 200, resp.text
    today = resp.json()["today"]
    assert today["emails_sent"] >= 1
    assert today["opens"] >= 1
    # No timeframe asked for → the endpoint's historical default, today.
    assert resp.json()["timeframe"] == "today"


# ─── /dialtone/console?timeframe= ────────────────────────────────────────────
#
# Windows come from the resolver rather than hand-rolled dates so the seeded
# timestamps land inside whatever reporting timezone the suite runs under.


@pytest.mark.asyncio
async def test_console_reports_timeframe_context(client: AsyncClient):
    resp = await client.get(
        "/api/v1/dialtone/console", params={"timeframe": "this_week"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["timeframe"] == "this_week"
    assert data["timeframe_label"] == "This week"
    tf = resolve_timeframe("this_week")
    assert data["range_start"] == tf.start_date.isoformat()
    assert data["range_end"] == tf.end_date.isoformat()
    assert data["previous_range_start"] == tf.previous_start_date.isoformat()
    # `stats` is the timeframe-aware field; `today` mirrors it for old clients.
    assert data["stats"] == data["today"]
    for k in ("open_rate", "click_rate", "reply_rate", "bounce_rate"):
        assert k in data["rates"]


@pytest.mark.asyncio
async def test_console_rejects_unknown_timeframe(client: AsyncClient):
    resp = await client.get(
        "/api/v1/dialtone/console", params={"timeframe": "last_fortnight"}
    )
    # A typo must 400, not silently report a different period's numbers.
    assert resp.status_code == 400, resp.text
    assert "this_week" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_console_timeframe_widens_the_window(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """A send earlier this week counts for this_week but not for today."""
    week = resolve_timeframe("this_week")
    today = resolve_timeframe("today")
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Monday", status="sent", sent_at=week.start + timedelta(minutes=1),
    ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Today", status="sent", sent_at=today.start + timedelta(minutes=1),
    ))
    await db_session.commit()

    week_resp = await client.get(
        "/api/v1/dialtone/console", params={"timeframe": "this_week"}
    )
    assert week_resp.json()["stats"]["emails_sent"] == 2

    day_resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    # Monday's send only collapses into "today" when today *is* Monday.
    expected = 2 if week.start == today.start else 1
    assert day_resp.json()["stats"]["emails_sent"] == expected


@pytest.mark.asyncio
async def test_console_previous_period_and_deltas(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    tf = resolve_timeframe("today")
    # Two sends yesterday, one today → -50%.
    for i in range(2):
        db_session.add(EmailSend(
            id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
            subject=f"Yesterday {i}", status="sent",
            sent_at=tf.previous_start + timedelta(minutes=i + 1),
        ))
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Today", status="sent", sent_at=tf.start + timedelta(minutes=1),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stats"]["emails_sent"] == 1
    assert data["previous"]["emails_sent"] == 2
    assert data["deltas"]["emails_sent"] == -50.0
    # Nothing opened in either period → no baseline, so no percentage.
    assert data["deltas"]["opens"] is None


@pytest.mark.asyncio
async def test_console_all_time_has_no_baseline(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Ancient", status="sent", sent_at=now - timedelta(days=400),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "all_time"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # Unbounded: even a year-old send counts.
    assert data["stats"]["emails_sent"] == 1
    assert data["previous"] is None
    assert data["range_start"] is None and data["range_end"] is None
    assert all(v is None for v in data["deltas"].values())


@pytest.mark.asyncio
async def test_console_counts_sequence_sends_in_timeframe(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """Sequence sends live only on OutreachEvent — the timeframe filter must read
    that table too, or the console under-reports most outreach volume."""
    tf = resolve_timeframe("this_week")
    seq = OutreachSequence(id=_uuid.uuid4(), name="Cold Seq")
    db_session.add(seq)
    await db_session.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), sequence_id=seq.id, contact_id=sample_contact.id, status="active",
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1, event_type="sent",
        occurred_at=tf.start + timedelta(minutes=1),
    ))
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1, event_type="opened",
        occurred_at=tf.start + timedelta(minutes=2),
    ))
    # Last week's send belongs to the baseline, not the current window.
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1, event_type="sent",
        occurred_at=tf.previous_start + timedelta(minutes=1),
    ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/dialtone/console", params={"timeframe": "this_week"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["stats"]["emails_sent"] == 1
    assert data["stats"]["opens"] == 1
    assert data["previous"]["emails_sent"] == 1
    # One open on one send → 100% open rate, expressed as a fraction.
    assert data["rates"]["open_rate"] == 1.0
    assert data["rates"]["click_rate"] == 0.0


@pytest.mark.asyncio
async def test_console_funnel_rates(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    tf = resolve_timeframe("today")
    at = tf.start + timedelta(minutes=1)
    for i in range(4):
        db_session.add(EmailSend(
            id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
            subject=f"S{i}", status="sent", sent_at=at,
            opened_at=at if i < 2 else None,
            replied_at=at if i < 1 else None,
        ))
    # A bounce isn't a send, so it lands in the bounce-rate denominator only.
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Bounced", status="bounced", sent_at=at,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    rates = resp.json()["rates"]
    assert rates["open_rate"] == 0.5       # 2 of 4 sends
    assert rates["reply_rate"] == 0.25     # 1 of 4 sends
    assert rates["bounce_rate"] == 0.2     # 1 of 5 attempts


@pytest.mark.asyncio
async def test_console_lists_senders(
    client: AsyncClient,
    db_session: AsyncSession,
):
    db_session.add(GmailAccount(
        id=_uuid.uuid4(),
        email="sender@apprend.tech",
        google_sub="sub-123",
        refresh_token_encrypted="enc",
        sending_enabled=True,
        daily_send_count=3,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console")
    assert resp.status_code == 200, resp.text
    senders = resp.json()["senders"]
    assert any(s["email"] == "sender@apprend.tech" for s in senders)
    s = next(s for s in senders if s["email"] == "sender@apprend.tech")
    assert s["account_type"] == "gmail"
    assert s["used_today"] == 3
    assert "effective_daily_limit" in s
