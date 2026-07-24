"""A bounce must never be processed as a prospect reply.

A delivery-status notification lands in the *same Gmail thread* as the send it
failed, so ``_process_single_reply``'s header/thread matcher tied it straight to
the enrollment and filed it as a reply. Observed in production on
2026-07-23: enrollment 0faf794f (sales@omniadesignmfg.com) was marked
``replied`` by a "Delivery Status Notification (Failure)" from
mailer-daemon@googlemail.com — permanently stopping outreach to a contact who
never answered, and booking a reply into the funnel that never happened.

``email_history.sync_replies_for_account`` already gated its own sync with
``is_bounce_message``; this path was missing the same guard.
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
    async def analyze_reply(self, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("AI analysis must not run for a bounce")


def _gmail_message(*, from_email, subject, body="Delivery failed permanently.",
                   thread_id="thr-1", in_reply_to=None):
    headers = [
        {"name": "From", "value": from_email},
        {"name": "Subject", "value": subject},
        {"name": "Message-Id", "value": "<dsn-1@googlemail.com>"},
    ]
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": "gmail-msg-1",
        "threadId": thread_id,
        "payload": {"headers": headers, "body": {"data": data}},
    }


async def _seq(db):
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    return seq


async def _enrollment(db, contact, seq, *, thread="thr-1", message="gmsg-1"):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
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
    return (
        await db.execute(
            select(OutreachEvent).where(
                OutreachEvent.enrollment_id == enrollment_id,
                OutreachEvent.event_type == "replied",
            )
        )
    ).scalars().all()


@pytest.fixture
def _sending_on(monkeypatch):
    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _FakeAI())


# The exact production shapes, plus the delay notice that precedes a hard fail.
BOUNCE_CASES = [
    ("mailer-daemon@googlemail.com", "Delivery Status Notification (Failure)"),
    ("mailer-daemon@googlemail.com", "Delivery Status Notification (Delay)"),
    ("MAILER-DAEMON@apprend.tech", "Undelivered Mail Returned to Sender"),
    ("postmaster@example.com", "Delivery Status Notification (Failure)"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("from_email,subject", BOUNCE_CASES)
async def test_bounce_in_the_send_thread_is_not_a_reply(
    db_session, sample_contact, _sending_on, from_email, subject
):
    """The regression: a DSN threaded onto our own send must be dropped."""
    seq = await _seq(db_session)
    enr = await _enrollment(db_session, sample_contact, seq)
    acct = await _gmail_account(db_session)
    await db_session.commit()

    # Threaded onto our send — this is what made the matcher claim it.
    msg = _gmail_message(
        from_email=from_email, subject=subject, in_reply_to="<gmsg-1@mail.gmail.com>"
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is None, "a bounce must not be processed as a reply"

    await db_session.refresh(enr)
    # The enrollment stays live — outreach to this contact must not be killed.
    assert enr.status == "active"
    # And the funnel must not count a reply that never happened.
    assert await _replied_events(db_session, enr.id) == []
    # Never auto-reply to mailer-daemon.
    assert gmail.store["sent"] == []


@pytest.mark.asyncio
async def test_a_real_reply_still_gets_through(db_session, sample_contact, monkeypatch):
    """Guard rail: the bounce filter must not swallow genuine prospect replies."""

    class _PositiveAI:
        async def analyze_reply(self, **kwargs):
            return {
                "intent": "interested",
                "confidence": 0.9,
                "summary": "wants to talk",
                "draft_reply": {"subject": "Re: Intro", "body": "How's Tuesday?"},
                "should_send": True,
                "should_auto_send": False,
                "suggested_action": "schedule_meeting",
                "follow_up_days": None,
            }

    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _PositiveAI())

    seq = await _seq(db_session)
    enr = await _enrollment(db_session, sample_contact, seq)
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        subject="Re: who eats it when a part runs out of tol?",
        body="Interesting — can you send more detail?",
        in_reply_to="<gmsg-1@mail.gmail.com>",
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is not None, "a genuine reply must still be processed"
    await db_session.refresh(enr)
    assert enr.status == "replied"
    assert len(await _replied_events(db_session, enr.id)) == 1


@pytest.mark.asyncio
async def test_prospect_whose_subject_mentions_delivery_is_not_dropped(
    db_session, sample_contact, monkeypatch
):
    """A real person writing about *their* delivery problem is not a bounce.

    The filter keys on DSN sender markers and DSN subject phrasing, so ordinary
    prospect mail that happens to use the word "delivery" must survive.
    """

    class _AI:
        async def analyze_reply(self, **kwargs):
            return {
                "intent": "question", "confidence": 0.8, "summary": "asks about delivery",
                "draft_reply": {"subject": "Re", "body": "Sure."},
                "should_send": True, "should_auto_send": False,
                "suggested_action": "reply", "follow_up_days": None,
            }

    async def _enabled(db):
        return True

    monkeypatch.setattr(system_settings, "is_sending_enabled", _enabled)
    monkeypatch.setattr(smart_reply_module, "get_ai_service", lambda: _AI())

    seq = await _seq(db_session)
    enr = await _enrollment(db_session, sample_contact, seq)
    acct = await _gmail_account(db_session)
    await db_session.commit()

    msg = _gmail_message(
        from_email=sample_contact.email,
        subject="Re: can you help with our delivery delays?",
        body="We keep missing ship dates — tell me more.",
        in_reply_to="<gmsg-1@mail.gmail.com>",
    )
    gmail = _FakeGmail(msg)

    result = await SmartReplyService()._process_single_reply(
        db_session, gmail, "gmail-msg-1", acct
    )

    assert result is not None, "a prospect writing about delivery is not a bounce"
    await db_session.refresh(enr)
    assert enr.status == "replied"
