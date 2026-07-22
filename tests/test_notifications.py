"""Tests for the notification delivery layer (P0-1).

Before this fix ``NotificationService._send_notification`` only logged and never
delivered, so every reply / open / click / phone-found / domain-health /
sender-health / danger-signal / A/B-winner / deal-escalation alert and the daily
digest were silently dropped. These tests pin that a configured notification
recipient now actually receives mail through the active connected account, and
that the path stays best-effort (skips cleanly, never raises).
"""

import pytest

from app.models.smtp_account import SmtpAccount
import app.services.email_dispatch as email_dispatch
from app.services.notifications import NotificationService

pytestmark = pytest.mark.asyncio

NOTIFY_TO = "ops@landline.app"


class _StubSender:
    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "sent", "transport": "smtp"}


@pytest.fixture
async def connected_account(db_session) -> SmtpAccount:
    account = SmtpAccount(
        email="sender@landline.app", display_name="Landline",
        host="smtp.example.com", port=587, username="sender@landline.app",
        password_encrypted="encrypted", is_active=True, status="connected",
    )
    db_session.add(account)
    await db_session.flush()
    return account


def _svc(monkeypatch, notify_to=NOTIFY_TO):
    svc = NotificationService()
    monkeypatch.setattr(svc.settings, "notification_email", notify_to)
    return svc


async def test_send_notification_delivers_to_configured_recipient(
    db_session, connected_account, monkeypatch
):
    stub = _StubSender()
    monkeypatch.setattr(email_dispatch, "_make_sender", lambda account: stub)
    svc = _svc(monkeypatch)

    result = await svc._send_notification(
        "Subject line", "First para.\n\nSecond para.", db=db_session
    )

    assert result["status"] == "sent"
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["to_email"] == NOTIFY_TO
    assert call["subject"] == "Subject line"
    assert call["body_text"] == "First para.\n\nSecond para."
    # Plain text is wrapped into HTML paragraphs.
    assert "<p>" in call["body_html"] and "Second para." in call["body_html"]


async def test_send_notification_skips_when_no_recipient_configured(
    db_session, connected_account, monkeypatch
):
    stub = _StubSender()
    monkeypatch.setattr(email_dispatch, "_make_sender", lambda account: stub)
    svc = _svc(monkeypatch, notify_to="")

    result = await svc._send_notification("Subj", "Body", db=db_session)
    assert result["status"] == "skipped"
    assert stub.calls == []


async def test_send_notification_no_account_is_reported_not_raised(
    db_session, monkeypatch
):
    """Recipient set but no connected sending account → status, not an exception."""
    svc = _svc(monkeypatch)
    result = await svc._send_notification("Subj", "Body", db=db_session)
    assert result["status"] == "no_account"


async def test_send_notification_swallows_transport_error(
    db_session, connected_account, monkeypatch
):
    class _Boom:
        def send_email(self, **kwargs):
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(email_dispatch, "_make_sender", lambda account: _Boom())
    svc = _svc(monkeypatch)

    result = await svc._send_notification("Subj", "Body", db=db_session)
    assert result["status"] == "error"  # logged + returned, never raised


async def test_notify_reply_delivers_through_the_same_path(
    db_session, connected_account, monkeypatch
):
    """A high-level wrapper (notify_reply) reaches the transport too."""
    stub = _StubSender()
    monkeypatch.setattr(email_dispatch, "_make_sender", lambda account: stub)
    svc = _svc(monkeypatch)
    # notify_reply doesn't take a db, so make the internal session reuse ours.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_session():
        yield db_session

    import app.database_celery as dbc
    monkeypatch.setattr(dbc, "celery_session", _fake_session)

    await svc.notify_reply("Dana Lee", "Acme Stamping", "dana@acme.co")

    assert len(stub.calls) == 1
    assert stub.calls[0]["to_email"] == NOTIFY_TO
    assert "Dana Lee" in stub.calls[0]["body_text"]


async def test_notify_sender_health_change_delivers(
    db_session, connected_account, monkeypatch
):
    stub = _StubSender()
    monkeypatch.setattr(email_dispatch, "_make_sender", lambda account: stub)
    svc = _svc(monkeypatch)

    await svc.notify_sender_health_change(
        "burning@apprend.co", "healthy", "critical",
        {"sent_7d": 40, "bounce_rate_7d": 0.3, "complaint_rate_7d": 0.0, "open_rate_7d": 0.01},
        db=db_session,
    )

    assert len(stub.calls) == 1
    body = stub.calls[0]["body_text"]
    assert "burning@apprend.co" in body
    assert "critical" in body
    assert "healthy" in body
