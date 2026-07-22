"""Tests for the Inbox improvements (docs/INBOX_IMPROVEMENTS.md).

Covers P0-1 mark-as-read, P0-2 archive/done, P0-3 body search + date range,
P0-4 needs_reply count, P1-2 priority filter + classification chips, and P1-3
bulk actions. Frontend has its own Vitest coverage.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.inbox_thread_state import InboxThreadState
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


async def _make_gmail_account(db, email, active=False, status="connected", user_id=None):
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
        user_id=user_id,
    )
    db.add(account)
    await db.flush()
    return account


async def _make_send(db, *, from_email, to_email, subject="Subject", thread_id=None,
                     message_id=None, body_html=None, sent_at=None, sender_account_id=None):
    send = EmailSend(
        id=uuid.uuid4(),
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_html=body_html if body_html is not None else f"<p>{subject}</p>",
        status="sent",
        gmail_thread_id=thread_id,
        gmail_message_id=message_id or f"snd-{uuid.uuid4().hex[:8]}",
        sender_account_id=sender_account_id,
        sent_at=sent_at or datetime.now(timezone.utc),
    )
    db.add(send)
    await db.flush()
    return send


async def _make_reply(db, *, from_email, to_email, subject="Re: Subject", thread_id=None,
                      message_id=None, body_text="Reply body", body_html=None,
                      received_at=None, is_read=False, priority_flag=False,
                      classification=None, gmail_account_id=None):
    reply = EmailReply(
        id=uuid.uuid4(),
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text=body_text,
        body_html=body_html if body_html is not None else f"<p>{body_text}</p>",
        snippet=(body_text or "")[:200],
        gmail_thread_id=thread_id,
        gmail_message_id=message_id or f"rep-{uuid.uuid4().hex[:8]}",
        gmail_account_id=gmail_account_id,
        received_at=received_at or datetime.now(timezone.utc),
        is_read=is_read,
        priority_flag=priority_flag,
        reply_classification=classification,
    )
    db.add(reply)
    await db.flush()
    return reply


# ---------------------------------------------------------------------------
# P0-1 · Mark as read
# ---------------------------------------------------------------------------

class TestMarkRead:
    async def test_mark_thread_read_flips_is_read(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-read", is_read=False,
        )
        await db_session.commit()

        service = EmailHistoryService()
        n = await service.set_threads_read(db_session, ["t-read"], is_read=True)
        assert n == 1

        reply = (await db_session.execute(
            select(EmailReply).where(EmailReply.gmail_thread_id == "t-read")
        )).scalar_one()
        assert reply.is_read is True

    async def test_mark_read_is_idempotent(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-idem", is_read=True,
        )
        await db_session.commit()

        service = EmailHistoryService()
        # Already read → still reports the row and stays read.
        n = await service.set_threads_read(db_session, ["t-idem"], is_read=True)
        assert n == 1
        reply = (await db_session.execute(
            select(EmailReply).where(EmailReply.gmail_thread_id == "t-idem")
        )).scalar_one()
        assert reply.is_read is True

    async def test_mark_read_scoped_cannot_touch_other_mailbox(self, db_session):
        """A user's mark-read must not flip a reply on another user's mailbox."""
        await _make_gmail_account(db_session, "mine@company.com", active=True)
        # Reply received on someone else's address.
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="other@company.com",
            thread_id="t-other", is_read=False,
        )
        await db_session.commit()

        service = EmailHistoryService()
        n = await service.set_threads_read(
            db_session, ["t-other"], is_read=True,
            user_gmail_emails={"mine@company.com"},
        )
        assert n == 0
        reply = (await db_session.execute(
            select(EmailReply).where(EmailReply.gmail_thread_id == "t-other")
        )).scalar_one()
        assert reply.is_read is False

    async def test_has_unread_flips_in_inbox_response(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-unread", is_read=False,
        )
        await db_session.commit()
        service = EmailHistoryService()

        before = await service.get_inbox(db_session, filter_type="all")
        assert before["threads"][0]["has_unread"] is True

        await service.set_threads_read(db_session, ["t-unread"], is_read=True)
        after = await service.get_inbox(db_session, filter_type="all")
        assert after["threads"][0]["has_unread"] is False

    async def test_mark_read_endpoint(self, client, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-ep-read", is_read=False,
        )
        await db_session.commit()

        resp = await client.post("/api/v1/emails/thread/t-ep-read/read")
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

        resp = await client.post("/api/v1/emails/thread/t-ep-read/unread")
        assert resp.status_code == 200
        assert resp.json()["is_read"] is False


# ---------------------------------------------------------------------------
# P0-2 · Archive / done
# ---------------------------------------------------------------------------

class TestArchive:
    async def test_archived_thread_leaves_default_view(self, db_session, make_user):
        user = await make_user("rep@company.com")
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-arch",
        )
        await db_session.commit()
        service = EmailHistoryService()

        # Visible by default.
        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-arch"}

        await service.set_threads_archived(db_session, ["t-arch"], True, user.id)

        # Gone from the default view, present under the archived tab.
        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert res["total"] == 0
        archived = await service.get_inbox(
            db_session, filter_type="archived", state_user_id=user.id
        )
        assert {t["gmail_thread_id"] for t in archived["threads"]} == {"t-arch"}
        assert archived["threads"][0]["is_archived"] is True

    async def test_unarchive_restores_thread(self, db_session, make_user):
        user = await make_user("rep2@company.com")
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-unarch",
        )
        await db_session.commit()
        service = EmailHistoryService()

        await service.set_threads_archived(db_session, ["t-unarch"], True, user.id)
        await service.set_threads_archived(db_session, ["t-unarch"], False, user.id)

        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-unarch"}
        # Only one state row exists (upsert, not duplicate insert).
        rows = (await db_session.execute(
            select(InboxThreadState).where(InboxThreadState.gmail_thread_id == "t-unarch")
        )).scalars().all()
        assert len(rows) == 1

    async def test_archive_is_per_user(self, db_session, make_user):
        """One user's archive must not hide the thread for another user."""
        alice = await make_user("alice@company.com")
        bob = await make_user("bob@company.com")
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-shared",
        )
        await db_session.commit()
        service = EmailHistoryService()

        await service.set_threads_archived(db_session, ["t-shared"], True, alice.id)

        alice_view = await service.get_inbox(
            db_session, filter_type="all", state_user_id=alice.id
        )
        bob_view = await service.get_inbox(
            db_session, filter_type="all", state_user_id=bob.id
        )
        assert alice_view["total"] == 0
        assert {t["gmail_thread_id"] for t in bob_view["threads"]} == {"t-shared"}

    async def test_archive_endpoint_requires_user(self, client, db_session):
        """The unscoped/legacy admin path can't own per-user thread state."""
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await db_session.commit()
        resp = await client.post("/api/v1/emails/thread/t-x/archive")
        assert resp.status_code == 400

    async def test_archive_endpoint_scoped_user(self, client, db_session, make_user, auth_headers):
        user = await make_user("scoped@company.com")
        await _make_gmail_account(db_session, "scoped@company.com", active=True, user_id=user.id)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="scoped@company.com",
            thread_id="t-ep-arch",
        )
        await db_session.commit()

        headers = auth_headers(user)
        resp = await client.post(
            "/api/v1/emails/thread/t-ep-arch/archive", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["is_archived"] is True

        # Now excluded from that user's default inbox.
        resp = await client.get("/api/v1/emails/inbox", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

        # And present under the archived tab.
        resp = await client.get("/api/v1/emails/inbox?filter=archived", headers=headers)
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# P0-3 · Full-text body search + date range
# ---------------------------------------------------------------------------

class TestBodySearchAndDateRange:
    async def test_search_matches_reply_body(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            subject="Re: hello", thread_id="t-body-hit",
            body_text="Yes we are interested in a pilot next quarter",
        )
        await _make_reply(
            db_session, from_email="other@x.com", to_email="us@company.com",
            subject="Re: nope", thread_id="t-body-miss",
            body_text="Not relevant to us at all",
        )
        await db_session.commit()

        service = EmailHistoryService()
        res = await service.get_inbox(db_session, search="pilot")
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-body-hit"}

    async def test_search_matches_sent_body(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_send(
            db_session, from_email="us@company.com", to_email="lead@x.com",
            subject="Intro", thread_id="t-sent-body",
            body_html="<p>Here is our quality management platform</p>",
        )
        await db_session.commit()

        service = EmailHistoryService()
        res = await service.get_inbox(db_session, search="quality management")
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-sent-body"}

    async def test_date_range_filters(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        old = datetime.now(timezone.utc) - timedelta(days=10)
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        await _make_reply(
            db_session, from_email="a@x.com", to_email="us@company.com",
            thread_id="t-old", received_at=old,
        )
        await _make_reply(
            db_session, from_email="b@x.com", to_email="us@company.com",
            thread_id="t-recent", received_at=recent,
        )
        await db_session.commit()

        service = EmailHistoryService()
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        res = await service.get_inbox(db_session, date_from=cutoff)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-recent"}

        res = await service.get_inbox(db_session, date_to=cutoff)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-old"}


# ---------------------------------------------------------------------------
# P0-4 · needs_reply pagination / count
# ---------------------------------------------------------------------------

class TestNeedsReplyCount:
    async def test_total_equals_needs_reply_thread_count(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        # Two threads that need a reply (last message received).
        for i in range(2):
            await _make_reply(
                db_session, from_email=f"lead{i}@x.com", to_email="us@company.com",
                thread_id=f"t-nr-{i}",
            )
        # Two threads that DON'T (last message sent).
        for i in range(2):
            tid = f"t-done-{i}"
            await _make_reply(
                db_session, from_email=f"lead{i}@y.com", to_email="us@company.com",
                thread_id=tid, received_at=datetime.now(timezone.utc) - timedelta(hours=2),
            )
            await _make_send(
                db_session, from_email="us@company.com", to_email=f"lead{i}@y.com",
                thread_id=tid, sent_at=datetime.now(timezone.utc),
            )
        await db_session.commit()

        service = EmailHistoryService()
        res = await service.get_inbox(db_session, filter_type="needs_reply", per_page=1)
        # total must reflect ALL needs-reply threads, not just the page size.
        assert res["total"] == 2
        assert len(res["threads"]) == 1


# ---------------------------------------------------------------------------
# P1-2 · Priority filter + classification surfaced
# ---------------------------------------------------------------------------

class TestPriorityAndClassification:
    async def test_priority_filter_returns_only_flagged(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="hot@x.com", to_email="us@company.com",
            thread_id="t-hot", priority_flag=True, classification="interested",
        )
        await _make_reply(
            db_session, from_email="cold@x.com", to_email="us@company.com",
            thread_id="t-cold", priority_flag=False, classification="not_interested",
        )
        await db_session.commit()

        service = EmailHistoryService()
        res = await service.get_inbox(db_session, filter_type="priority")
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-hot"}
        assert res["threads"][0]["priority"] is True

    async def test_classification_surfaced_on_thread_and_message(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="lead@x.com", to_email="us@company.com",
            thread_id="t-cls", classification="interested",
        )
        await db_session.commit()

        service = EmailHistoryService()
        res = await service.get_inbox(db_session, filter_type="all")
        thread = res["threads"][0]
        assert thread["classification"] == "interested"
        assert thread["messages"][0]["reply_classification"] == "interested"

    async def test_priority_filter_endpoint(self, client, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="hot@x.com", to_email="us@company.com",
            thread_id="t-ep-hot", priority_flag=True,
        )
        await db_session.commit()
        resp = await client.get("/api/v1/emails/inbox?filter=priority")
        assert resp.status_code == 200
        assert resp.json()["total"] == 1


# ---------------------------------------------------------------------------
# P1-3 · Bulk actions
# ---------------------------------------------------------------------------

class TestBulkActions:
    async def test_bulk_mark_read_flips_selected_only(self, db_session):
        await _make_gmail_account(db_session, "us@company.com", active=True)
        await _make_reply(
            db_session, from_email="a@x.com", to_email="us@company.com",
            thread_id="t-b1", is_read=False,
        )
        await _make_reply(
            db_session, from_email="b@x.com", to_email="us@company.com",
            thread_id="t-b2", is_read=False,
        )
        await _make_reply(
            db_session, from_email="c@x.com", to_email="us@company.com",
            thread_id="t-b3", is_read=False,
        )
        await db_session.commit()

        service = EmailHistoryService()
        n = await service.set_threads_read(db_session, ["t-b1", "t-b2"], is_read=True)
        assert n == 2

        rows = {
            r.gmail_thread_id: r.is_read
            for r in (await db_session.execute(select(EmailReply))).scalars().all()
        }
        assert rows["t-b1"] is True
        assert rows["t-b2"] is True
        assert rows["t-b3"] is False

    async def test_bulk_archive_endpoint(self, client, db_session, make_user, auth_headers):
        user = await make_user("bulk@company.com")
        await _make_gmail_account(db_session, "bulk@company.com", active=True, user_id=user.id)
        for tid in ("t-bulk-1", "t-bulk-2"):
            await _make_reply(
                db_session, from_email="lead@x.com", to_email="bulk@company.com",
                thread_id=tid,
            )
        await db_session.commit()

        resp = await client.post(
            "/api/v1/emails/threads/archive",
            json={"thread_ids": ["t-bulk-1", "t-bulk-2"]},
            headers=auth_headers(user),
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        resp = await client.get("/api/v1/emails/inbox", headers=auth_headers(user))
        assert resp.json()["total"] == 0
