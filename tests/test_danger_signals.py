"""Tests for Feature 2 — Auto-Pause on Danger Signals.

Covers the windowed rate math, the low-volume rate floor + absolute hard-bounce
floor, the trip → kill-switch flip, the acknowledged resume flow (success +
refusal while still breached + forced override), and the dry-run test endpoint.
"""

import uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.danger_signal import DangerSignalEvent
from app.models.email_template import EmailSend
from app.services import system_settings
from app.services.danger_monitor import get_danger_monitor

pytestmark = pytest.mark.asyncio


# ── helpers ──────────────────────────────────────────────────────────────────


async def _company(db: AsyncSession) -> Company:
    c = Company(id=uuid.uuid4(), name="Acme", domain="acme.com", country="US", source="test")
    db.add(c)
    await db.flush()
    return c


async def _contact(db: AsyncSession, company: Company, *, hard_bounce: bool = False) -> Contact:
    c = Contact(
        id=uuid.uuid4(),
        company_id=company.id,
        first_name="A",
        last_name="B",
        email=f"c{uuid.uuid4().hex[:8]}@x.com",
    )
    if hard_bounce:
        c.last_bounce_type = "hard"
        c.last_bounced_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.add(c)
    await db.flush()
    return c


async def _send(
    db: AsyncSession,
    *,
    status: str = "sent",
    contact: Contact | None = None,
    hours_ago: float = 1.0,
    from_email: str = "sender@apprend.tech",
    account_id: uuid.UUID | None = None,
) -> EmailSend:
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    s = EmailSend(
        id=uuid.uuid4(),
        to_email="lead@x.com",
        subject="hi",
        status=status,
        sent_at=ts,
        from_email=from_email,
        contact_id=contact.id if contact is not None else None,
        sender_account_id=account_id,
    )
    db.add(s)
    await db.flush()
    return s


async def _bulk_sends(db, company, *, total, bounced=0, complaints=0, hard=0, hours_ago=1.0):
    """Create `total` sends, of which `bounced` are bounced (`hard` of them hard),
    and `complaints` are spam complaints. Remaining are clean 'sent'."""
    made = 0
    for _ in range(hard):
        ct = await _contact(db, company, hard_bounce=True)
        await _send(db, status="bounced", contact=ct, hours_ago=hours_ago)
        made += 1
    for _ in range(bounced - hard):
        ct = await _contact(db, company)  # soft/unclassified
        await _send(db, status="bounced", contact=ct, hours_ago=hours_ago)
        made += 1
    for _ in range(complaints):
        await _send(db, status="complained", hours_ago=hours_ago)
        made += 1
    for _ in range(total - made):
        await _send(db, status="sent", hours_ago=hours_ago)
    await db.flush()


def _metric(report, metric, scope="global"):
    return next(m for m in report.metrics if m.metric == metric and m.scope == scope)


# ── windowed rate math ───────────────────────────────────────────────────────


async def test_window_rate_math(db_session: AsyncSession):
    company = await _company(db_session)
    # 100 sends, 6 bounced → 6% bounce rate; 0 complaints.
    await _bulk_sends(db_session, company, total=100, bounced=6)

    report = await get_danger_monitor().evaluate(db_session)
    bounce = _metric(report, "bounce_rate")
    assert bounce.sample_size == 100
    assert round(bounce.observed_value, 4) == 0.06
    assert bounce.status == "breach"  # 6% > 5% and sample >= 20
    assert report.breach is True
    # Complaint rate is 0 → pass.
    assert _metric(report, "complaint_rate").status == "pass"


async def test_complaint_rate_breach(db_session: AsyncSession):
    company = await _company(db_session)
    # 1000 sends, 2 complaints → 0.2% complaint rate (> 0.1% threshold).
    await _bulk_sends(db_session, company, total=1000, complaints=2)

    report = await get_danger_monitor().evaluate(db_session)
    complaint = _metric(report, "complaint_rate")
    assert complaint.sample_size == 1000
    assert round(complaint.observed_value, 4) == 0.002
    assert complaint.status == "breach"
    assert report.breach is True


async def test_bounce_rate_warn_band(db_session: AsyncSession):
    company = await _company(db_session)
    # 100 sends, 4 bounced → 4% (> 3% warn, <= 5% breach) → warn, not breach.
    await _bulk_sends(db_session, company, total=100, bounced=4)

    report = await get_danger_monitor().evaluate(db_session)
    bounce = _metric(report, "bounce_rate")
    assert bounce.status == "warn"
    assert report.breach is False
    assert report.overall_status == "watch"


# ── low-volume floor ─────────────────────────────────────────────────────────


