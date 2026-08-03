"""Tests for the lead-generation tracking API: GET /leads/stats, GET
/leads/recent, GET /dialtone/status."""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.prospecting import ProspectCandidate, ProspectingRun
from app.models.sequence import OutreachSequence
from app.org_context import APPREND_ORG_ID
from app.services.timeframes import resolve_timeframe


# ─── auth ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leads_stats_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.get("/api/v1/leads/stats")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_leads_recent_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.get("/api/v1/leads/recent")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_dialtone_status_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.get("/api/v1/dialtone/status")
    assert resp.status_code == 401


# ─── /leads/stats ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leads_stats_empty_ok(client: AsyncClient):
    resp = await client.get("/api/v1/leads/stats")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["counts"]["emails_sent"] == 0
    assert data["conversion_rate"] == 0.0
    assert data["by_org"] == []


@pytest.mark.asyncio
async def test_leads_stats_aggregates_email_and_outreach(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Intro", status="opened", sent_at=now, opened_at=now,
    ))
    seq = OutreachSequence(id=_uuid.uuid4(), name="Cold Seq")
    db_session.add(seq)
    await db_session.flush()
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="sent", occurred_at=now,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/leads/stats", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["counts"]["emails_sent"] == 2  # one EmailSend + one OutreachEvent
    assert data["counts"]["opens"] == 1
    assert data["rates"]["open_rate"] == 0.5

    # Single-org caller (not platform_admin) still gets a by_org breakdown —
    # just one entry, restating their own org's totals.
    assert len(data["by_org"]) == 1
    assert data["by_org"][0]["org_id"] == str(APPREND_ORG_ID)
    assert data["by_org"][0]["counts"]["emails_sent"] == 2


