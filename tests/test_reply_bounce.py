"""Tests for the ``bounce`` reply-classification category.

A delivery-status notification (DSN) that lands in the mailbox as a reply must
be recognised as a bounce — not misread as an unsubscribe just because the DSN
quotes our original email's opt-out footer — and routed into the bounce pipeline
so the *failed recipient* is suppressed.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.services import ai_service
from app.services.ai_service import is_bounce, keyword_prefilter
from app.tasks.replies import classify_reply_record


def _now():
    return datetime.now(timezone.utc)


# --- detection (pure) --------------------------------------------------------


def test_is_bounce_by_daemon_sender():
    assert is_bounce("anything", "body", "MAILER-DAEMON@googlemail.com")
    assert is_bounce("hi", "body", "postmaster@acme.com")


def test_is_bounce_by_subject():
    assert is_bounce("Delivery Status Notification (Failure)", "", "x@y.com")
    assert is_bounce("Undelivered Mail Returned to Sender", "", "x@y.com")
    assert is_bounce("Mail delivery failed: returning message to sender", "", "x@y.com")


def test_normal_reply_is_not_a_bounce():
    assert not is_bounce("Re: your email", "Sure, let's talk", "dana@acme.com")


def test_bounce_wins_over_footer_unsubscribe():
    """A DSN echoing our unsubscribe footer must classify as bounce, not unsub."""
    body = "Your message could not be delivered.\n\n--- Original ---\nUnsubscribe here: http://x/u"
    result = keyword_prefilter("Delivery Status Notification", body, "mailer-daemon@x.com")
    assert result == "bounce"


def test_human_unsubscribe_still_wins_for_real_person():
    result = keyword_prefilter("Re: hi", "please unsubscribe me", "dana@acme.com")
    assert result == "unsubscribe"


@pytest.mark.asyncio
async def test_classify_reply_marks_bounce_via_keyword():
    result = await ai_service.get_ai_service().classify_reply(
        subject="Delivery Status Notification (Failure)",
        body_text="550 5.1.1 user unknown",
        from_email="mailer-daemon@googlemail.com",
    )
    assert result["category"] == "bounce"
    assert result["source"] == "keyword"


# --- routing -----------------------------------------------------------------


async def _make_contact(db, sample_company, **kw):
    kw.setdefault("email", "dana@acmestamp.com")
    contact = Contact(
        id=uuid.uuid4(),
        company_id=sample_company.id,
        first_name="Dana",
        last_name="Reyes",
        source="test",
        **kw,
    )
    db.add(contact)
    await db.flush()
    return contact


@pytest.mark.asyncio
async def test_bounce_reply_suppresses_hard_bounced_contact(db_session, sample_company):
    contact = await _make_contact(db_session, sample_company)
    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=contact.id,
        from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body_text="550 5.1.1 The email account that you tried to reach does not exist.",
        received_at=_now(),
    )
    db_session.add(reply)
    await db_session.flush()

    summary = await classify_reply_record(db_session, reply)

    assert summary["category"] == "bounce"
    assert summary["handled_action"] == "bounced"
    # Hard bounce (5.1.1) → the contact is suppressed and its bounce recorded.
    assert contact.unsubscribed is True
    assert contact.bounce_count == 1
    assert contact.last_bounce_type == "hard"


@pytest.mark.asyncio
async def test_bounce_uses_send_recipient_when_no_contact(db_session, sample_company):
    # The failed recipient exists as a contact, but the reply resolves to no
    # contact (from_email is the daemon) — routing must fall back to the send's
    # to_email to find who actually bounced.
    victim = await _make_contact(
        db_session, sample_company, email="bouncer@acmestamp.com"
    )
    send = EmailSend(
        id=uuid.uuid4(),
        contact_id=None,
        to_email="bouncer@acmestamp.com",
        subject="Following up",
        status="sent",
        sent_at=_now(),
    )
    db_session.add(send)
    await db_session.flush()

    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=None,
        email_send_id=send.id,
        from_email="mailer-daemon@googlemail.com",
        subject="Undelivered Mail Returned to Sender",
        body_text="550 5.1.1 user unknown",
        received_at=_now(),
    )
    db_session.add(reply)
    await db_session.flush()

    summary = await classify_reply_record(db_session, reply)

    assert summary["category"] == "bounce"
    assert summary["handled_action"] == "bounced"
    await db_session.refresh(victim)
    assert victim.unsubscribed is True
    assert victim.bounce_count == 1


@pytest.mark.asyncio
async def test_bounce_without_recipient_is_left_for_review(db_session):
    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=None,
        from_email="mailer-daemon@googlemail.com",
        subject="Delivery Status Notification (Failure)",
        body_text="550 user unknown",
        received_at=_now(),
    )
    db_session.add(reply)
    await db_session.flush()

    summary = await classify_reply_record(db_session, reply)

    assert summary["category"] == "bounce"
    # No contact and no send to tie it to → recorded but not auto-actioned.
    assert summary["handled_action"] == "none"
