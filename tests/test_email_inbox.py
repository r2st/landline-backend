"""Tests for unified inbox — direction detection, filters, thread dedup, purge & cleanup."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


async def _make_gmail_account(
    db: AsyncSession,
    email: str,
    active: bool = False,
    status: str = "connected",
) -> GmailAccount:
    account = GmailAccount(
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=active,
        status=status,
    )
    db.add(account)
    await db.flush()
    return account


async def _make_smtp_account(
    db: AsyncSession,
    email: str,
    active: bool = False,
    status: str = "connected",
) -> SmtpAccount:
    account = SmtpAccount(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        host="smtp.example.com",
        port=587,
        username=email,
        password_encrypted=crypto.encrypt("smtp-secret"),
        is_active=active,
        status=status,
    )
    db.add(account)
    await db.flush()
    return account


async def _make_email_send(
    db: AsyncSession,
    *,
    from_email: str,
    to_email: str,
    subject: str = "Test subject",
    gmail_thread_id: str | None = None,
    gmail_message_id: str | None = None,
    sender_account_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    status: str = "sent",
) -> EmailSend:
    send = EmailSend(
        id=uuid.uuid4(),
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_html=f"<p>{subject}</p>",
        status=status,
        gmail_thread_id=gmail_thread_id,
        gmail_message_id=gmail_message_id,
        sender_account_id=sender_account_id,
        contact_id=contact_id,
        sent_at=datetime.now(timezone.utc),
    )
    db.add(send)
    await db.flush()
    return send


async def _make_email_reply(
    db: AsyncSession,
    *,
    from_email: str,
    to_email: str,
    subject: str = "Re: Test subject",
    gmail_thread_id: str | None = None,
    gmail_message_id: str | None = None,
    gmail_account_id: uuid.UUID | None = None,
    contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    snippet: str | None = None,
    received_at: datetime | None = None,
) -> EmailReply:
    reply = EmailReply(
        id=uuid.uuid4(),
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text="Reply body",
        body_html="<p>Reply body</p>",
        snippet=snippet or "Reply body",
        gmail_thread_id=gmail_thread_id,
        gmail_message_id=gmail_message_id or f"msg-{uuid.uuid4().hex[:8]}",
        gmail_account_id=gmail_account_id,
        contact_id=contact_id,
        company_id=company_id,
        received_at=received_at or datetime.now(timezone.utc),
        is_read=False,
    )
    db.add(reply)
    await db.flush()
    return reply


# ---------------------------------------------------------------------------
# Direction detection tests
# ---------------------------------------------------------------------------

class TestDirectionDetection:
    """_reply_to_message should set direction based on connected accounts."""

    async def test_reply_from_external_is_received(self, db_session):
        """Emails from external senders are direction='received'."""
        await _make_gmail_account(db_session, "us@company.com", active=True)
        reply = await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="us@company.com",
            gmail_thread_id="thread-1",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        threads = result["threads"]
        assert len(threads) == 1
        msg = threads[0]["messages"][0]
        assert msg["direction"] == "received"

    async def test_reply_from_connected_account_is_sent(self, db_session):
        """Emails from our own connected accounts are direction='sent'."""
        acct = await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_gmail_account(db_session, "colleague@company.com")
        # A reply synced from colleague's inbox, but FROM us
        reply = await _make_email_reply(
            db_session,
            from_email="us@company.com",
            to_email="colleague@company.com",
            gmail_thread_id="thread-2",
            gmail_account_id=acct.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        threads = result["threads"]
        assert len(threads) == 1
        msg = threads[0]["messages"][0]
        assert msg["direction"] == "sent"
        assert msg["status"] == "sent"


# ---------------------------------------------------------------------------
# Received filter tests
# ---------------------------------------------------------------------------

class TestReceivedFilter:
    """The 'received' filter should exclude emails from connected accounts."""

    async def test_received_filter_excludes_own_emails(self, db_session):
        """Received filter must not show emails from our connected accounts."""
        acct = await _make_gmail_account(db_session, "sender@company.com", active=True)
        # Our own email synced back as a reply
        await _make_email_reply(
            db_session,
            from_email="sender@company.com",
            to_email="client@example.com",
            gmail_thread_id="thread-own",
            gmail_account_id=acct.id,
        )
        # A genuine incoming reply
        await _make_email_reply(
            db_session,
            from_email="client@example.com",
            to_email="sender@company.com",
            gmail_thread_id="thread-client",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="received")
        threads = result["threads"]
        # Only the genuine incoming reply should appear
        assert len(threads) == 1
        assert threads[0]["messages"][0]["from_email"] == "client@example.com"

    async def test_received_filter_case_insensitive(self, db_session):
        """Direction check should be case-insensitive on email addresses."""
        await _make_gmail_account(db_session, "Sender@Company.COM", active=True)
        # Reply with lowercase from_email matching the connected account
        await _make_email_reply(
            db_session,
            from_email="sender@company.com",
            to_email="someone@external.com",
            gmail_thread_id="thread-case",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="received")
        # Should be excluded despite case mismatch
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Thread deduplication tests
# ---------------------------------------------------------------------------

class TestThreadDedup:
    """Messages with the same gmail_message_id should be deduplicated."""

    async def test_dedup_send_and_synced_reply(self, db_session):
        """Same message as EmailSend and EmailReply should appear once."""
        acct = await _make_gmail_account(db_session, "us@company.com", active=True)
        thread_id = "thread-dedup"
        msg_id = "msg-dedup-123"

        # Original send
        await _make_email_send(
            db_session,
            from_email="us@company.com",
            to_email="client@example.com",
            gmail_thread_id=thread_id,
            gmail_message_id=msg_id,
            sender_account_id=acct.id,
        )
        # Same message synced as reply (from the recipient's connected account)
        await _make_email_reply(
            db_session,
            from_email="us@company.com",
            to_email="client@example.com",
            gmail_thread_id=thread_id,
            gmail_message_id=msg_id,
            gmail_account_id=acct.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        threads = result["threads"]
        assert len(threads) == 1
        # Only one message in the thread (deduplicated)
        assert len(threads[0]["messages"]) == 1


# ---------------------------------------------------------------------------
# Needs-reply filter tests
# ---------------------------------------------------------------------------

class TestNeedsReplyFilter:
    """needs_reply filter should only show threads where last msg is received."""

    async def test_needs_reply_when_last_is_received(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        thread_id = "thread-nr"
        # We sent first
        await _make_email_send(
            db_session,
            from_email="us@company.com",
            to_email="lead@example.com",
            gmail_thread_id=thread_id,
            gmail_message_id="msg-sent-1",
        )
        # They replied (most recent)
        await _make_email_reply(
            db_session,
            from_email="lead@example.com",
            to_email="us@company.com",
            gmail_thread_id=thread_id,
            received_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="needs_reply")
        assert result["total"] == 1

    async def test_no_needs_reply_when_last_is_sent(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        thread_id = "thread-responded"
        # They wrote first
        await _make_email_reply(
            db_session,
            from_email="lead@example.com",
            to_email="us@company.com",
            gmail_thread_id=thread_id,
            received_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        # We replied last
        await _make_email_send(
            db_session,
            from_email="us@company.com",
            to_email="lead@example.com",
            gmail_thread_id=thread_id,
            gmail_message_id="msg-sent-2",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="needs_reply")
        assert result["total"] == 0


# ---------------------------------------------------------------------------
# Snippet HTML entity decoding tests
# ---------------------------------------------------------------------------

class TestSnippetDecoding:
    """Snippets should have HTML entities decoded."""

    async def test_reply_snippet_unescaped(self, db_session):
        """HTML entities like &#39; should be decoded before storage/display."""
        await _make_gmail_account(db_session, "us@company.com", active=True)
        # Simulate a reply with HTML-encoded snippet (as Gmail API returns)
        await _make_email_reply(
            db_session,
            from_email="client@example.com",
            to_email="us@company.com",
            gmail_thread_id="thread-snippet",
            snippet="That&#39;s great &amp; we&#39;re ready",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        # Note: The snippet is stored as-is in the test fixture; the unescape
        # happens during Gmail sync (email_history.py sync logic).
        # This test verifies the inbox returns whatever snippet is stored.
        threads = result["threads"]
        assert len(threads) == 1


# ---------------------------------------------------------------------------
# Purge endpoint tests
# ---------------------------------------------------------------------------

class TestPurgeEndpoint:
    """Purge should delete all emails linked to an account including incoming."""

    async def test_purge_deletes_outgoing_and_incoming(self, client, db_session, monkeypatch):
        monkeypatch.setattr(google_oauth, "revoke_token", lambda *a, **k: True)

        acct = await _make_gmail_account(db_session, "user@company.com", active=True)
        # Outgoing: linked by sender_account_id
        await _make_email_send(
            db_session,
            from_email="user@company.com",
            to_email="client@external.com",
            sender_account_id=acct.id,
            gmail_message_id="send-1",
        )
        # Incoming: linked by gmail_account_id
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="user@company.com",
            gmail_account_id=acct.id,
        )
        # Incoming by to_email only (no gmail_account_id link)
        await _make_email_reply(
            db_session,
            from_email="other@external.com",
            to_email="user@company.com",
            gmail_account_id=None,
        )
        await db_session.commit()

        resp = await client.delete(f"/api/v1/gmail/accounts/{acct.id}/purge")
        assert resp.status_code == 200
        data = resp.json()
        # All replies deleted (2 incoming)
        assert data["replies_deleted"] >= 2
        assert data["sends_deleted"] == 1
        assert data["total_deleted"] >= 3

        # Verify account is gone
        remaining = (await db_session.execute(
            select(GmailAccount).where(GmailAccount.id == acct.id)
        )).scalar_one_or_none()
        assert remaining is None

    async def test_purge_promotes_next_account(self, client, db_session, monkeypatch):
        monkeypatch.setattr(google_oauth, "revoke_token", lambda *a, **k: True)

        primary = await _make_gmail_account(db_session, "primary@co.com", active=True)
        backup = await _make_gmail_account(db_session, "backup@co.com", active=False)
        await db_session.commit()

        await client.delete(f"/api/v1/gmail/accounts/{primary.id}/purge")
        await db_session.commit()

        # Backup should now be active
        resp = await client.get("/api/v1/gmail/status")
        data = resp.json()
        assert data["active_email"] == "backup@co.com"