@pytest.mark.asyncio
async def test_leads_stats_qualified_and_deals(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
):
    sample_company.stage = "qualified"
    db_session.add(sample_company)
    await db_session.flush()
    db_session.add(Deal(
        id=_uuid.uuid4(), company_id=sample_company.id, title="Big Deal",
        status="won",
    ))
    db_session.add(Deal(
        id=_uuid.uuid4(), company_id=sample_company.id, title="Pending Deal",
        status="pending",
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/leads/stats", params={"timeframe": "all_time"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["leads_qualified"] >= 1
    assert data["deals_created"] == 2
    assert data["deals_won"] == 1


@pytest.mark.asyncio
async def test_leads_stats_date_range_filter(
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
    resp = await client.get("/api/v1/leads/stats", params={"start": start})
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["emails_sent"] == 1


@pytest.mark.asyncio
async def test_leads_stats_rejects_unknown_timeframe(client: AsyncClient):
    resp = await client.get("/api/v1/leads/stats", params={"timeframe": "never"})
    assert resp.status_code == 400, resp.text


# ─── /leads/recent ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_leads_recent_empty_ok(client: AsyncClient):
    resp = await client.get("/api/v1/leads/recent")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_leads_recent_lists_sends_and_leads(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="Hi", status="sent", sent_at=now,
    ))
    run = ProspectingRun(id=_uuid.uuid4(), status="completed")
    db_session.add(run)
    await db_session.flush()
    db_session.add(ProspectCandidate(
        id=_uuid.uuid4(), run_id=run.id, company_name="Discovered Co",
        source="apollo", fit_score=80, status="qualified", created_at=now,
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/leads/recent")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    types = {i["type"] for i in items}
    assert "email_sent" in types
    assert "lead_discovered" in types
    sent = next(i for i in items if i["type"] == "email_sent")
    assert sent["org_id"] == str(APPREND_ORG_ID)
    assert sent["org_name"]


@pytest.mark.asyncio
async def test_leads_recent_pagination(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    now = datetime.now(timezone.utc)
    for i in range(5):
        db_session.add(EmailSend(
            id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
            subject=f"E{i}", status="sent", sent_at=now - timedelta(minutes=i),
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/leads/recent", params={"page": 1, "page_size": 2})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2


# ─── /dialtone/status ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dialtone_status_shape(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) >= {
        "generated_at", "sending", "warmup", "counts", "rates",
        "active_senders", "healthy_senders", "by_org",
    }
    assert data["sending"]["enabled"] in (True, False)
    for k in ("emails_sent", "opens", "clicks", "replies", "bounces", "errors", "leads_discovered"):
        assert k in data["counts"]


@pytest.mark.asyncio
async def test_dialtone_status_reflects_timeframe(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    tf = resolve_timeframe("this_week")
    db_session.add(EmailSend(
        id=_uuid.uuid4(), contact_id=sample_contact.id, to_email=sample_contact.email,
        subject="This week", status="sent", sent_at=tf.start + timedelta(minutes=1),
    ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/status", params={"timeframe": "this_week"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["timeframe"] == "this_week"
    assert data["counts"]["emails_sent"] == 1
    assert len(data["by_org"]) == 1
    assert data["by_org"][0]["org_id"] == str(APPREND_ORG_ID)


@pytest.mark.asyncio
async def test_dialtone_status_rejects_unknown_timeframe(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/status", params={"timeframe": "nope"})
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_dialtone_status_explicit_bounds_beat_timeframe(
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

    resp = await client.get("/api/v1/dialtone/status", params={
        "timeframe": "today",
        "start": (now - timedelta(days=120)).date().isoformat(),
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["counts"]["emails_sent"] == 1


@pytest.mark.asyncio
async def test_bounce_rate_denominator_is_sends_not_sends_plus_bounces(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """A bounced sequence send is one send, not two.

    ``bounce_handler._suppress_contact`` writes a ``bounced`` OutreachEvent on
    top of the ``sent`` event the send already wrote, so ``sent + bounces``
    counts the same message twice and the reported bounce rate lands below the
    real one — by more, the worse the real one is. Here every one of the four
    sends bounced: the honest answer is 100%, and the old formula said 50%.
    """
    now = datetime.now(timezone.utc)
    seq = OutreachSequence(id=_uuid.uuid4(), name="Dead domain")
    db_session.add(seq)
    await db_session.flush()
    for _ in range(4):
        enrollment = OutreachEnrollment(
            id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        )
        db_session.add(enrollment)
        await db_session.flush()
        db_session.add(OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="sent", occurred_at=now,
        ))
        db_session.add(OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="bounced", occurred_at=now,
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/leads/stats", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["counts"]["emails_sent"] == 4
    assert data["counts"]["bounces"] == 4
    assert data["rates"]["bounce_rate"] == 1.0
    assert data["by_org"][0]["rates"]["bounce_rate"] == 1.0


@pytest.mark.asyncio
async def test_console_bounce_rate_agrees_with_the_auto_pause_rail(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_contact: Contact,
):
    """The console and ``danger_monitor`` must report the same ratio.

    The rail trips on ``bounces / sent``. While the console divided by
    ``sent + bounces`` an operator watching a 10%-threshold alarm saw 9.1% and
    had no way to tell why sending had been paused underneath them.
    """
    from app.services.danger_monitor import DangerMonitor

    tf = resolve_timeframe("today")
    at = tf.start + timedelta(minutes=1)
    seq = OutreachSequence(id=_uuid.uuid4(), name="Ten percent")
    db_session.add(seq)
    await db_session.flush()
    for i in range(10):
        enrollment = OutreachEnrollment(
            id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        )
        db_session.add(enrollment)
        await db_session.flush()
        db_session.add(OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
            event_type="sent", occurred_at=at,
        ))
        if i == 0:
            db_session.add(OutreachEvent(
                id=_uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
                event_type="bounced", occurred_at=at,
            ))
    await db_session.commit()

    resp = await client.get("/api/v1/dialtone/console", params={"timeframe": "today"})
    assert resp.status_code == 200, resp.text
    console_rate = resp.json()["rates"]["bounce_rate"]

    rail = await DangerMonitor()._window_metrics(db_session, hours=24)

    assert console_rate == 0.1
    assert round(rail["bounce_rate"], 4) == console_rate
