"""Tests for feature 3.3 — AI Email Deliverability Optimizer."""

import uuid
from datetime import datetime, timezone, timedelta

import pytest

from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.sender_health import SenderHealth
from app.services.deliverability_optimizer import get_deliverability_optimizer

pytestmark = pytest.mark.asyncio


async def _gmail_account(db_session, email="sender@apprend.co", sending=True):
    acct = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=str(uuid.uuid4()),
        refresh_token_encrypted="x", is_active=True, sending_enabled=sending,
        status="connected",
    )
    db_session.add(acct)
    await db_session.flush()
    return acct


async def _email(db_session, acct, status="sent", opened=False, days_ago=1):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db_session.add(EmailSend(
        id=uuid.uuid4(), to_email="lead@x.com", subject="hi", status=status,
        sender_account_id=acct.id, from_email=acct.email, sent_at=ts,
        opened_at=ts if opened else None,
    ))


async def test_content_scan_flags_spam():
    opt = get_deliverability_optimizer()
    result = opt.scan_content_heuristic(
        subject="ACT NOW!!! FREE MONEY",
        body="CLICK HERE to BUY NOW!!! 100% free guarantee. http://a.com http://b.com http://c.com http://d.com",
        has_unsubscribe=False,
    )
    assert result["spam_score"] >= 50
    assert result["risk"] == "high"
    assert result["issues"]


async def test_content_scan_clean_email():
    opt = get_deliverability_optimizer()
    result = opt.scan_content_heuristic(
        subject="quick question about your quoting process",
        body="Hey Dana, saw you run a stamping shop. Are you still quoting in spreadsheets? "
             "We cut a similar shop's turnaround from 2 days to 20 minutes. Worth a 15-min call?",
        has_unsubscribe=True,
    )
    assert result["spam_score"] < 25
    assert result["risk"] == "low"


async def test_assess_account_healthy(db_session):
    acct = await _gmail_account(db_session)
    for _ in range(20):
        await _email(db_session, acct, status="sent", opened=True)
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 20
    assert row.reputation_score >= 80
    assert row.status == "healthy"
    assert row.throttle_factor == 1.0


async def test_assess_account_no_sends_is_pending_not_fake_healthy(db_session):
    # Audit #6: an account that has never sent must NOT report "reputation 100 /
    # healthy" — that fabricated a proven-clean reputation for a sender we had no
    # data on. It is reported as 'pending' with a NULL reputation instead.
    acct = await _gmail_account(db_session)
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 0
    assert row.status == "pending"
    assert row.reputation_score is None
    assert row.throttle_factor == 1.0  # nothing to throttle


async def test_assess_counts_ingested_bounce_records(db_session):
    # Real bounces arrive as DSNs recorded in bounce_records — the ingestion
    # sweep does NOT flip EmailSend.status to "bounced", so health scoring must
    # fold the bounce ledger in or it reports ~0 bounces for a bouncing account.
    from datetime import datetime, timezone
    from app.models.bounce_record import BounceRecord

    acct = await _gmail_account(db_session)
    for _ in range(16):
        await _email(db_session, acct, status="sent")
    # 4 DSN-ingested hard bounces attributed to this sending mailbox.
    for i in range(4):
        db_session.add(BounceRecord(
            id=uuid.uuid4(), gmail_message_id=f"dsn-{i}", gmail_account_id=acct.id,
            recipient_email=f"dead{i}@x.com", bounce_kind="hard", status_code="5.1.1",
            diagnostic="user unknown", processed_at=datetime.now(timezone.utc),
        ))
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 16
    assert row.bounces_7d == 4           # counted from bounce_records, not send status
    assert row.bounce_rate_7d == 0.25
    assert row.throttle_factor < 1.0
    assert row.status in ("throttled", "critical")


async def test_low_volume_clean_account_is_not_critical(db_session):
    # A handful of clean sends must not be thrown to 'critical' by noisy rates —
    # but it is also no longer a fabricated perfect score; it's a real 'healthy'.
    acct = await _gmail_account(db_session)
    for _ in range(3):
        await _email(db_session, acct, status="sent", opened=True)
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 3
    assert row.status == "healthy"
    assert row.reputation_score == 100


async def test_low_volume_with_bounce_is_penalized(db_session):
    acct = await _gmail_account(db_session)
    for _ in range(2):
        await _email(db_session, acct, status="sent")
    await _email(db_session, acct, status="bounced")
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 3
    assert row.reputation_score is not None and row.reputation_score < 100


async def test_assess_account_high_bounce_throttled(db_session):
    acct = await _gmail_account(db_session)
    for _ in range(16):
        await _email(db_session, acct, status="sent")
    for _ in range(4):  # 20% bounce rate
        await _email(db_session, acct, status="bounced")
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.sent_7d == 20
    assert row.bounce_rate_7d == 0.2
    assert row.throttle_factor < 1.0
    assert row.status in ("throttled", "critical")


async def test_rotation_skips_critical_account(db_session):
    from app.services.email_dispatch import get_next_rotation_account

    good = await _gmail_account(db_session, email="good@apprend.co")
    bad = await _gmail_account(db_session, email="bad@apprend.co")
    # Mark the "bad" account critical via a SenderHealth row.
    db_session.add(SenderHealth(
        id=uuid.uuid4(), account_id=bad.id, account_type="gmail", account_email=bad.email,
        reputation_score=10, throttle_factor=0.1, status="critical",
    ))
    await db_session.flush()

    chosen = await get_next_rotation_account(db_session)
    assert chosen is not None
    assert chosen.id == good.id


