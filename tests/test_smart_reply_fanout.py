"""Per-account fan-out for the SmartReplyService (P1 fix).

The reply sweep no longer loops every connected mailbox serially on the event
loop. ``check_email_replies`` enumerates connected accounts and dispatches a
per-account Celery task; the service exposes ``connected_account_ids`` (the
enumeration) and ``process_account_by_id`` (the per-account entry point), and all
Gmail I/O is offloaded off the loop via ``_gexec``.
"""

import base64
import uuid

import pytest

from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import smart_reply as smart_reply_module
from app.services import system_settings
from app.services.smart_reply import SmartReplyService


class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _FakeMessages:
    def __init__(self, store):
        self.store = store

    def list(self, userId, q, maxResults):
        return _Exec({"messages": [{"id": "m-1"}]})

    def get(self, userId, id, format=None):
        return _Exec(self.store["msg"])

    def send(self, userId, body):
        self.store["sent"].append(body)
        return _Exec({"id": "sent-1"})

    def modify(self, userId, id, body):
        return _Exec({})


class _FakeUsers:
    def __init__(self, store):
        self.store = store

    def messages(self):
        return _FakeMessages(self.store)


class _FakeGmail:
    def __init__(self, msg):
        self.store = {"msg": msg, "sent": []}

    def users(self):
        return _FakeUsers(self.store)


class _FakeAI:
    async def analyze_reply(self, **kwargs):
        return {
            "intent": "interested", "confidence": 0.9, "summary": "yes",
            "draft_reply": {"subject": "Re: Intro", "body": "Tuesday?"},
            "should_send": True, "should_auto_send": True,
            "suggested_action": "schedule_meeting", "follow_up_days": None,
        }


def _gmail_message(from_email):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "Subject", "value": "Re: Intro"},
        {"name": "Message-Id", "value": "<r@x.co>"},
    ]
    data = base64.urlsafe_b64encode(b"sounds good").decode()
    return {"id": "m-1", "threadId": "t-1",
            "payload": {"headers": headers, "body": {"data": data}}}


async def _account(db, *, status="connected"):
    acct = GmailAccount(
        id=uuid.uuid4(), email=f"rep-{uuid.uuid4().hex[:6]}@apprend.tech",
        google_sub=uuid.uuid4().hex, refresh_token_encrypted="x",
        status=status, is_active=True,
    )
    db.add(acct)
    await db.flush()
    return acct


async def test_connected_account_ids_lists_only_connected(db_session):
    a = await _account(db_session, status="connected")
    await _account(db_session, status="disconnected")
    await db_session.commit()

    ids = await SmartReplyService().connected_account_ids(db_session)
    assert a.id in ids
    assert len(ids) == 1


async def test_process_account_by_id_processes_one_mailbox(
    db_session, sample_contact, monkeypatch
):
    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(system_settings, "is_auto_reply_enabled", _enabled)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())

    seq = OutreachSequence(id=uuid.uuid4(), name="seq")
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(id=uuid.uuid4(), contact_id=sample_contact.id,
                             sequence_id=seq.id, current_step=1, status="active")
    db_session.add(enr)
    acct = await _account(db_session)
    await db_session.commit()

    service = SmartReplyService()
    # Patch the credential build + Gmail client so no real network happens.
    monkeypatch.setattr(service, "_build_credentials", lambda account: object())
    gmail = _FakeGmail(_gmail_message(sample_contact.email))
    monkeypatch.setattr(smart_reply_module, "build", lambda *a, **k: gmail)

    results = await service.process_account_by_id(db_session, acct.id)
    assert len(results) == 1
    assert results[0]["receiving_account"] == acct.email
    await db_session.refresh(enr)
    assert enr.status == "replied"


async def test_process_account_by_id_skips_unknown_or_disconnected(db_session, monkeypatch):
    # Unknown id → empty, no crash.
    assert await SmartReplyService().process_account_by_id(db_session, uuid.uuid4()) == []
    # Disconnected account → skipped.
    acct = await _account(db_session, status="disconnected")
    await db_session.commit()
    assert await SmartReplyService().process_account_by_id(db_session, acct.id) == []
