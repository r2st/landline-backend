"""Tests for outreach-reply filtering in the Gmail sync.

The inbox sync must only persist EmailReply rows for messages that are genuine
replies to our outreach (matched by thread id, In-Reply-To/References header, or
a known outreach contact). Newsletters and other inbox noise must be skipped.
"""

import base64
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
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


# --------------------------------------------------------------------------- #
# Fake Gmail service — returns a fixed set of inbox messages.
# --------------------------------------------------------------------------- #

def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(msg_id, from_email, subject, thread_id, *, in_reply_to=None, references=None):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "To", "value": "sender@apprend.tech"},
        {"name": "Subject", "value": subject},
    ]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    if references:
        headers.append({"name": "References", "value": references})
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
        id=uuid.uuid4(),
        email="sender@apprend.tech",
        google_sub="sub-sender",
        display_name="Sender",
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=True,
        status="connected",
    )
    db.add(acct)
    await db.flush()
    return acct


@pytest.mark.asyncio
async def test_sync_filters_out_inbox_noise(db_session: AsyncSession, monkeypatch):
    account = await _account(db_session)

    # A company + contact we've enrolled in a sequence (known outreach contact).
    company = Company(
        id=uuid.uuid4(), name="Prospect Co", domain="prospect.co",
        country="US", source="test",
    )
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Pat", last_name="Prospect",
        email="pat@prospect.co", source="test",
    )
    db_session.add(contact)
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", status="active")
    db_session.add(seq)
    await db_session.flush()
    db_session.add(OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    ))

    # A manual send establishing a thread we own.
    db_session.add(EmailSend(
        id=uuid.uuid4(), from_email="sender@apprend.tech", to_email="lead@acme.com",
        subject="Intro", status="sent", gmail_thread_id="thread-acme",
        gmail_message_id="apisend-1", provider_message_id="<rfc-send-1@apprend.tech>",
        sent_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    messages = {
        # (1) reply from a known outreach contact, new thread → KEEP.
        "m-contact": _msg("m-contact", "Pat Prospect <pat@prospect.co>", "Re: hello", "thread-x"),
        # (2) reply in a thread we started → KEEP.
        "m-thread": _msg("m-thread", "lead@acme.com", "Re: Intro", "thread-acme"),
        # (3) reply matched by In-Reply-To header → KEEP.
        "m-header": _msg("m-header", "someone@acme.com", "Re: Intro", "thread-new",
                         in_reply_to="<rfc-send-1@apprend.tech>"),
        # (4) newsletter — unknown sender, unknown thread, no headers → DROP.
        "m-news": _msg("m-news", "newsletter@substack.com", "Weekly digest", "thread-news"),
        # (5) Kaggle notification — unknown sender → DROP.
        "m-kaggle": _msg("m-kaggle", "noreply@kaggle.com", "Your competition update", "thread-k"),
    }

    monkeypatch.setattr(
        EmailHistoryService, "_build_gmail_service",
        lambda self, acct: _FakeService(messages),
    )

    service = EmailHistoryService()
    result = await service.sync_replies_for_account(db_session, account)

    assert result["synced"] == 3, result
    assert result["skipped"] == 2, result

    stored = (await db_session.execute(select(EmailReply.from_email))).scalars().all()
    stored = set(stored)
    assert "pat@prospect.co" in stored
    assert "lead@acme.com" in stored
    assert "someone@acme.com" in stored
    assert "newsletter@substack.com" not in stored
    assert "noreply@kaggle.com" not in stored

    # The kept contact reply is linked back to the contact/company.
    reply = (
        await db_session.execute(
            select(EmailReply).where(EmailReply.from_email == "pat@prospect.co")
        )
    ).scalar_one()
    assert reply.contact_id == contact.id
    assert reply.company_id == company.id


@pytest.mark.asyncio
async def test_sync_with_only_noise_stores_nothing(db_session: AsyncSession, monkeypatch):
    account = await _account(db_session)
    await db_session.commit()

    messages = {
        "n1": _msg("n1", "newsletter@substack.com", "Digest", "t1"),
        "n2": _msg("n2", "promo@store.com", "50% off", "t2"),
    }
    monkeypatch.setattr(
        EmailHistoryService, "_build_gmail_service",
        lambda self, acct: _FakeService(messages),
    )

    service = EmailHistoryService()
    result = await service.sync_replies_for_account(db_session, account)

    assert result["synced"] == 0
    assert result["skipped"] == 2
    count = (await db_session.execute(select(func.count()).select_from(EmailReply))).scalar()
    assert count == 0