async def test_send_limiter_account_capacity(db_session):
    from app.services.send_limiter import get_send_limiter

    acct = await _gmail_account(db_session)
    db_session.add(SenderHealth(
        id=uuid.uuid4(), account_id=acct.id, account_type="gmail", account_email=acct.email,
        reputation_score=65, throttle_factor=0.4, status="throttled",
    ))
    await db_session.flush()

    cap = await get_send_limiter().account_capacity(db_session, acct)
    assert cap["throttle_factor"] == 0.4
    assert cap["effective_daily_limit"] < cap["base_daily_limit"]
    assert cap["health_status"] == "throttled"


async def test_deliverability_api(client):
    r = await client.post("/api/v1/deliverability/scan-content", json={
        "subject": "FREE MONEY!!!", "body": "CLICK HERE to BUY NOW", "has_unsubscribe": False,
    })
    assert r.status_code == 200, r.text
    assert r.json()["spam_score"] > 0

    r = await client.post("/api/v1/deliverability/assess")
    assert r.status_code == 200

    r = await client.get("/api/v1/deliverability/senders")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_assess_writes_history_snapshot(db_session):
    from app.models.sender_health import SenderHealthSnapshot
    from sqlalchemy import select

    acct = await _gmail_account(db_session)
    for _ in range(20):
        await _email(db_session, acct, status="sent", opened=True)
    await db_session.flush()

    # Two assessments should leave two dated points for this account.
    await get_deliverability_optimizer().assess_account(db_session, acct)
    await get_deliverability_optimizer().assess_account(db_session, acct)
    await db_session.flush()

    snaps = (
        await db_session.execute(
            select(SenderHealthSnapshot).where(SenderHealthSnapshot.account_id == acct.id)
        )
    ).scalars().all()
    assert len(snaps) == 2
    assert all(s.account_email == acct.email for s in snaps)
    assert all(s.reputation_score >= 80 for s in snaps)


async def test_sender_history_endpoint(client, db_session):
    acct = await _gmail_account(db_session, email="hist@apprend.co")
    for _ in range(20):
        await _email(db_session, acct, status="sent", opened=True)
    await db_session.flush()
    await get_deliverability_optimizer().assess_account(db_session, acct)
    await db_session.commit()

    r = await client.get("/api/v1/deliverability/senders/history?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    series = next((s for s in body if s["account_email"] == "hist@apprend.co"), None)
    assert series is not None
    assert series["account_id"] == str(acct.id)
    assert len(series["points"]) >= 1
    pt = series["points"][0]
    assert set(pt) >= {
        "captured_at", "reputation_score", "bounce_rate_7d",
        "complaint_rate_7d", "throttle_factor", "status",
    }


async def test_sender_history_rejects_bad_window(client):
    # days is bounded 1..365; a nonsense window is a 422, not a silent clamp.
    r = await client.get("/api/v1/deliverability/senders/history?days=0")
    assert r.status_code == 422


# ─── Sender-health degradation alerting (P0-2) ───────────────────────────────

class _SpyNotifier:
    """Captures notify_sender_health_change calls instead of sending mail."""

    def __init__(self):
        self.calls = []

    async def notify_sender_health_change(self, account_email, prior_status, new_status, metrics=None, db=None):
        self.calls.append({
            "email": account_email, "prior": prior_status,
            "new": new_status, "metrics": metrics,
        })


async def test_degrading_transition_alerts_operator(db_session, monkeypatch):
    """healthy → throttled/critical fires exactly one alert."""
    import app.services.notifications as notifications_module

    spy = _SpyNotifier()
    monkeypatch.setattr(notifications_module, "get_notification_service", lambda: spy)

    acct = await _gmail_account(db_session, email="degrading@apprend.co")
    # Seed a prior 'healthy' assessment so there is a state to transition FROM.
    db_session.add(SenderHealth(
        account_type="gmail", account_id=acct.id, account_email=acct.email,
        status="healthy", reputation_score=95,
    ))
    await db_session.flush()

    # Now make it bounce hard so the next assessment drops to throttled/critical.
    for _ in range(16):
        await _email(db_session, acct, status="sent")
    for _ in range(4):
        await _email(db_session, acct, status="bounced")
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.status in ("throttled", "critical")
    assert len(spy.calls) == 1
    assert spy.calls[0]["email"] == "degrading@apprend.co"
    assert spy.calls[0]["prior"] == "healthy"
    assert spy.calls[0]["new"] in ("throttled", "critical")


async def test_first_assessment_does_not_alert(db_session, monkeypatch):
    """No prior row → the first-ever assessment is silent, even if bad."""
    import app.services.notifications as notifications_module

    spy = _SpyNotifier()
    monkeypatch.setattr(notifications_module, "get_notification_service", lambda: spy)

    acct = await _gmail_account(db_session, email="fresh-bad@apprend.co")
    for _ in range(20):
        await _email(db_session, acct, status="bounced")
    await db_session.flush()

    row = await get_deliverability_optimizer().assess_account(db_session, acct)
    assert row.status in ("throttled", "critical")
    assert spy.calls == []  # first observation is not alerted (avoids rollout burst)


async def test_stable_degraded_does_not_realert(db_session, monkeypatch):
    """An already-throttled account does not re-alert on the next assessment."""
    import app.services.notifications as notifications_module

    spy = _SpyNotifier()
    monkeypatch.setattr(notifications_module, "get_notification_service", lambda: spy)

    acct = await _gmail_account(db_session, email="stuck@apprend.co")
    db_session.add(SenderHealth(
        account_type="gmail", account_id=acct.id, account_email=acct.email,
        status="critical", reputation_score=20,
    ))
    await db_session.flush()
    for _ in range(20):
        await _email(db_session, acct, status="bounced")
    await db_session.flush()

    await get_deliverability_optimizer().assess_account(db_session, acct)
    assert spy.calls == []  # critical → critical is not a new degradation
