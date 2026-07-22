"""Tests for DSN (bounce) misclassification (email pipeline audit #2).

A mailer-daemon delivery-failure/delay notice arrives as ordinary inbox mail —
often in the very thread it failed to deliver to. It must be recognised as a
bounce (bounce_records), never filed as a prospect reply (email_replies).
"""

import base64
import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.services import crypto, google_oauth
from app.services.bounce_ingestion import BOUNCE_QUERY, is_bounce_message
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


# ─── is_bounce_message unit coverage ─────────────────────────────────────────


@pytest.mark.parametrize(
    "from_email,subject",
    [
        ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)"),
        ("postmaster@outlook.com", "Undeliverable: Intro"),
        ("MAILER-DAEMON@corp.com", "Delivery has been delayed"),
        ("mail-daemon@x.com", "anything"),
        ("bounce@x.com", "Mail delivery failed: returning message to sender"),
        ("noreply@x.com", "Warning: could not send message for past 4 hours"),
    ],
)
def test_is_bounce_message_true(from_email, subject):
    assert is_bounce_message(from_email, subject) is True


@pytest.mark.parametrize(
    "from_email,subject",
    [
        ("dana@acme.com", "Re: quick question"),
        ("pat@prospect.co", "Re: closing the loop"),
        ("newsletter@substack.com", "Weekly digest"),  # noise, but not a bounce
    ],
)
def test_is_bounce_message_false(from_email, subject):
    assert is_bounce_message(from_email, subject) is False


def test_bounce_query_covers_delay_notices():
    q = BOUNCE_QUERY.lower()
    assert "delivery delayed" in q
    assert "mail delivery failed" in q
    assert "mailer-daemon" in q


# ─── email_history skips DSNs instead of filing them as replies ──────────────


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(msg_id, from_email, subject, thread_id, *, in_reply_to=None):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "To", "value": "sender@apprend.tech"},
        {"name": "Subject", "value": subject},
    ]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    return {
        "id": msg_id,
        "threadId": thread_id,
        "snippet": subject,
        "internalDate": "1700000000000",
        "payload": {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(f"Body for {subject}")},
        },
    }


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id

    def list(self, **kwargs):
        return _Exec({"messages": [{"id": mid} for mid in self._by_id]})

    def get(self, userId=None, id=None, format=None, **kwargs):
        return _Exec(self._by_id[id])


class _Users:
    def __init__(self, by_id):
        self._messages = _Messages(by_id)

    def messages(self):
        return self._messages


class _FakeService:
    def __init__(self, by_id):
        self._users = _Users(by_id)

    def users(self):
        return self._users


async def _account(db: AsyncSession) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(), email="sender@apprend.tech", google_sub="sub-sender",
        display_name="Sender",
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES), is_active=True, status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


@pytest.mark.asyncio
async def test_dsn_in_owned_thread_is_not_filed_as_reply(
    db_session: AsyncSession, monkeypatch
):
    account = await _account(db_session)

    # A send that established a thread we own. Its DSN will arrive in that thread.
    db_session.add(EmailSend(
        id=uuid.uuid4(), from_email="sender@apprend.tech", to_email="dead@gone.com",
        subject="Intro", status="sent", gmail_thread_id="thread-dead",
        gmail_message_id="apisend-1", provider_message_id="<rfc-send-1@apprend.tech>",
        sent_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    messages = {
        # Bounce landing in the thread we started — WITHOUT the fix, the thread
        # match in _is_outreach_reply files it as a reply. It must be skipped.
        "m-dsn-thread": _msg(
            "m-dsn-thread", "Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
            "Delivery Status Notification (Failure)", "thread-dead",
        ),
        # Bounce matched by In-Reply-To header — also must be skipped.
        "m-dsn-hdr": _msg(
            "m-dsn-hdr", "postmaster@googlemail.com", "Undelivered Mail Returned to Sender",
            "thread-new", in_reply_to="<rfc-send-1@apprend.tech>",
        ),
    }
    monkeypatch.setattr(
        EmailHistoryService, "_build_gmail_service",
        lambda self, acct: _FakeService(messages),
    )

    service = EmailHistoryService()
    result = await service.sync_replies_for_account(db_session, account)

    assert result["synced"] == 0, result
    assert result["skipped"] == 2, result
    count = (await db_session.execute(select(func.count()).select_from(EmailReply))).scalar()
    assert count == 0