async def test_low_volume_rate_floor_does_not_trip(db_session: AsyncSession):
    company = await _company(db_session)
    # 10 sends, 2 bounced = 20% — a huge rate, but below the 20-send min sample,
    # and the bounces are soft (not hard), so NOTHING trips.
    await _bulk_sends(db_session, company, total=10, bounced=2, hard=0)

    report = await get_danger_monitor().evaluate(db_session)
    assert _metric(report, "bounce_rate").status == "pass"  # sample floor protects warm-up
    assert _metric(report, "hard_bounce_count").status == "pass"
    assert report.breach is False


async def test_hard_bounce_floor_trips_at_low_volume(db_session: AsyncSession):
    company = await _company(db_session)
    # Only 10 sends (below rate min-sample), but 5 are HARD bounces → the
    # absolute floor trips regardless of sample size.
    await _bulk_sends(db_session, company, total=10, bounced=5, hard=5)

    report = await get_danger_monitor().evaluate(db_session)
    hard = _metric(report, "hard_bounce_count")
    assert hard.observed_value == 5.0
    assert hard.status == "breach"
    # The rate metric is still gated out by the low sample.
    assert _metric(report, "bounce_rate").status == "pass"
    assert report.breach is True
    # Hard bounce should be the primary breach → drives the reason string.
    primary = report.primary_breach()
    assert primary.metric == "hard_bounce_count"


async def test_window_excludes_old_sends(db_session: AsyncSession):
    company = await _company(db_session)
    # 6% bounce but all 48h ago → outside the 24h window → no breach.
    await _bulk_sends(db_session, company, total=100, bounced=6, hours_ago=48)

    report = await get_danger_monitor().evaluate(db_session)
    assert _metric(report, "bounce_rate").sample_size == 0
    assert report.breach is False


# ── trip → kill-switch flip ──────────────────────────────────────────────────


async def test_trip_flips_kill_switch_and_records_event(db_session: AsyncSession):
    company = await _company(db_session)
    await _bulk_sends(db_session, company, total=100, bounced=8)

    assert await system_settings.is_sending_enabled(db_session) is True

    monitor = get_danger_monitor()
    report = await monitor.evaluate(db_session)
    assert report.breach is True

    event = await monitor.trip(db_session, report)
    await db_session.flush()

    # Kill switch is now OFF.
    assert await system_settings.is_sending_enabled(db_session) is False
    # One event recorded, capturing the breach.
    assert event is not None
    assert event.action_taken == "auto_paused"
    assert event.metric == "bounce_rate"
    assert event.resolved_at is None
    rows = (await db_session.execute(select(DangerSignalEvent))).scalars().all()
    assert len(rows) == 1
    assert "reason" in rows[0].details
    assert rows[0].details["breaches"]


async def test_task_does_not_trip_when_no_breach(db_session: AsyncSession):
    company = await _company(db_session)
    await _bulk_sends(db_session, company, total=100, bounced=1)  # 1% — healthy

    monitor = get_danger_monitor()
    report = await monitor.evaluate(db_session)
    assert report.breach is False
    # Sending stays enabled; no events.
    assert await system_settings.is_sending_enabled(db_session) is True
    rows = (await db_session.execute(select(DangerSignalEvent))).scalars().all()
    assert rows == []


# ── endpoints: signals + dry-run test ────────────────────────────────────────


async def test_signals_endpoint_healthy_when_empty(client: AsyncClient):
    resp = await client.get("/api/v1/dialtone/health/signals")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["overall_status"] == "healthy"
    assert data["breach"] is False
    assert data["active_trip"] is None
    assert data["thresholds"]["bounce_rate"] == 0.05


async def test_test_endpoint_is_dry_run(
    client: AsyncClient, db_session: AsyncSession
):
    company = await _company(db_session)
    await _bulk_sends(db_session, company, total=100, bounced=8)
    await db_session.commit()

    resp = await client.post("/api/v1/dialtone/health/signals/test")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["would_pause"] is True
    assert data["would_pause_reason"]
    assert any(b["metric"] == "bounce_rate" for b in data["breaches"])
    # Crucially: the dry-run did NOT actually pause.
    assert data["sending_enabled"] is True
    assert await system_settings.is_sending_enabled(db_session) is True
    # And no event was recorded.
    rows = (await db_session.execute(select(DangerSignalEvent))).scalars().all()
    assert rows == []


# ── endpoints: acknowledged resume ───────────────────────────────────────────


