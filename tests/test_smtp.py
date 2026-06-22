"""Tests for SMTP sender-account management + unified sender dispatch."""

import uuid

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.services import crypto
from app.services import smtp_sender as smtp_sender_mod
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


@pytest.fixture(autouse=True)
def _stub_smtp(monkeypatch):
    """Stub the network-touching SMTP methods so tests never open a socket."""
    monkeypatch.setattr(smtp_sender_mod.SmtpSender, "verify", lambda self: {"ok": True})
    monkeypatch.setattr(
        smtp_sender_mod.SmtpSender,
        "send_email",
        lambda self, **kw: {"status": "sent", "transport": "smtp", "sender": self.sender_email},
    )


def _payload(email="ops@example.com", **over):
    body = {
        "email": email,
        "display_name": "Ops",
        "host": "smtp.example.com",
        "port": 587,
        "username": email,
        "password": "hunter2",
        "use_tls": True,
        "use_ssl": False,
    }
    body.update(over)
    return body


# --- add / list ---

async def test_add_smtp_account_verifies_and_persists(client):
    resp = await client.post("/api/v1/smtp/accounts", json=_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["email"] == "ops@example.com"
    assert data["status"] == "connected"
    assert data["type"] == "smtp"
    # First sender of any kind becomes primary automatically.
    assert data["is_active"] is True
    # The plaintext password must never be returned.
    assert "password" not in data


async def test_add_smtp_account_rejects_bad_connection(client, monkeypatch):
    monkeypatch.setattr(
        smtp_sender_mod.SmtpSender, "verify",
        lambda self: {"ok": False, "detail": "Authentication failed"},
    )
    resp = await client.post("/api/v1/smtp/accounts", json=_payload())
    assert resp.status_code == 400
    assert "Authentication failed" in resp.json()["detail"]


async def test_add_duplicate_email_conflicts(client):
    assert (await client.post("/api/v1/smtp/accounts", json=_payload())).status_code == 201
    resp = await client.post("/api/v1/smtp/accounts", json=_payload())
    assert resp.status_code == 409


async def test_list_accounts(client):
    await client.post("/api/v1/smtp/accounts", json=_payload("a@example.com"))
    await client.post("/api/v1/smtp/accounts", json=_payload("b@example.com"))
    resp = await client.get("/api/v1/smtp/accounts")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# --- primary sender is shared with Gmail ---

async def test_set_active_demotes_gmail_primary(client, db_session):
    gmail = GmailAccount(
        id=uuid.uuid4(), email="g@example.com", google_sub="sub-g",
        refresh_token_encrypted=crypto.encrypt("r"), is_active=True, status="connected",
    )
    db_session.add(gmail)
    await db_session.flush()

    created = (await client.post("/api/v1/smtp/accounts", json=_payload())).json()
    resp = await client.post(f"/api/v1/smtp/accounts/{created['id']}/set-active")
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True

    # Gmail must have been demoted — its status endpoint shows no active account.
    status = (await client.get("/api/v1/gmail/status")).json()
    assert status["active_email"] is None


# --- toggle sending / test / delete ---

async def test_toggle_sending(client):
    created = (await client.post("/api/v1/smtp/accounts", json=_payload())).json()
    resp = await client.post(
        f"/api/v1/smtp/accounts/{created['id']}/toggle-sending",
        json={"sending_enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["sending_enabled"] is False


async def test_test_send_returns_sent(client):
    created = (await client.post("/api/v1/smtp/accounts", json=_payload())).json()
    resp = await client.post(f"/api/v1/smtp/accounts/{created['id']}/test", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"


async def test_delete_promotes_replacement(client):
    a = (await client.post("/api/v1/smtp/accounts", json=_payload("a@example.com"))).json()
    b = (await client.post("/api/v1/smtp/accounts", json=_payload("b@example.com"))).json()
    assert a["is_active"] is True  # first one is primary

    resp = await client.delete(f"/api/v1/smtp/accounts/{a['id']}")
    assert resp.status_code == 204

    accounts = (await client.get("/api/v1/smtp/accounts")).json()
    assert len(accounts) == 1
    assert accounts[0]["id"] == b["id"]
    assert accounts[0]["is_active"] is True


# --- dispatch rotation spans both account types ---

async def test_rotation_includes_smtp_accounts(client, db_session):
    from app.services.email_dispatch import get_sending_accounts, get_next_rotation_account

    await client.post("/api/v1/smtp/accounts", json=_payload("smtp@example.com"))
    gmail = GmailAccount(
        id=uuid.uuid4(), email="gmail@example.com", google_sub="sub-x",
        refresh_token_encrypted=crypto.encrypt("r"), sending_enabled=True, status="connected",
    )
    db_session.add(gmail)
    await db_session.flush()

    senders = await get_sending_accounts(db_session)
    emails = {a.email for a in senders}
    assert {"smtp@example.com", "gmail@example.com"} <= emails

    # Rotation picks a never-used account first; both qualify.
    picked = await get_next_rotation_account(db_session)
    assert picked is not None
