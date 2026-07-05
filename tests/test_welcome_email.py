"""Tests for the welcome email sent when an admin creates a user account.

Covers the service (``send_welcome_email``) and its integration into the
``POST /api/v1/admin/users`` endpoint.
"""

import pytest
from httpx import AsyncClient

from app.models.smtp_account import SmtpAccount
from app.models.user import User
from app.services import welcome_email as welcome_email_module
from app.services.welcome_email import send_welcome_email

USERS = "/api/v1/admin/users"


class _StubSender:
    """Records the email it was asked to send instead of hitting a transport."""

    def __init__(self):
        self.calls = []

    def send_email(self, **kwargs):
        self.calls.append(kwargs)
        return {"status": "sent", "transport": "smtp", "sender": "stub"}


@pytest.fixture
async def connected_smtp_account(db_session) -> SmtpAccount:
    account = SmtpAccount(
        email="sender@landline.app",
        display_name="Landline Outreach",
        host="smtp.example.com",
        port=587,
        username="sender@landline.app",
        password_encrypted="encrypted",
        is_active=True,
        status="connected",
    )
    db_session.add(account)
    await db_session.flush()
    return account


# ─── Service ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_welcome_email_uses_primary_account(
    db_session, connected_smtp_account, monkeypatch
):
    stub = _StubSender()
    monkeypatch.setattr(welcome_email_module, "_make_sender", lambda account: stub)

    user = User(
        email="newbie@landline.app",
        name="New Bie",
        role="agent",
        password_hash="x",
        must_change_password=True,
    )
    db_session.add(user)
    await db_session.flush()

    sent = await send_welcome_email(db_session, user, "temp-pass-123")

    assert sent is True
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["to_email"] == "newbie@landline.app"
    # Credentials + change-password instruction present in both parts.
    assert "temp-pass-123" in call["body_html"]
    assert "temp-pass-123" in call["body_text"]
    assert "newbie@landline.app" in call["body_html"]
    assert "/login" in call["body_html"]
    assert "change" in call["body_text"].lower()


@pytest.mark.asyncio
async def test_send_welcome_email_no_account_returns_false(db_session):
    """No configured sending account → returns False, does not raise."""
    user = User(
        email="orphan@landline.app",
        name="Orphan",
        role="agent",
        password_hash="x",
        must_change_password=True,
    )
    db_session.add(user)
    await db_session.flush()

    sent = await send_welcome_email(db_session, user, "temp-pass-123")
    assert sent is False


@pytest.mark.asyncio
async def test_send_welcome_email_swallows_transport_error(
    db_session, connected_smtp_account, monkeypatch
):
    """A transport that raises must not propagate — creation cannot be blocked."""

    class _Boom:
        def send_email(self, **kwargs):
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(welcome_email_module, "_make_sender", lambda account: _Boom())

    user = User(
        email="boom@landline.app",
        name="Boom",
        role="agent",
        password_hash="x",
        must_change_password=True,
    )
    db_session.add(user)
    await db_session.flush()

    sent = await send_welcome_email(db_session, user, "temp-pass-123")
    assert sent is False


# ─── Endpoint integration ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_user_sends_welcome_and_reports_flag(
    client: AsyncClient, admin_user, auth_headers, connected_smtp_account, monkeypatch
):
    stub = _StubSender()
    monkeypatch.setattr(welcome_email_module, "_make_sender", lambda account: stub)

    resp = await client.post(
        USERS,
        headers=auth_headers(admin_user),
        json={
            "email": "welcomed@landline.app",
            "name": "Wel Comed",
            "role": "agent",
            "password": "initialpw123",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "welcomed@landline.app"
    assert body["welcome_email_sent"] is True
    assert len(stub.calls) == 1
    assert stub.calls[0]["to_email"] == "welcomed@landline.app"
    assert "initialpw123" in stub.calls[0]["body_text"]


@pytest.mark.asyncio
async def test_create_user_without_account_still_succeeds(
    client: AsyncClient, admin_user, auth_headers
):
    """No sending account configured → user created, flag False, no error."""
    resp = await client.post(
        USERS,
        headers=auth_headers(admin_user),
        json={
            "email": "noemail@landline.app",
            "name": "No Email",
            "role": "agent",
            "password": "initialpw123",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "noemail@landline.app"
    assert body["welcome_email_sent"] is False
    # User really was created and can be listed.
    listing = await client.get(USERS, headers=auth_headers(admin_user))
    assert any(u["email"] == "noemail@landline.app" for u in listing.json())
