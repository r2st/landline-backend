"""Auto-send safety floor for the SmartReplyService (P1 fix).

_process_single_reply must NEVER auto-send when:
  - the inbound message is machine-generated (Auto-Submitted / Precedence: bulk /
    List-* / X-Autoreply) — an OOO bot or a mailing list;
  - the reply-analysis confidence is below the configured floor;
  - the org has opted OUT of auto-replies.

In every blocked case it still drafts (records the reply, stops the enrollment)
but leaves the send to a human, and stamps auto_send_decision with the reason.
"""

import base64
import uuid

import pytest

from app.config import get_settings
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import smart_reply as smart_reply_module
from app.services import system_settings
from app.services.smart_reply import SmartReplyService, is_automated_message


# ── fakes (mirror test_smart_reply_sequence_match) ──────────────────────────────

class _Exec:
    def __init__(self, val):
        self._val = val

    def execute(self):
        return self._val


class _FakeMessages:
    def __init__(self, store):
        self.store = store

    def get(self, userId, id, format=None):
        return _Exec(self.store["msg"])

    def send(self, userId, body):
        self.store["sent"].append(body)
        return _Exec({"id": "gmail-sent-1"})

    def modify(self, userId, id, body):
        self.store["modified"].append((id, body))
        return _Exec({})


class _FakeUsers:
    def __init__(self, store):
        self.store = store

    def messages(self):
        return _FakeMessages(self.store)


class _FakeGmail:
    def __init__(self, msg):
        self.store = {"msg": msg, "sent": [], "modified": []}

    def users(self):
        return _FakeUsers(self.store)


class _FakeAI:
    def __init__(self, confidence=0.9, intent="interested"):
        self._confidence = confidence
        self._intent = intent

    async def analyze_reply(self, **kwargs):
        return {
            "intent": self._intent,
            "confidence": self._confidence,
            "summary": "wants to talk",
            "draft_reply": {"subject": "Re: Intro", "body": "Great — how's Tuesday?"},
            "should_send": True,
            "should_auto_send": True,
            "suggested_action": "schedule_meeting",
            "follow_up_days": None,
        }


def _gmail_message(*, from_email, extra_headers=None, body="Sounds great, let's talk."):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "Subject", "value": "Re: Intro"},
        {"name": "Message-Id", "value": "<reply-x@prospect.co>"},
    ]
    for name, value in (extra_headers or {}).items():
        headers.append({"name": name, "value": value})
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": "gmail-msg-1",
        "threadId": "thr-1",
        "payload": {"headers": headers, "body": {"data": data}},
    }


async def _setup(db, *, contact):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    acct = GmailAccount(
        id=uuid.uuid4(), email=f"rep-{uuid.uuid4().hex[:6]}@apprend.tech",
        google_sub=uuid.uuid4().hex, refresh_token_encrypted="x",
        status="connected", is_active=True,
    )
    db.add(acct)
    await db.flush()
    return enr, acct


@pytest.fixture
def _ai_and_switch(monkeypatch):
    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    # Default: org opted in (mirror config default) unless a test overrides it.
    async def _opted_in(db):
        return True

    monkeypatch.setattr(system_settings, "is_auto_reply_enabled", _opted_in)


# ── unit: header detection ──────────────────────────────────────────────────────

def test_is_automated_message_signals():
    assert is_automated_message({"auto-submitted": "auto-replied"})
    assert is_automated_message({"auto-submitted": "auto-generated"})
    assert is_automated_message({"precedence": "bulk"})
    assert is_automated_message({"precedence": "list"})
    assert is_automated_message({"list-unsubscribe": "<mailto:x>"})
    assert is_automated_message({"list-id": "<news.corp>"})
    assert is_automated_message({"x-autoreply": "yes"})
    # Not automated: a normal human reply, and Auto-Submitted: no.
    assert is_automated_message({"from": "a@b.com", "subject": "hi"}) is None
    assert is_automated_message({"auto-submitted": "no"}) is None


# ── integration: the floor blocks auto-send but still drafts ────────────────────

@pytest.mark.asyncio
async def test_out_of_office_bot_is_not_auto_replied(
    db_session, sample_contact, _ai_and_switch, monkeypatch
):
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())
    enr, acct = await _setup(db_session, contact=sample_contact)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        extra_headers={"Auto-Submitted": "auto-replied", "Subject": "Out of Office"},
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )
    assert result is not None
    assert result["auto_sent"] is False
    assert "automated_message" in result["auto_send_decision"]["reason"]
    assert gmail.store["sent"] == []  # nothing went out
    # Still recorded the reply / stopped the enrollment (drafted, not sent).
    await db_session.refresh(enr)
    assert enr.status == "replied"


@pytest.mark.asyncio
async def test_mailing_list_is_not_auto_replied(
    db_session, sample_contact, _ai_and_switch, monkeypatch
):
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())
    _, acct = await _setup(db_session, contact=sample_contact)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        extra_headers={"List-Unsubscribe": "<mailto:leave@list.co>"},
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )
    assert result["auto_sent"] is False
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_low_confidence_is_not_auto_sent(
    db_session, sample_contact, _ai_and_switch, monkeypatch
):
    # Confidence below the 0.75 floor → draft only.
    monkeypatch.setattr(
        smart_reply_module, "get_ai_service", lambda: _FakeAI(confidence=0.40)
    )
    _, acct = await _setup(db_session, contact=sample_contact)
    await db_session.commit()

    msg = _gmail_message(from_email=sample_contact.email)
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )
    assert result["auto_sent"] is False
    assert "low_confidence" in result["auto_send_decision"]["reason"]
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_org_opt_out_blocks_auto_send(
    db_session, sample_contact, monkeypatch
):
    async def _enabled(db):
        return True

    async def _opted_out(db):
        return False

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(system_settings, "is_auto_reply_enabled", _opted_out)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())

    _, acct = await _setup(db_session, contact=sample_contact)
    await db_session.commit()

    msg = _gmail_message(from_email=sample_contact.email)
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )
    assert result["auto_sent"] is False
    assert result["auto_send_decision"]["reason"] == "org_opt_out"
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_high_confidence_human_reply_auto_sends(
    db_session, sample_contact, _ai_and_switch, monkeypatch
):
    # Control: a clean, confident, human reply DOES auto-send + stamps positive.
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())
    enr, acct = await _setup(db_session, contact=sample_contact)
    await db_session.commit()

    msg = _gmail_message(from_email=sample_contact.email)
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )
    assert result["auto_sent"] is True
    assert result["auto_send_decision"]["sent"] is True
    assert len(gmail.store["sent"]) == 1