async def _seed_active_trip(db: AsyncSession) -> DangerSignalEvent:
    """Simulate an auto-pause: a paused kill switch + one unresolved trip."""
    event = DangerSignalEvent(
        id=uuid.uuid4(),
        metric="bounce_rate",
        scope="global",
        window="24h",
        observed_value=0.08,
        threshold=0.05,
        sample_size=100,
        action_taken="auto_paused",
        details={"reason": "auto-pause: bounce_rate 8.00% > 5% on all senders (24h, n=100)"},
    )
    db.add(event)
    await system_settings.set_sending_enabled(
        db, enabled=False, updated_by="dialtone:auto-pause", reason="auto-pause test"
    )
    await db.commit()
    return event


async def test_resume_refuses_without_active_trip(client: AsyncClient):
    resp = await client.post(
        "/api/v1/dialtone/resume",
        json={"acknowledge_danger_signal_id": str(uuid.uuid4()), "note": "all good"},
    )
    assert resp.status_code == 409
    assert "no active auto-pause" in resp.json()["detail"].lower()


async def test_resume_wrong_ack_id_refused(
    client: AsyncClient, db_session: AsyncSession
):
    await _seed_active_trip(db_session)
    resp = await client.post(
        "/api/v1/dialtone/resume",
        json={"acknowledge_danger_signal_id": str(uuid.uuid4()), "note": "x"},
    )
    assert resp.status_code == 409
    assert "most recent" in resp.json()["detail"].lower()


async def test_resume_with_ack_success(
    client: AsyncClient, db_session: AsyncSession
):
    event = await _seed_active_trip(db_session)
    # No breaching data present → live re-check passes → resume allowed.
    resp = await client.post(
        "/api/v1/dialtone/resume",
        json={"acknowledge_danger_signal_id": str(event.id), "note": "cleaned the list"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["resumed"] is True
    assert data["sending_enabled"] is True
    assert data["forced"] is False
    assert str(event.id) in data["resolved_event_ids"]

    # Sending is back on and the event is resolved.
    assert await system_settings.is_sending_enabled(db_session) is True
    refreshed = (
        await db_session.execute(
            select(DangerSignalEvent).where(DangerSignalEvent.id == event.id)
        )
    ).scalar_one()
    assert refreshed.resolved_at is not None
    assert refreshed.resolved_by.startswith("operator:")
    assert refreshed.resolve_note == "cleaned the list"


async def test_resume_refused_while_still_breached_then_forced(
    client: AsyncClient, db_session: AsyncSession
):
    company = await _company(db_session)
    # Live breach still present.
    await _bulk_sends(db_session, company, total=100, bounced=9)
    event = await _seed_active_trip(db_session)

    # Plain resume is refused (409) because metrics are still breaching.
    resp = await client.post(
        "/api/v1/dialtone/resume",
        json={"acknowledge_danger_signal_id": str(event.id), "note": "trust me"},
    )
    assert resp.status_code == 409
    assert "still active" in resp.json()["detail"].lower()
    assert await system_settings.is_sending_enabled(db_session) is False  # unchanged

    # Forced resume overrides (logged), still_breaching flagged.
    resp = await client.post(
        "/api/v1/dialtone/resume",
        json={
            "acknowledge_danger_signal_id": str(event.id),
            "note": "known issue, sending anyway",
            "force": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["resumed"] is True
    assert data["forced"] is True
    assert data["still_breaching"] is True
    assert await system_settings.is_sending_enabled(db_session) is True
    refreshed = (
        await db_session.execute(
            select(DangerSignalEvent).where(DangerSignalEvent.id == event.id)
        )
    ).scalar_one()
    assert refreshed.resolved_at is not None
    assert "FORCED" in (refreshed.resolve_note or "")


# ── events history endpoint ──────────────────────────────────────────────────


async def test_events_endpoint_lists_trips(
    client: AsyncClient, db_session: AsyncSession
):
    await _seed_active_trip(db_session)
    resp = await client.get("/api/v1/dialtone/health/signals/events")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["metric"] == "bounce_rate"
    assert data["items"][0]["action_taken"] == "auto_paused"

    # Unresolved filter finds it; resolved filter does not.
    resp = await client.get("/api/v1/dialtone/health/signals/events?resolved=false")
    assert resp.json()["total"] == 1
    resp = await client.get("/api/v1/dialtone/health/signals/events?resolved=true")
    assert resp.json()["total"] == 0


# ── activity feed surfaces the trip ──────────────────────────────────────────


async def test_activity_feed_shows_auto_paused(
    client: AsyncClient, db_session: AsyncSession
):
    await _seed_active_trip(db_session)
    resp = await client.get("/api/v1/dialtone/activity?types=auto_paused")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] >= 1
    ev = data["items"][0]
    assert ev["type"] == "auto_paused"
    assert ev["severity"] == "error"
    assert ev["source"] == "danger_monitor"
