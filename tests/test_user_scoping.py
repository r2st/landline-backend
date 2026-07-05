"""Tests for per-user scoping of the inbox, email accounts, and settings.

Covers the fix where the inbox and Gmail/SMTP account lists showed every
user's data. When a request carries a JWT Bearer token, endpoints must scope
to the calling user's own connected accounts and their mail — and an
authenticated user with no accounts must see an *empty* inbox, not everyone's.
"""

import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.models.user import User
from app.security import create_access_token, hash_password
from app.services import crypto, google_oauth


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


def _auth_header(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


async def _make_user(db: AsyncSession, email: str, role: str = "agent") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        password_hash=hash_password("pw123456"),
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_gmail(
    db: AsyncSession, email: str, user: User | None = None, active: bool = False
) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        user_id=user.id if user else None,
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=active,
        status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _make_smtp(
    db: AsyncSession, email: str, user: User | None = None
) -> SmtpAccount:
    acct = SmtpAccount(
        id=uuid.uuid4(),
        user_id=user.id if user else None,
        email=email,
        display_name=email.split("@")[0].title(),
        host="smtp.example.com",
        port=587,
        username=email,
        password_encrypted=crypto.encrypt("pw"),
        status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _make_send(db: AsyncSession, from_email: str, to_email: str, subject: str):
    send = EmailSend(
        id=uuid.uuid4(),
        to_email=to_email,
        from_email=from_email,
        subject=subject,
        body_html=f"<p>{subject}</p>",
        status="sent",
        gmail_message_id=f"msg-{uuid.uuid4().hex}",
        gmail_thread_id=f"thr-{uuid.uuid4().hex}",
        sent_at=datetime.now(timezone.utc),
    )
    db.add(send)
    await db.flush()
    return send


async def _make_reply(db: AsyncSession, from_email: str, to_email: str, subject: str):
    reply = EmailReply(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-{uuid.uuid4().hex}",
        gmail_thread_id=f"thr-{uuid.uuid4().hex}",
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text="hi",
        body_html="<p>hi</p>",
        snippet="hi",
        received_at=datetime.now(timezone.utc),
        is_read=False,
    )
    db.add(reply)
    await db.flush()
    return reply


# ---------------------------------------------------------------------------
# Inbox scoping
# ---------------------------------------------------------------------------


class TestInboxScoping:
    async def test_inbox_scoped_to_own_accounts(self, client, db_session):
        """Each user only sees mail on their own connected addresses."""
        alice = await _make_user(db_session, "alice@corp.com")
        bob = await _make_user(db_session, "bob@corp.com")
        await _make_gmail(db_session, "alice@gmail.com", user=alice, active=True)
        await _make_gmail(db_session, "bob@gmail.com", user=bob, active=True)

        # Alice sent one; Bob received one on his address.
        await _make_send(db_session, "alice@gmail.com", "lead1@x.com", "Alice pitch")
        await _make_reply(db_session, "lead2@x.com", "bob@gmail.com", "Re: Bob pitch")

        r_alice = await client.get("/api/v1/emails/inbox", headers=_auth_header(alice))
        assert r_alice.status_code == 200
        subjects = {
            m["subject"]
            for t in r_alice.json()["threads"]
            for m in t["messages"]
        }
        assert subjects == {"Alice pitch"}

        r_bob = await client.get("/api/v1/emails/inbox", headers=_auth_header(bob))
        subjects_bob = {
            m["subject"]
            for t in r_bob.json()["threads"]
            for m in t["messages"]
        }
        assert subjects_bob == {"Re: Bob pitch"}

    async def test_inbox_includes_smtp_sender_address(self, client, db_session):
        """The inbox scopes to SMTP sender addresses too, not just Gmail."""
        alice = await _make_user(db_session, "alice@corp.com")
        await _make_smtp(db_session, "alice@outlook.com", user=alice)
        await _make_send(db_session, "alice@outlook.com", "lead@x.com", "SMTP pitch")

        resp = await client.get("/api/v1/emails/inbox", headers=_auth_header(alice))
        subjects = {
            m["subject"] for t in resp.json()["threads"] for m in t["messages"]
        }
        assert subjects == {"SMTP pitch"}

    async def test_authenticated_user_with_no_accounts_sees_empty_inbox(
        self, client, db_session
    ):
        """Fail closed: a logged-in user with no accounts sees nothing, not all."""
        # Somebody else has mail flowing.
        other = await _make_user(db_session, "other@corp.com")
        await _make_gmail(db_session, "other@gmail.com", user=other, active=True)
        await _make_send(db_session, "other@gmail.com", "lead@x.com", "Not yours")

        newbie = await _make_user(db_session, "newbie@corp.com")
        resp = await client.get("/api/v1/emails/inbox", headers=_auth_header(newbie))
        assert resp.status_code == 200
        assert resp.json()["threads"] == []
        assert resp.json()["total"] == 0

    async def test_unauthenticated_inbox_unscoped_backward_compat(
        self, client, db_session
    ):
        """No token (API-key mode) keeps the legacy unscoped view."""
        alice = await _make_user(db_session, "alice@corp.com")
        await _make_gmail(db_session, "alice@gmail.com", user=alice, active=True)
        await _make_send(db_session, "alice@gmail.com", "lead@x.com", "Visible")

        resp = await client.get("/api/v1/emails/inbox")
        subjects = {
            m["subject"] for t in resp.json()["threads"] for m in t["messages"]
        }
        assert "Visible" in subjects


# ---------------------------------------------------------------------------
# Account list scoping
# ---------------------------------------------------------------------------


class TestAccountScoping:
    async def test_gmail_accounts_scoped_by_user(self, client, db_session):
        alice = await _make_user(db_session, "alice@corp.com")
        bob = await _make_user(db_session, "bob@corp.com")
        await _make_gmail(db_session, "alice@gmail.com", user=alice)
        await _make_gmail(db_session, "bob@gmail.com", user=bob)

        resp = await client.get("/api/v1/gmail/accounts", headers=_auth_header(alice))
        assert resp.status_code == 200
        emails = {a["email"] for a in resp.json()}
        assert emails == {"alice@gmail.com"}

    async def test_gmail_status_scoped_by_user(self, client, db_session):
        alice = await _make_user(db_session, "alice@corp.com")
        bob = await _make_user(db_session, "bob@corp.com")
        await _make_gmail(db_session, "alice@gmail.com", user=alice, active=True)
        await _make_gmail(db_session, "bob@gmail.com", user=bob, active=True)

        resp = await client.get("/api/v1/gmail/status", headers=_auth_header(alice))
        data = resp.json()
        assert {a["email"] for a in data["accounts"]} == {"alice@gmail.com"}
        assert data["active_email"] == "alice@gmail.com"

    async def test_smtp_accounts_scoped_by_user(self, client, db_session):
        alice = await _make_user(db_session, "alice@corp.com")
        bob = await _make_user(db_session, "bob@corp.com")
        await _make_smtp(db_session, "alice@outlook.com", user=alice)
        await _make_smtp(db_session, "bob@outlook.com", user=bob)

        resp = await client.get("/api/v1/smtp/accounts", headers=_auth_header(alice))
        assert {a["email"] for a in resp.json()} == {"alice@outlook.com"}

    async def test_cannot_mutate_another_users_account(self, client, db_session):
        """Ownership check: acting on someone else's account id is a 404."""
        alice = await _make_user(db_session, "alice@corp.com")
        bob = await _make_user(db_session, "bob@corp.com")
        bob_acct = await _make_gmail(db_session, "bob@gmail.com", user=bob)

        resp = await client.post(
            f"/api/v1/gmail/accounts/{bob_acct.id}/set-active",
            headers=_auth_header(alice),
        )
        assert resp.status_code == 404

    async def test_unauthenticated_accounts_unscoped_backward_compat(
        self, client, db_session
    ):
        alice = await _make_user(db_session, "alice@corp.com")
        await _make_gmail(db_session, "alice@gmail.com", user=alice)
        await _make_gmail(db_session, "orphan@gmail.com", user=None)

        resp = await client.get("/api/v1/gmail/accounts")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# OAuth state carries the connecting user
# ---------------------------------------------------------------------------


class TestOAuthStateUserBinding:
    def test_state_roundtrips_user_id(self):
        uid = str(uuid.uuid4())
        state = google_oauth.issue_state(user_id=uid)
        payload = google_oauth.read_state(state)
        assert payload is not None
        assert payload["u"] == uid

    def test_read_state_rejects_expired(self, monkeypatch):
        state = google_oauth.issue_state(user_id="abc")
        # Jump past the TTL.
        real_time = google_oauth.time.time()
        monkeypatch.setattr(
            google_oauth.time, "time", lambda: real_time + google_oauth._STATE_TTL + 1
        )
        assert google_oauth.read_state(state) is None
        assert google_oauth.verify_state(state) is False

    async def test_authorize_embeds_user_id_in_state(
        self, client, db_session
    ):
        settings = get_settings()
        prev = (settings.google_client_id, settings.google_client_secret)
        settings.google_client_id = "test-client.apps.googleusercontent.com"
        settings.google_client_secret = "test-secret"
        try:
            alice = await _make_user(db_session, "alice@corp.com")
            resp = await client.get(
                "/api/v1/gmail/oauth/authorize", headers=_auth_header(alice)
            )
            assert resp.status_code == 200
            payload = google_oauth.read_state(resp.json()["state"])
            assert payload is not None and payload["u"] == str(alice.id)
        finally:
            settings.google_client_id, settings.google_client_secret = prev

    async def test_callback_links_account_to_user(
        self, client, db_session, monkeypatch
    ):
        """The public callback claims the account for the user in the state."""
        from sqlalchemy import select

        settings = get_settings()
        prev = (settings.google_client_id, settings.google_client_secret)
        settings.google_client_id = "test-client.apps.googleusercontent.com"
        settings.google_client_secret = "test-secret"
        try:
            alice = await _make_user(db_session, "alice@corp.com")

            connected = google_oauth.ConnectedAccount(
                email="alice.work@gmail.com",
                google_sub="google-sub-123",
                display_name="Alice Work",
                refresh_token="refresh-token",
                access_token="access-token",
                expires_in=3600,
                scopes=" ".join(google_oauth.SCOPES),
            )
            monkeypatch.setattr(
                google_oauth, "exchange_code", lambda code: connected
            )

            state = google_oauth.issue_state(user_id=str(alice.id))
            resp = await client.get(
                f"/api/v1/gmail/oauth/callback?code=abc&state={state}"
            )
            assert resp.status_code == 200

            acct = (
                await db_session.execute(
                    select(GmailAccount).where(
                        GmailAccount.email == "alice.work@gmail.com"
                    )
                )
            ).scalar_one()
            assert acct.user_id == alice.id
        finally:
            settings.google_client_id, settings.google_client_secret = prev
