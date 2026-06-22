"""Tests for Gmail OAuth connection + per-user email dispatch."""

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.services import crypto, google_oauth
from app.models.gmail_account import GmailAccount


@pytest.fixture(autouse=True)
def _crypto_key():
    """Give crypto a valid Fernet key for the duration of each test."""
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.fixture
def _google_configured():
    settings = get_settings()
    prev = (settings.google_client_id, settings.google_client_secret)
    settings.google_client_id = "test-client-id.apps.googleusercontent.com"
    settings.google_client_secret = "test-secret"
    yield
    settings.google_client_id, settings.google_client_secret = prev


# --- crypto + state ---

def test_crypto_roundtrip():
    token = crypto.encrypt("a-refresh-token")
    assert token != "a-refresh-token"
    assert crypto.decrypt(token) == "a-refresh-token"


def test_state_roundtrip_and_rejects_garbage():
    state = google_oauth.issue_state()
    assert google_oauth.verify_state(state) is True
    assert google_oauth.verify_state("not-a-real-state") is False
    assert google_oauth.verify_state("") is False


# --- endpoints ---

async def test_status_not_connected_reports_fallback(client):
    resp = await client.get("/api/v1/gmail/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connected"] is False
    assert data["configured"] is False
    assert data["fallback_sender"] == get_settings().ses_sender_email
    assert data["accounts"] == []


async def test_authorize_requires_configuration(client):
    resp = await client.get("/api/v1/gmail/oauth/authorize")
    assert resp.status_code == 503


async def test_authorize_returns_consent_url_when_configured(client, _google_configured):
    resp = await client.get("/api/v1/gmail/oauth/authorize")
    assert resp.status_code == 200
    data = resp.json()
    assert "accounts.google.com" in data["authorization_url"]
    assert "gmail.send" in data["authorization_url"]
    assert data["state"]


async def _make_account(db_session, email="vidhi@example.com", active=True):
    account = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=active,
        status="connected",
    )
    db_session.add(account)
    await db_session.flush()
    return account


async def test_status_and_set_active_with_connected_accounts(client, db_session):
    a = await _make_account(db_session, "vidhi@example.com", active=True)
    b = await _make_account(db_session, "suman@example.com", active=False)
    await db_session.commit()

    resp = await client.get("/api/v1/gmail/status")
    data = resp.json()
    assert data["connected"] is True
    assert data["active_email"] == "vidhi@example.com"
    assert len(data["accounts"]) == 2

    # Switch the active sender to Suman.
    resp = await client.post(f"/api/v1/gmail/accounts/{b.id}/set-active")
    assert resp.status_code == 200
    assert resp.json()["email"] == "suman@example.com"

    resp = await client.get("/api/v1/gmail/status")
    assert resp.json()["active_email"] == "suman@example.com"


async def test_disconnect_promotes_replacement(client, db_session, monkeypatch):
    monkeypatch.setattr(google_oauth, "revoke_token", lambda *a, **k: True)
    a = await _make_account(db_session, "vidhi@example.com", active=True)
    b = await _make_account(db_session, "suman@example.com", active=False)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/gmail/accounts/{a.id}")
    assert resp.status_code == 204

    resp = await client.get("/api/v1/gmail/status")
    data = resp.json()
    assert len(data["accounts"]) == 1
    assert data["active_email"] == "suman@example.com"


# --- dispatch / compose ---

async def _default_template(client):
    resp = await client.post(
        "/api/v1/email-templates",
        json={
            "name": "Outreach",
            "subject": "Hello {{first_name}}",
            "body": "<p>Hi {{first_name}} at {{company}}</p>",
            "is_default": True,
        },
    )
    assert resp.status_code == 201
    return resp.json()


async def test_compose_falls_back_to_ses_when_no_gmail(client, sample_contact):
    await _default_template(client)
    resp = await client.post(
        "/api/v1/email-compose",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 200
    data = resp.json()
    # No SES creds configured in tests, so the send is "skipped", but the
    # transport is SES and the From is the shared fallback sender.
    assert data["transport"] == "ses"
    assert data["sender"] == get_settings().ses_sender_email


async def test_compose_uses_gmail_when_connected(client, db_session, sample_contact, monkeypatch):
    sent = {}

    def fake_send(self, **kwargs):
        sent.update(kwargs)
        sent["from"] = self.sender_email
        return {"status": "sent", "message_id": "gmail-123", "transport": "gmail"}

    monkeypatch.setattr("app.services.email_dispatch.GmailSender.send_email", fake_send)

    await _make_account(db_session, "vidhi@example.com", active=True)
    await db_session.commit()
    await _default_template(client)

    resp = await client.post(
        "/api/v1/email-compose",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transport"] == "gmail"
    assert data["sender"] == "vidhi@example.com"
    assert data["sent"] == 1
    assert sent["from"] == "vidhi@example.com"


async def test_daily_send_count_rolls_over_at_day_boundary(client, db_session, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from app.services.email_dispatch import send_outreach_email

    monkeypatch.setattr(
        "app.services.email_dispatch.GmailSender.send_email",
        lambda self, **kw: {"status": "sent", "message_id": "x", "transport": "gmail"},
    )

    account = await _make_account(db_session, "rotate@example.com", active=True)
    # Yesterday's stale counter should reset before today's send is counted.
    account.daily_send_count = 42
    account.daily_count_reset_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    await send_outreach_email(
        db_session, to_email="lead@x.com", subject="Hi", body_html="<p>Hi</p>",
        include_unsubscribe=False,
    )
    await db_session.commit()
    await db_session.refresh(account)
    assert account.daily_send_count == 1
    reset = account.daily_count_reset_at
    if reset.tzinfo is None:
        reset = reset.replace(tzinfo=timezone.utc)
    assert reset.date() == datetime.now(timezone.utc).date()


async def test_daily_send_count_not_incremented_on_skip(client, db_session, monkeypatch):
    from app.services.email_dispatch import send_outreach_email

    monkeypatch.setattr(
        "app.services.email_dispatch.GmailSender.send_email",
        lambda self, **kw: {"status": "error", "transport": "gmail", "detail": "boom"},
    )
    account = await _make_account(db_session, "fails@example.com", active=True)
    await db_session.commit()

    await send_outreach_email(
        db_session, to_email="lead@x.com", subject="Hi", body_html="<p>Hi</p>",
        include_unsubscribe=False,
    )
    await db_session.commit()
    await db_session.refresh(account)
    # Errored send must not count toward the daily total.
    assert account.daily_send_count == 0
