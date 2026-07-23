"""Tests for Inbox analytics (volume, response time, intent breakdown)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


async def _make_gmail_account(db, email, user_id=None):
    account = GmailAccount(
        id=uuid.uuid4(), email=email, google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=True, status="connected", user_id=user_id,
    )
    db.add(account)
    await db.flush()
    return account


async def _make_send(db, *, thread_id, sent_at, from_email="us@company.com", to_email="lead@x.com"):
    send = EmailSend(
        id=uuid.uuid4(), from_email=from_email, to_email=to_email, subject="Hi",
        body_html="<p>hi</p>", status="sent", gmail_thread_id=thread_id,
        gmail_message_id=f"snd-{uuid.uuid4().hex[:8]}", sent_at=sent_at,
    )
    db.add(send)
    await db.flush()
    return send


async def _make_reply(db, *, thread_id, received_at, is_read=False, classification=None,
                      to_email="us@company.com", from_email="lead@x.com"):
    reply = EmailReply(
        id=uuid.uuid4(), from_email=from_email, to_email=to_email, subject="Re: hi",
        body_text="hello", body_html="<p>hello</p>", snippet="hello",
        gmail_thread_id=thread_id, gmail_message_id=f"rep-{uuid.uuid4().hex[:8]}",
        received_at=received_at, is_read=is_read, reply_classification=classification,
    )
    db.add(reply)
    await db.flush()
    return reply


class TestAnalytics:
    async def test_volume_and_unread_counts(self, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        now = datetime.now(timezone.utc)
        await _make_send(db_session, thread_id="t1", sent_at=now - timedelta(days=1))
        await _make_reply(db_session, thread_id="t1", received_at=now - timedelta(hours=12), is_read=False)
        await _make_reply(db_session, thread_id="t2", received_at=now - timedelta(hours=6), is_read=True)
        await db_session.commit()

        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, days=30)
        assert a["sent_count"] == 1
        assert a["received_count"] == 2
        assert a["unread_count"] == 1

    async def test_window_excludes_old_messages(self, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        now = datetime.now(timezone.utc)
        await _make_reply(db_session, thread_id="t-old", received_at=now - timedelta(days=45))
        await _make_reply(db_session, thread_id="t-new", received_at=now - timedelta(days=2))
        await db_session.commit()

        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, days=30)
        assert a["received_count"] == 1

    async def test_response_time_measured(self, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        now = datetime.now(timezone.utc)
        # Inbound at T, our reply 2 hours later in the same thread.
        await _make_reply(db_session, thread_id="t-resp", received_at=now - timedelta(hours=5))
        await _make_send(db_session, thread_id="t-resp", sent_at=now - timedelta(hours=3))
        await db_session.commit()

        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, days=30)
        assert a["responded_count"] == 1
        assert a["avg_response_seconds"] == pytest.approx(2 * 3600, rel=0.01)

    async def test_needs_reply_count(self, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        now = datetime.now(timezone.utc)
        # Thread whose latest message is inbound → needs reply.
        await _make_reply(db_session, thread_id="t-nr", received_at=now - timedelta(hours=1))
        # Thread we already answered → not needs-reply.
        await _make_reply(db_session, thread_id="t-done", received_at=now - timedelta(hours=3))
        await _make_send(db_session, thread_id="t-done", sent_at=now - timedelta(hours=1))
        await db_session.commit()

        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, days=30)
        assert a["needs_reply_count"] == 1

    async def test_classification_breakdown(self, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        now = datetime.now(timezone.utc)
        await _make_reply(db_session, thread_id="c1", received_at=now, classification="interested")
        await _make_reply(db_session, thread_id="c2", received_at=now, classification="interested")
        await _make_reply(db_session, thread_id="c3", received_at=now, classification="not_interested")
        await db_session.commit()

        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, days=30)
        assert a["classification_breakdown"]["interested"] == 2
        assert a["classification_breakdown"]["not_interested"] == 1

    async def test_empty_when_scoped_user_has_no_accounts(self, db_session):
        service = EmailHistoryService()
        a = await service.get_inbox_analytics(db_session, user_gmail_emails=set(), days=30)
        assert a["sent_count"] == 0
        assert a["received_count"] == 0

    async def test_analytics_endpoint(self, client, db_session, make_user, auth_headers):
        user = await make_user("an-ep@company.com")
        await _make_gmail_account(db_session, "an-ep@company.com", user_id=user.id)
        now = datetime.now(timezone.utc)
        await _make_reply(db_session, thread_id="t-an", received_at=now - timedelta(hours=2),
                          to_email="an-ep@company.com")
        await db_session.commit()

        resp = await client.get("/api/v1/emails/analytics?days=14", headers=auth_headers(user))
        assert resp.status_code == 200
        body = resp.json()
        assert body["window_days"] == 14
        assert body["received_count"] == 1
