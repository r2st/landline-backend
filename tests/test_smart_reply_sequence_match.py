"""SmartReplyService must answer replies to *sequence* sends, not just legacy
EmailSend rows.

Cold-outreach sequence sends are recorded as OutreachEvents and carry NO
EmailSend row (see app/tasks/outreach.py). Before this fix,
``_process_single_reply`` bailed with ``return None`` whenever no EmailSend
matched the sender, so the auto-reply engine never fired for real prospects —
only for the legacy engagement path. These cover the reordered matching:
enrollment is resolved by contact / threading headers *before* the EmailSend
gate, so a sequence reply is stopped, recorded, and answered; genuine inbox
noise is still skipped; and an already-answered reply is idempotent.
"""

import base64
import uuid

import pytest
from sqlalchemy import select

from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services import smart_reply as smart_reply_module
from app.services import system_settings
from app.services.smart_reply import SmartReplyService


# ── fakes ──────────────────────────────────────────────────────────────────────

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
    """Minimal stand-in for the googleapiclient Gmail resource."""

    def __init__(self, msg):
        self.store = {"msg": msg, "sent": [], "modified": []}

    def users(self):
        return _FakeUsers(self.store)


class _FakeAI:
    """analyze_reply stub — returns a clearly-positive, auto-sendable analysis."""

    async def analyze_reply(self, **kwargs):
        return {
            "intent": "interested",
            "confidence": 0.9,
            "summary": "wants to talk",
            "draft_reply": {"subject": "Re: Intro", "body": "Great — how's Tuesday?"},
            "should_send": True,
            "should_auto_send": True,
            "suggested_action": "schedule_meeting",
            "follow_up_days": None,
        }


def _gmail_message(
    *,
    from_email,
    subject="Re: Intro",
    body="Sounds great, let's talk.",
    thread_id="thr-1",
    message_id="<reply-x@prospect.co>",
    in_reply_to=None,
    references=None,
):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "Subject", "value": subject},
        {"name": "Message-Id", "value": message_id},
    ]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    if references:
        headers.append({"name": "References", "value": references})
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": "gmail-msg-1",
        "threadId": thread_id,
        "payload": {"headers": headers, "body": {"data": data}},
    }


# ── helpers ─────────────────────────────────────────────────────────────────────

async def _seq(db):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    return seq


async def _enrollment(db, contact, seq, *, status="active", thread=None, message=None):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status=status,
        gmail_thread_id=thread, gmail_message_id=message,
    )
    db.add(enr)
    await db.flush()
    return enr


async def _gmail_account(db):
    acct = GmailAccount(
        id=uuid.uuid4(),
        email=f"rep-{uuid.uuid4().hex[:6]}@apprend.tech",
        google_sub=uuid.uuid4().hex,
        refresh_token_encrypted="x",
        status="connected",
        is_active=True,
    )
    db.add(acct)
    await db.flush()
    return acct


async def _replied_events(db, enrollment_id):
    rows = (
        await db.execute(
            select(OutreachEvent).where(
                OutreachEvent.enrollment_id == enrollment_id,
                OutreachEvent.event_type == "replied",
            )
        )
    ).scalars().all()
    return rows


@pytest.fixture
def _sending_on(monkeypatch):
    """Force the kill switch open and the AI to a canned analysis."""
    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())


# ── tests ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sequence_reply_matched_by_headers_stops_and_autosends(
    db_session, sample_contact, _sending_on
):
    """A reply to a sequence send (no EmailSend row) matched by In-Reply-To is
    stopped, recorded, and auto-answered — the whole point of the fix."""
    seq = await _seq(db_session)
    enr = await _enrollment(
        db_session, sample_contact, seq, message="gmsg-1", thread="thr-1"
    )
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        in_reply_to="<gmsg-1@mail.gmail.com>",
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is not None, "reply to a sequence send must not be dropped"
    assert result["auto_sent"] is True
    assert len(gmail.store["sent"]) == 1  # a reply actually went out

    await db_session.refresh(enr)
    assert enr.status == "replied"
    assert len(await _replied_events(db_session, enr.id)) == 1


@pytest.mark.asyncio
async def test_sequence_reply_matched_by_contact_address(
    db_session, sample_contact, _sending_on
):
    """No EmailSend and no threading headers — the sender address alone resolves
    the contact and their active enrollment."""
    seq = await _seq(db_session)
    enr = await _enrollment(db_session, sample_contact, seq)  # no gmail ids
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(from_email=sample_contact.email)  # no In-Reply-To
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is not None
    await db_session.refresh(enr)
    assert enr.status == "replied"


@pytest.mark.asyncio
async def test_unrelated_inbox_mail_is_skipped(db_session, sample_contact, _sending_on):
    """A message with no prior send and no matching enrollment is inbox noise."""
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(
        from_email="stranger@newsletter.example.com",
        in_reply_to="<not-ours@x>",
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is None
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_already_replied_sequence_is_idempotent(
    db_session, sample_contact, _sending_on
):
    """A second sweep over an already-answered reply must not re-send. Both
    matchers exclude the terminal 'replied' status, so nothing re-fires."""
    seq = await _seq(db_session)
    await _enrollment(
        db_session, sample_contact, seq, status="replied", message="gmsg-2"
    )
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        in_reply_to="<gmsg-2@mail.gmail.com>",
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is None
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_legacy_emailsend_path_still_works(
    db_session, sample_contact, _sending_on
):
    """Regression: a reply that DOES have an EmailSend row still stops the
    enrollment, marks the send replied, and auto-answers."""
    from datetime import datetime, timezone

    from app.models.email_template import EmailSend

    seq = await _seq(db_session)
    enr = await _enrollment(db_session, sample_contact, seq)
    send = EmailSend(
        id=uuid.uuid4(),
        from_email="rep@apprend.tech",
        to_email=sample_contact.email,
        subject="Intro",
        status="sent",
        sent_at=datetime.now(timezone.utc),
        contact_id=sample_contact.id,
    )
    db_session.add(send)
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(from_email=sample_contact.email)
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is not None
    assert result["auto_sent"] is True
    await db_session.refresh(send)
    await db_session.refresh(enr)
    assert send.status == "replied"
    assert send.replied_at is not None
    assert enr.status == "replied"