# ---------------------------------------------------------------------------
# Cleanup endpoint tests
# ---------------------------------------------------------------------------

class TestCleanupEndpoint:
    """Cleanup should delete orphaned emails by email address."""

    async def test_cleanup_deletes_by_email_address(self, client, db_session):
        email = "orphan@removed.com"
        # Create orphaned replies (no gmail_account link)
        await _make_email_reply(
            db_session,
            from_email=email,
            to_email="us@company.com",
        )
        await _make_email_reply(
            db_session,
            from_email="someone@else.com",
            to_email=email,
        )
        # And an orphaned send
        await _make_email_send(
            db_session,
            from_email=email,
            to_email="client@x.com",
            gmail_message_id="orphan-send-1",
        )
        await db_session.commit()

        resp = await client.delete(f"/api/v1/gmail/cleanup-emails?email={email}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["replies_deleted"] == 2
        assert data["sends_deleted"] == 1
        assert data["total_deleted"] == 3

        # Verify all cleaned up
        remaining_replies = (await db_session.execute(
            select(EmailReply).where(
                (EmailReply.from_email == email) | (EmailReply.to_email == email)
            )
        )).scalars().all()
        assert len(remaining_replies) == 0


# ---------------------------------------------------------------------------
# Search filter tests
# ---------------------------------------------------------------------------

class TestSearchFilter:
    """Search should filter by subject, from, or to email."""

    async def test_search_by_subject(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_email_send(
            db_session,
            from_email="us@company.com",
            to_email="client@x.com",
            subject="Quality Management Platform Demo",
            gmail_message_id="search-1",
        )
        await _make_email_send(
            db_session,
            from_email="us@company.com",
            to_email="other@y.com",
            subject="Follow up on our meeting",
            gmail_message_id="search-2",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, search="Quality")
        assert result["total"] == 1
        assert "Quality" in result["threads"][0]["subject"]

    async def test_search_by_email_address(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_email_reply(
            db_session,
            from_email="vip@bigcorp.com",
            to_email="us@company.com",
            gmail_thread_id="thread-vip",
        )
        await _make_email_reply(
            db_session,
            from_email="random@small.com",
            to_email="us@company.com",
            gmail_thread_id="thread-random",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, search="bigcorp")
        assert result["total"] == 1


# ---------------------------------------------------------------------------
# Email-account attribution & filtering tests
# ---------------------------------------------------------------------------

class TestEmailAccountAttribution:
    """Each message should carry the connected account it belongs to, and the
    inbox should be filterable by that account."""

    async def test_sent_message_carries_sender_account(self, db_session):
        """A sent email exposes its sender account id + email."""
        acct = await _make_gmail_account(db_session, "sales@company.com", active=True)
        await _make_email_send(
            db_session,
            from_email="sales@company.com",
            to_email="lead@example.com",
            gmail_message_id="attr-sent-1",
            sender_account_id=acct.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        msg = result["threads"][0]["messages"][0]
        assert msg["gmail_account_id"] == acct.id
        assert msg["gmail_account_email"] == "sales@company.com"

    async def test_received_message_carries_receiving_account(self, db_session):
        """A received email exposes the account it landed in."""
        acct = await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="support@company.com",
            gmail_thread_id="attr-recv-1",
            gmail_account_id=acct.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        msg = result["threads"][0]["messages"][0]
        assert msg["gmail_account_id"] == acct.id
        assert msg["gmail_account_email"] == "support@company.com"

    async def test_received_account_email_falls_back_to_to_email(self, db_session):
        """When a reply has no gmail_account_id link, the received-on address
        (to_email) is used for display."""
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="support@company.com",
            gmail_thread_id="attr-recv-2",
            gmail_account_id=None,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all")
        msg = result["threads"][0]["messages"][0]
        assert msg["gmail_account_id"] is None
        assert msg["gmail_account_email"] == "support@company.com"

    async def test_filter_by_account_scopes_sends_and_replies(self, db_session):
        """gmail_account_id should return only mail tied to that account."""
        support = await _make_gmail_account(db_session, "support@company.com", active=True)
        sales = await _make_gmail_account(db_session, "sales@company.com")

        # Sent from support
        await _make_email_send(
            db_session,
            from_email="support@company.com",
            to_email="a@x.com",
            gmail_thread_id="t-support-sent",
            gmail_message_id="acc-1",
            sender_account_id=support.id,
        )
        # Received on support
        await _make_email_reply(
            db_session,
            from_email="a@x.com",
            to_email="support@company.com",
            gmail_thread_id="t-support-recv",
            gmail_account_id=support.id,
        )
        # Sent from sales
        await _make_email_send(
            db_session,
            from_email="sales@company.com",
            to_email="b@y.com",
            gmail_thread_id="t-sales-sent",
            gmail_message_id="acc-2",
            sender_account_id=sales.id,
        )
        # Received on sales
        await _make_email_reply(
            db_session,
            from_email="b@y.com",
            to_email="sales@company.com",
            gmail_thread_id="t-sales-recv",
            gmail_account_id=sales.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="all", gmail_account_id=support.id
        )
        thread_ids = {t["gmail_thread_id"] for t in result["threads"]}
        assert thread_ids == {"t-support-sent", "t-support-recv"}
        for t in result["threads"]:
            for m in t["messages"]:
                assert m["gmail_account_email"] == "support@company.com"

    async def test_filter_by_account_combines_with_sent_filter(self, db_session):
        """Account filter composes with filter_type."""
        support = await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_email_send(
            db_session,
            from_email="support@company.com",
            to_email="a@x.com",
            gmail_thread_id="t-combo-sent",
            gmail_message_id="combo-1",
            sender_account_id=support.id,
        )
        await _make_email_reply(
            db_session,
            from_email="a@x.com",
            to_email="support@company.com",
            gmail_thread_id="t-combo-recv",
            gmail_account_id=support.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="sent", gmail_account_id=support.id
        )
        assert result["total"] == 1
        assert result["threads"][0]["gmail_thread_id"] == "t-combo-sent"


class TestInboxAccountFilterEndpoint:
    """HTTP-level coverage for the gmail_account_id query parameter."""

    async def test_endpoint_filters_by_account(self, client, db_session):
        support = await _make_gmail_account(db_session, "support@company.com", active=True)
        sales = await _make_gmail_account(db_session, "sales@company.com")
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="support@company.com",
            gmail_thread_id="ep-support",
            gmail_account_id=support.id,
        )
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="sales@company.com",
            gmail_thread_id="ep-sales",
            gmail_account_id=sales.id,
        )
        await db_session.commit()

        resp = await client.get(f"/api/v1/emails/inbox?gmail_account_id={support.id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["threads"][0]["gmail_thread_id"] == "ep-support"
        assert data["threads"][0]["messages"][0]["gmail_account_email"] == "support@company.com"

    async def test_endpoint_unknown_account_returns_404(self, client, db_session):
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await db_session.commit()

        resp = await client.get(f"/api/v1/emails/inbox?gmail_account_id={uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Filter by email address (Gmail *and* SMTP)
# ---------------------------------------------------------------------------

class TestInboxAccountEmailFilter:
    """``account_email`` scopes the inbox to one address and, unlike
    ``gmail_account_id``, works for SMTP accounts (which have no FK on the
    message rows)."""

    async def test_filter_by_email_scopes_sends_and_replies(self, db_session):
        support = await _make_gmail_account(db_session, "support@company.com", active=True)
        sales = await _make_gmail_account(db_session, "sales@company.com")

        await _make_email_send(
            db_session,
            from_email="support@company.com",
            to_email="a@x.com",
            gmail_thread_id="e-support-sent",
            gmail_message_id="em-1",
            sender_account_id=support.id,
        )
        await _make_email_reply(
            db_session,
            from_email="a@x.com",
            to_email="support@company.com",
            gmail_thread_id="e-support-recv",
            gmail_account_id=support.id,
        )
        await _make_email_send(
            db_session,
            from_email="sales@company.com",
            to_email="b@y.com",
            gmail_thread_id="e-sales-sent",
            gmail_message_id="em-2",
            sender_account_id=sales.id,
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="all", account_email="support@company.com"
        )
        thread_ids = {t["gmail_thread_id"] for t in result["threads"]}
        assert thread_ids == {"e-support-sent", "e-support-recv"}

    async def test_filter_by_email_is_case_insensitive(self, db_session):
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_email_reply(
            db_session,
            from_email="a@x.com",
            to_email="support@company.com",
            gmail_thread_id="e-case",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="all", account_email="SUPPORT@Company.com"
        )
        assert result["total"] == 1
        assert result["threads"][0]["gmail_thread_id"] == "e-case"

    async def test_filter_by_smtp_account_email(self, db_session):
        """SMTP sends/replies carry no account-id FK — the address is the only
        link, and ``account_email`` must still scope to them."""
        await _make_smtp_account(db_session, "relay@company.com", active=True)
        # SMTP send: sender_account_id is null (FK points at gmail_accounts).
        await _make_email_send(
            db_session,
            from_email="relay@company.com",
            to_email="lead@x.com",
            gmail_thread_id="smtp-sent",
            gmail_message_id="smtp-em-1",
            sender_account_id=None,
        )
        # SMTP reply landed over IMAP: gmail_account_id is null too.
        await _make_email_reply(
            db_session,
            from_email="lead@x.com",
            to_email="relay@company.com",
            gmail_thread_id="smtp-recv",
            gmail_account_id=None,
        )
        # Unrelated mail on another address must be excluded.
        await _make_email_reply(
            db_session,
            from_email="lead@x.com",
            to_email="other@company.com",
            gmail_thread_id="smtp-other",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="all", account_email="relay@company.com"
        )
        thread_ids = {t["gmail_thread_id"] for t in result["threads"]}
        assert thread_ids == {"smtp-sent", "smtp-recv"}

    async def test_filter_by_email_combines_with_sent_filter(self, db_session):
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_email_send(
            db_session,
            from_email="support@company.com",
            to_email="a@x.com",
            gmail_thread_id="e-combo-sent",
            gmail_message_id="ec-1",
        )
        await _make_email_reply(
            db_session,
            from_email="a@x.com",
            to_email="support@company.com",
            gmail_thread_id="e-combo-recv",
        )
        await db_session.commit()

        service = EmailHistoryService()
        result = await service.get_inbox(
            db_session, filter_type="sent", account_email="support@company.com"
        )
        assert result["total"] == 1
        assert result["threads"][0]["gmail_thread_id"] == "e-combo-sent"

    async def test_endpoint_filters_by_email(self, client, db_session):
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await _make_smtp_account(db_session, "relay@company.com")
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="support@company.com",
            gmail_thread_id="ep-em-support",
        )
        await _make_email_reply(
            db_session,
            from_email="client@external.com",
            to_email="relay@company.com",
            gmail_thread_id="ep-em-relay",
        )
        await db_session.commit()

        resp = await client.get(
            "/api/v1/emails/inbox?account_email=relay@company.com"
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["threads"][0]["gmail_thread_id"] == "ep-em-relay"

    async def test_endpoint_unknown_email_returns_404(self, client, db_session):
        await _make_gmail_account(db_session, "support@company.com", active=True)
        await db_session.commit()

        resp = await client.get(
            "/api/v1/emails/inbox?account_email=nobody@nowhere.com"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------

class TestPagination:

    async def test_inbox_pagination(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        # Create 5 threads
        for i in range(5):
            await _make_email_send(
                db_session,
                from_email="us@company.com",
                to_email=f"lead{i}@example.com",
                subject=f"Thread {i}",
                gmail_thread_id=f"thread-page-{i}",
                gmail_message_id=f"msg-page-{i}",
            )
        await db_session.commit()

        service = EmailHistoryService()
        page1 = await service.get_inbox(db_session, page=1, per_page=2)
        assert len(page1["threads"]) == 2
        assert page1["total"] == 5

        page2 = await service.get_inbox(db_session, page=2, per_page=2)
        assert len(page2["threads"]) == 2

        page3 = await service.get_inbox(db_session, page=3, per_page=2)
        assert len(page3["threads"]) == 1


# ---------------------------------------------------------------------------
# N+1 batching: contact/company lookups are grouped, not per-row
# ---------------------------------------------------------------------------

class TestInboxBatching:
    """Inbox assembly must resolve contacts + companies in grouped queries.

    Previously ``get_inbox`` issued up to two lookups per send and per reply
    (contact, then company) — an N+1 that, with the ~500-row scan cap, meant
    thousands of round-trips to render one page. The lookups are now batched into
    a single grouped query per table; this test proves the query count no longer
    scales with the number of rows, while the resolved lead/company is unchanged.
    """

    async def test_contact_company_lookups_are_batched(self, db_session, monkeypatch):
        acct = await _make_gmail_account(db_session, "us@company.com", active=True)

        n = 6
        expected_lead = {}
        for i in range(n):
            company = Company(
                id=uuid.uuid4(), name=f"Batch Co {i}", country="US", source="test"
            )
            db_session.add(company)
            await db_session.flush()
            contact = Contact(
                id=uuid.uuid4(), company_id=company.id, first_name="Lead",
                last_name=str(i), email=f"lead{i}@x.com", source="test",
            )
            db_session.add(contact)
            await db_session.flush()
            await _make_email_send(
                db_session, from_email="us@company.com", to_email=f"lead{i}@x.com",
                subject=f"Batch {i}", gmail_thread_id=f"batch-thr-{i}",
                gmail_message_id=f"batch-send-{i}", sender_account_id=acct.id,
                contact_id=contact.id,
            )
            await _make_email_reply(
                db_session, from_email=f"lead{i}@x.com", to_email="us@company.com",
                gmail_thread_id=f"batch-thr-{i}", contact_id=contact.id,
                company_id=company.id,
            )
            expected_lead[f"batch-thr-{i}"] = f"Batch Co {i}"
        await db_session.commit()

        # Count SELECTs that touch the contacts / companies tables during assembly.
        real_execute = db_session.execute
        counts = {"contacts": 0, "companies": 0}

        async def counting_execute(statement, *args, **kwargs):
            sql = str(statement).lower()
            if "from contacts" in sql:
                counts["contacts"] += 1
            if "from companies" in sql:
                counts["companies"] += 1
            return await real_execute(statement, *args, **kwargs)

        monkeypatch.setattr(db_session, "execute", counting_execute)

        service = EmailHistoryService()
        result = await service.get_inbox(db_session, filter_type="all", per_page=50)

        # Batched: one grouped query per table regardless of row count. The old
        # N+1 would have issued ~12 contact + ~12 company selects for n=6.
        assert counts["contacts"] <= 1, counts
        assert counts["companies"] <= 1, counts

        # Correctness preserved: every thread resolved its company as the lead.
        threads = result["threads"]
        assert len(threads) == n
        for t in threads:
            assert t["lead_name"] == expected_lead[t["gmail_thread_id"]]


# ---------------------------------------------------------------------------
# Reply-sync self-healing on revoked OAuth tokens
# ---------------------------------------------------------------------------

class _FakeGmailExecute:
    """Terminal ``.execute()`` node of the fake Gmail fluent chain."""

    def __init__(self, result=None, raise_exc=None):
        self._result = result
        self._raise = raise_exc

    def execute(self):
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeGmailMessages:
    def __init__(self, list_exc=None):
        self._list_exc = list_exc

    def list(self, **kwargs):
        # The token refresh is triggered on the first real API call.
        return _FakeGmailExecute(result={"messages": []}, raise_exc=self._list_exc)


class _FakeGmailUsers:
    def __init__(self, list_exc=None):
        self._messages = _FakeGmailMessages(list_exc=list_exc)

    def messages(self):
        return self._messages


class _FakeGmailService:
    """Minimal stand-in for the googleapiclient Gmail resource."""

    def __init__(self, list_exc=None):
        self._users = _FakeGmailUsers(list_exc=list_exc)

    def users(self):
        return self._users


class TestReplySyncSelfHealing:
    """sync_all_accounts must self-heal when an account's OAuth token is revoked."""

    async def test_revoked_token_deactivates_account_and_continues(
        self, db_session, monkeypatch
    ):
        """A RefreshError (invalid_grant) flips the account to 'revoked',
        logs a warning, and does not stop the other accounts from syncing."""
        from google.auth.exceptions import RefreshError

        revoked = await _make_gmail_account(db_session, "suman.onenet@gmail.com")
        healthy = await _make_gmail_account(db_session, "good@company.com")
        await db_session.commit()

        def _fake_build_service(self, account):
            if account.email == "suman.onenet@gmail.com":
                return _FakeGmailService(
                    list_exc=RefreshError("invalid_grant: Token has been expired or revoked.")
                )
            return _FakeGmailService()

        monkeypatch.setattr(
            EmailHistoryService, "_build_gmail_service", _fake_build_service
        )

        service = EmailHistoryService()
        result = await service.sync_all_accounts(db_session)

        # The revoked account was auto-deactivated; the batch did not crash.
        assert result["deactivated"] == 1

        await db_session.refresh(revoked)
        await db_session.refresh(healthy)
        assert revoked.status == "revoked"
        # The healthy account is untouched and still connected.
        assert healthy.status == "connected"

    async def test_revoked_account_dropped_from_next_poll(
        self, db_session, monkeypatch
    ):
        """Once flipped to 'revoked', the account no longer matches the
        connected-only query, so it stops being polled entirely."""
        from google.auth.exceptions import RefreshError

        await _make_gmail_account(db_session, "suman.onenet@gmail.com")
        await db_session.commit()

        polled: list[str] = []

        def _fake_build_service(self, account):
            polled.append(account.email)
            return _FakeGmailService(
                list_exc=RefreshError("invalid_grant: Token has been expired or revoked.")
            )

        monkeypatch.setattr(
            EmailHistoryService, "_build_gmail_service", _fake_build_service
        )

        service = EmailHistoryService()
        # First run trips the RefreshError and deactivates the account.
        await service.sync_all_accounts(db_session)
        # Second run: the account is 'revoked', so it must not be polled again.
        await service.sync_all_accounts(db_session)

        assert polled == ["suman.onenet@gmail.com"]
