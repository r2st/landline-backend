"""SMTP mailboxes get reply/bounce visibility over IMAP (P1 audit fix).

Before this, reply_tracker and bounce_ingestion only spoke to the Gmail API, so
a contact on an SMTP-sent sequence kept getting follow-ups after they replied or
bounced. These cover the IMAP host derivation and that both sweeps now pick up
SMTP accounts.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.models.smtp_account import SmtpAccount
from app.services import crypto
from app.services.bounce_ingestion import BounceIngestionService, _looks_like_bounce
from app.services.reply_tracker import ReplyTracker
from app.services.smtp_reader import SmtpMailboxReader, derive_imap_host


@pytest.fixture
def crypto_key():
    from cryptography.fernet import Fernet

    from app.config import get_settings

    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


async def _smtp_account(db, crypto_on, **kw) -> SmtpAccount:
    acct = SmtpAccount(
        id=uuid.uuid4(),
        email=kw.pop("email", "rep@outlook.com"),
        host=kw.pop("host", "smtp.outlook.com"),
        port=587,
        username=kw.pop("username", "rep@outlook.com"),
        password_encrypted=crypto.encrypt("secret"),
        status="connected",
        **kw,
    )
    db.add(acct)
    await db.flush()
    return acct


# --- IMAP host derivation --------------------------------------------------

def test_derive_imap_host_from_smtp_prefix():
    class A:
        imap_host = None
        host = "smtp.example.com"
    assert derive_imap_host(A()) == "imap.example.com"


def test_derive_imap_host_explicit_wins():
    class A:
        imap_host = "mail.custom.net"
        host = "smtp.example.com"
    assert derive_imap_host(A()) == "mail.custom.net"


def test_derive_imap_host_non_smtp_host_unchanged():
    class A:
        imap_host = None
        host = "mail.example.com"
    assert derive_imap_host(A()) == "mail.example.com"


# --- bounce classification filter ------------------------------------------

def test_looks_like_bounce_by_sender():
    assert _looks_like_bounce("mailer-daemon@outlook.com", "anything")
    assert _looks_like_bounce("postmaster@corp.com", "hi")


def test_looks_like_bounce_by_subject():
    assert _looks_like_bounce("system@corp.com", "Undeliverable: your message")
    assert _looks_like_bounce("x@y.com", "Delivery Status Notification (Failure)")


def test_normal_mail_is_not_a_bounce():
    assert not _looks_like_bounce("pat@prospect.co", "Re: your proposal")


# --- reply_tracker over IMAP -----------------------------------------------

@pytest.mark.asyncio
async def test_smtp_reply_pauses_enrollment(db_session, crypto_key, monkeypatch):
    """An SMTP reply flips the send to 'replied' and stops the sequence."""
    company = Company(id=uuid.uuid4(), name="Prospect Co", country="US", source="test")
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
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status="active",
    )
    db_session.add(enrollment)
    send = EmailSend(
        id=uuid.uuid4(), from_email="rep@outlook.com", to_email="pat@prospect.co",
        subject="Intro", status="sent", sent_at=datetime.now(timezone.utc),
        contact_id=contact.id,
    )
    db_session.add(send)
    acct = await _smtp_account(db_session, crypto_key, imap_enabled=True)
    await db_session.commit()

    # Stub the IMAP fetch: a reply from the contact plus our own sent copy.
    def fake_fetch(self, days=2, mailbox="INBOX"):
        return [
            {"from_email": "rep@outlook.com", "subject": "Intro", "text": "sent copy"},
            {"from_email": "pat@prospect.co", "subject": "Re: Intro", "text": "Sounds good!"},
        ]

    monkeypatch.setattr(SmtpMailboxReader, "fetch_recent", fake_fetch)

    replies = await ReplyTracker().check_for_replies(db_session)

    assert any(r["from_email"] == "pat@prospect.co" for r in replies)
    await db_session.refresh(send)
    await db_session.refresh(enrollment)
    assert send.status == "replied"
    assert enrollment.status == "replied"


@pytest.mark.asyncio
async def test_send_only_smtp_account_is_skipped(db_session, crypto_key, monkeypatch):
    """imap_enabled=False (send-only relay) must not be polled."""
    await _smtp_account(db_session, crypto_key, email="relay@sendonly.com",
                        username="relay@sendonly.com", imap_enabled=False)
    await db_session.commit()

    called = {"n": 0}

    def fake_fetch(self, days=2, mailbox="INBOX"):
        called["n"] += 1
        return []

    monkeypatch.setattr(SmtpMailboxReader, "fetch_recent", fake_fetch)
    await ReplyTracker().check_for_replies(db_session)
    assert called["n"] == 0


# --- bounce_ingestion over IMAP --------------------------------------------

HARD_DSN = """Delivery to the following recipient failed permanently:

Final-Recipient: rfc822; deadlead@gone-corp.com
Action: failed
Status: 5.1.1
Diagnostic-Code: smtp; 550 5.1.1 The email account that you tried to reach does not exist.
"""


@pytest.mark.asyncio
async def test_smtp_bounce_is_ingested_and_suppresses(db_session, crypto_key, monkeypatch):
    company = Company(id=uuid.uuid4(), name="Gone Corp", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Dead", last_name="Lead",
        email="deadlead@gone-corp.com", source="test",
    )
    db_session.add(contact)
    acct = await _smtp_account(db_session, crypto_key, imap_enabled=True)
    await db_session.commit()

    def fake_fetch(self, days=3, mailbox="INBOX"):
        return [{
            "message_id": "<dsn-1@outlook.com>",
            "from_email": "postmaster@outlook.com",
            "subject": "Undeliverable: Intro",
            "text": HARD_DSN,
        }]

    monkeypatch.setattr(SmtpMailboxReader, "fetch_recent", fake_fetch)

    result = await BounceIngestionService().ingest_all_accounts(db_session)
    assert result["bounces_ingested"] == 1
    assert result["hard"] == 1

    # The bounce handler blocks the address via ``unsubscribed`` + last_bounce_type
    # (see bounce_handler); ``suppressed`` is the separate reply-based opt-out.
    await db_session.refresh(contact)
    assert contact.unsubscribed is True
    assert contact.last_bounce_type == "hard"


@pytest.mark.asyncio
async def test_smtp_bounce_dedup_on_repeat_sweep(db_session, crypto_key, monkeypatch):
    """The same DSN on a second sweep must not double-count."""
    company = Company(id=uuid.uuid4(), name="Gone Corp", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, email="deadlead@gone-corp.com",
        first_name="Dead", last_name="Lead", source="test",
    )
    db_session.add(contact)
    await _smtp_account(db_session, crypto_key, imap_enabled=True)
    await db_session.commit()

    def fake_fetch(self, days=3, mailbox="INBOX"):
        return [{
            "message_id": "<dsn-1@outlook.com>",
            "from_email": "postmaster@outlook.com",
            "subject": "Undeliverable: Intro",
            "text": HARD_DSN,
        }]

    monkeypatch.setattr(SmtpMailboxReader, "fetch_recent", fake_fetch)

    first = await BounceIngestionService().ingest_all_accounts(db_session)
    second = await BounceIngestionService().ingest_all_accounts(db_session)
    assert first["bounces_ingested"] == 1
    assert second["bounces_ingested"] == 0
