"""Tests for Inbox snooze (per-user, backed by inbox_thread_state.snoozed_until).

A snoozed thread drops out of the default view and every filter except
``snoozed``; it resurfaces automatically once ``snoozed_until`` passes (no sweep
needed). The ``clear_expired_snoozes`` task only tidies the elapsed rows.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.gmail_account import GmailAccount
from app.models.inbox_thread_state import InboxThreadState
from app.services import crypto, google_oauth
from app.services.email_history import EmailHistoryService
from app.tasks.inbox import clear_expired_snoozes_async


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
        id=uuid.uuid4(),
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0].title(),
        refresh_token_encrypted=crypto.encrypt("refresh-xyz"),
        access_token_encrypted=crypto.encrypt("access-xyz"),
        scopes=" ".join(google_oauth.SCOPES),
        is_active=True,
        status="connected",
        user_id=user_id,
    )
    db.add(account)
    await db.flush()
    return account


async def _make_reply(db, *, thread_id, to_email="us@company.com", from_email="lead@x.com"):
    reply = EmailReply(
        id=uuid.uuid4(),
        from_email=from_email,
        to_email=to_email,
        subject="Re: hi",
        body_text="hello",
        body_html="<p>hello</p>",
        snippet="hello",
        gmail_thread_id=thread_id,
        gmail_message_id=f"rep-{uuid.uuid4().hex[:8]}",
        received_at=datetime.now(timezone.utc),
        is_read=False,
    )
    db.add(reply)
    await db.flush()
    return reply


class TestSnooze:
    async def test_snoozed_thread_leaves_default_view(self, db_session, make_user):
        user = await make_user("rep@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-snooze")
        await db_session.commit()
        service = EmailHistoryService()

        future = datetime.now(timezone.utc) + timedelta(hours=3)
        await service.set_threads_snoozed(db_session, ["t-snooze"], future, user.id)

        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert res["total"] == 0

        snoozed = await service.get_inbox(
            db_session, filter_type="snoozed", state_user_id=user.id
        )
        assert {t["gmail_thread_id"] for t in snoozed["threads"]} == {"t-snooze"}
        assert snoozed["threads"][0]["snoozed_until"] is not None

    async def test_elapsed_snooze_resurfaces_automatically(self, db_session, make_user):
        user = await make_user("rep2@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-past")
        await db_session.commit()
        service = EmailHistoryService()

        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        await service.set_threads_snoozed(db_session, ["t-past"], past, user.id)

        # Already elapsed → treated as not snoozed, back in the default view.
        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-past"}
        assert res["threads"][0]["snoozed_until"] is None
        # ...and NOT in the snoozed tab.
        snoozed = await service.get_inbox(
            db_session, filter_type="snoozed", state_user_id=user.id
        )
        assert snoozed["total"] == 0

    async def test_unsnooze_restores_immediately(self, db_session, make_user):
        user = await make_user("rep3@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-uns")
        await db_session.commit()
        service = EmailHistoryService()

        future = datetime.now(timezone.utc) + timedelta(days=1)
        await service.set_threads_snoozed(db_session, ["t-uns"], future, user.id)
        await service.set_threads_snoozed(db_session, ["t-uns"], None, user.id)

        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-uns"}
        # Upsert, not duplicate insert.
        rows = (await db_session.execute(
            select(InboxThreadState).where(InboxThreadState.gmail_thread_id == "t-uns")
        )).scalars().all()
        assert len(rows) == 1

    async def test_snooze_is_per_user(self, db_session, make_user):
        alice = await make_user("alice-sn@company.com")
        bob = await make_user("bob-sn@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-shared-sn")
        await db_session.commit()
        service = EmailHistoryService()

        future = datetime.now(timezone.utc) + timedelta(hours=6)
        await service.set_threads_snoozed(db_session, ["t-shared-sn"], future, alice.id)

        alice_view = await service.get_inbox(db_session, filter_type="all", state_user_id=alice.id)
        bob_view = await service.get_inbox(db_session, filter_type="all", state_user_id=bob.id)
        assert alice_view["total"] == 0
        assert {t["gmail_thread_id"] for t in bob_view["threads"]} == {"t-shared-sn"}

    async def test_clear_expired_snoozes_nulls_only_elapsed(self, db_session, make_user):
        user = await make_user("rep4@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-elapsed")
        await _make_reply(db_session, thread_id="t-future")
        await db_session.commit()
        service = EmailHistoryService()

        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        future = datetime.now(timezone.utc) + timedelta(days=1)
        await service.set_threads_snoozed(db_session, ["t-elapsed"], past, user.id)
        await service.set_threads_snoozed(db_session, ["t-future"], future, user.id)

        cleared = await clear_expired_snoozes_async(db_session)
        await db_session.commit()
        assert cleared == 1

        rows = {
            s.gmail_thread_id: s.snoozed_until
            for s in (await db_session.execute(select(InboxThreadState))).scalars().all()
        }
        assert rows["t-elapsed"] is None
        assert rows["t-future"] is not None


class TestSnoozeEndpoints:
    async def test_snooze_endpoint_requires_user(self, client, db_session):
        await _make_gmail_account(db_session, "us@company.com")
        await db_session.commit()
        resp = await client.post(
            "/api/v1/emails/thread/t-x/snooze", json={"snoozed_until": None}
        )
        assert resp.status_code == 400

    async def test_snooze_endpoint_scoped_user(self, client, db_session, make_user, auth_headers):
        user = await make_user("scoped-sn@company.com")
        await _make_gmail_account(db_session, "scoped-sn@company.com", user_id=user.id)
        await _make_reply(db_session, thread_id="t-ep-sn", to_email="scoped-sn@company.com")
        await db_session.commit()

        headers = auth_headers(user)
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        resp = await client.post(
            "/api/v1/emails/thread/t-ep-sn/snooze",
            json={"snoozed_until": until},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["snoozed_until"] is not None

        # Gone from default view, present in snoozed tab.
        resp = await client.get("/api/v1/emails/inbox", headers=headers)
        assert resp.json()["total"] == 0
        resp = await client.get("/api/v1/emails/inbox?filter=snoozed", headers=headers)
        assert resp.json()["total"] == 1

        # Un-snooze restores it.
        resp = await client.post("/api/v1/emails/thread/t-ep-sn/unsnooze", headers=headers)
        assert resp.status_code == 200
        resp = await client.get("/api/v1/emails/inbox", headers=headers)
        assert resp.json()["total"] == 1
