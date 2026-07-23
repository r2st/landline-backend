"""Tests for Inbox labels (per-user thread tags)."""

import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.gmail_account import GmailAccount
from app.models.inbox_label import InboxLabel, InboxThreadLabel
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


async def _make_reply(db, *, thread_id, to_email="us@company.com"):
    reply = EmailReply(
        id=uuid.uuid4(), from_email="lead@x.com", to_email=to_email,
        subject="Re: hi", body_text="hello", body_html="<p>hello</p>", snippet="hello",
        gmail_thread_id=thread_id, gmail_message_id=f"rep-{uuid.uuid4().hex[:8]}",
        received_at=datetime.now(timezone.utc), is_read=False,
    )
    db.add(reply)
    await db.flush()
    return reply


class TestLabelCrud:
    async def test_create_and_list_labels(self, db_session, make_user):
        user = await make_user("lab@company.com")
        service = EmailHistoryService()

        created = await service.create_label(db_session, user.id, "Hot", "red")
        assert created["name"] == "Hot"
        assert created["color"] == "red"
        assert created["thread_count"] == 0

        labels = await service.list_labels(db_session, user.id)
        assert [l["name"] for l in labels] == ["Hot"]

    async def test_duplicate_name_rejected(self, db_session, make_user):
        user = await make_user("lab2@company.com")
        service = EmailHistoryService()
        await service.create_label(db_session, user.id, "Follow up", "blue")
        dup = await service.create_label(db_session, user.id, "follow up", "green")
        assert dup is None

    async def test_update_and_delete_label(self, db_session, make_user):
        user = await make_user("lab3@company.com")
        service = EmailHistoryService()
        label = await service.create_label(db_session, user.id, "Temp", "gray")

        updated = await service.update_label(
            db_session, user.id, label["id"], name="Renamed", color="indigo"
        )
        assert updated["name"] == "Renamed"
        assert updated["color"] == "indigo"

        assert await service.delete_label(db_session, user.id, label["id"]) is True
        assert await service.list_labels(db_session, user.id) == []

    async def test_delete_cascades_thread_attachments(self, db_session, make_user):
        user = await make_user("lab4@company.com")
        service = EmailHistoryService()
        label = await service.create_label(db_session, user.id, "Bug", "red")
        await service.set_thread_label(db_session, user.id, "t-1", label["id"], True)

        await service.delete_label(db_session, user.id, label["id"])
        rows = (await db_session.execute(select(InboxThreadLabel))).scalars().all()
        assert rows == []


class TestThreadLabeling:
    async def test_attach_and_filter_by_label(self, db_session, make_user):
        user = await make_user("lab5@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-tagged")
        await _make_reply(db_session, thread_id="t-untagged")
        await db_session.commit()
        service = EmailHistoryService()

        label = await service.create_label(db_session, user.id, "VIP", "purple")
        await service.set_thread_label(db_session, user.id, "t-tagged", label["id"], True)

        # Label chips are surfaced on the thread.
        res = await service.get_inbox(db_session, filter_type="all", state_user_id=user.id)
        tagged = next(t for t in res["threads"] if t["gmail_thread_id"] == "t-tagged")
        assert [l["name"] for l in tagged["labels"]] == ["VIP"]

        # Filtering by the label narrows to the tagged thread.
        res = await service.get_inbox(
            db_session, filter_type="all", state_user_id=user.id, label_id=label["id"]
        )
        assert {t["gmail_thread_id"] for t in res["threads"]} == {"t-tagged"}

    async def test_detach_is_idempotent(self, db_session, make_user):
        user = await make_user("lab6@company.com")
        service = EmailHistoryService()
        label = await service.create_label(db_session, user.id, "X", "gray")

        await service.set_thread_label(db_session, user.id, "t-x", label["id"], True)
        await service.set_thread_label(db_session, user.id, "t-x", label["id"], False)
        await service.set_thread_label(db_session, user.id, "t-x", label["id"], False)
        rows = (await db_session.execute(select(InboxThreadLabel))).scalars().all()
        assert rows == []

    async def test_attach_unknown_label_returns_none(self, db_session, make_user):
        user = await make_user("lab7@company.com")
        service = EmailHistoryService()
        result = await service.set_thread_label(
            db_session, user.id, "t-x", uuid.uuid4(), True
        )
        assert result is None

    async def test_labels_are_per_user(self, db_session, make_user):
        alice = await make_user("alice-lab@company.com")
        bob = await make_user("bob-lab@company.com")
        await _make_gmail_account(db_session, "us@company.com")
        await _make_reply(db_session, thread_id="t-shared-lab")
        await db_session.commit()
        service = EmailHistoryService()

        label = await service.create_label(db_session, alice.id, "Mine", "red")
        await service.set_thread_label(db_session, alice.id, "t-shared-lab", label["id"], True)

        # Bob can't attach Alice's label.
        assert await service.set_thread_label(
            db_session, bob.id, "t-shared-lab", label["id"], True
        ) is None
        # Bob sees no labels on the shared thread.
        res = await service.get_inbox(db_session, filter_type="all", state_user_id=bob.id)
        assert res["threads"][0]["labels"] == []


class TestLabelEndpoints:
    async def test_label_lifecycle_endpoints(self, client, db_session, make_user, auth_headers):
        user = await make_user("lab-ep@company.com")
        await _make_gmail_account(db_session, "lab-ep@company.com", user_id=user.id)
        await _make_reply(db_session, thread_id="t-ep-lab", to_email="lab-ep@company.com")
        await db_session.commit()
        headers = auth_headers(user)

        # Create.
        resp = await client.post(
            "/api/v1/emails/labels", json={"name": "Priority", "color": "orange"},
            headers=headers,
        )
        assert resp.status_code == 201
        label_id = resp.json()["id"]

        # Duplicate → 409.
        resp = await client.post(
            "/api/v1/emails/labels", json={"name": "Priority"}, headers=headers
        )
        assert resp.status_code == 409

        # Attach to a thread.
        resp = await client.post(
            f"/api/v1/emails/thread/t-ep-lab/labels/{label_id}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["attached"] is True

        # Inbox filtered by label shows the thread + chip.
        resp = await client.get(
            f"/api/v1/emails/inbox?label_id={label_id}", headers=headers
        )
        assert resp.json()["total"] == 1
        assert resp.json()["threads"][0]["labels"][0]["name"] == "Priority"

        # List shows a thread_count of 1.
        resp = await client.get("/api/v1/emails/labels", headers=headers)
        assert resp.json()[0]["thread_count"] == 1

        # Detach + delete.
        resp = await client.delete(
            f"/api/v1/emails/thread/t-ep-lab/labels/{label_id}", headers=headers
        )
        assert resp.json()["attached"] is False
        resp = await client.delete(f"/api/v1/emails/labels/{label_id}", headers=headers)
        assert resp.status_code == 204
