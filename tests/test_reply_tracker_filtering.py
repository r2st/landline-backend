"""Tests for reply-tracker noise filtering and same-domain matching.

Covers audit #3 (bulk/automated mail must never be treated as a prospect reply)
and #4 (a reply from another mailbox at a company we emailed still matches).
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend
from app.services.reply_tracker import (
    ReplyTracker,
    _is_automated_sender,
    get_reply_tracker,
)


# ─── #3: automated-sender filter ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "addr",
    [
        "no-reply@squarespace.com",
        "noreply@kaggle.com",
        "notifications@google.com",
        "newsletter@substack.com",
        "news@claude.com",
        "updates@googlecloud.com",
        "mailer-daemon@googlemail.com",
        "postmaster@corp.com",
        "digest@medium.com",
        "bounce@sendgrid.net",
        "marketing@vendor.com",
    ],
)
def test_automated_senders_are_flagged(addr):
    assert _is_automated_sender(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "dana@acme.com",
        "john.smith@shop.com",
        "andrews@company.com",  # 'news' substring but not a 'news@' mailbox
        "pat@prospect.co",
    ],
)
def test_human_senders_are_not_flagged(addr):
    assert _is_automated_sender(addr) is False


def test_empty_sender_is_treated_as_automated():
    assert _is_automated_sender("") is True


# ─── #4: same-domain fallback matching ───────────────────────────────────────


async def _send(db, to_email, *, contact_id=None, status="sent", days_ago=0):
    ts = datetime.now(timezone.utc)
    send = EmailSend(
        id=uuid.uuid4(), from_email="sender@apprend.tech", to_email=to_email,
        subject="Intro", status=status, contact_id=contact_id, sent_at=ts,
    )
    db.add(send)
    await db.flush()
    return send


@pytest.mark.asyncio
async def test_exact_address_match_still_works(db_session: AsyncSession):
    await _send(db_session, "john@shop.com")
    await db_session.commit()

    info = await get_reply_tracker()._match_reply_to_send(
        db_session, "john@shop.com", "Re: Intro"
    )
    assert info is not None
    assert info["original_subject"] == "Intro"


@pytest.mark.asyncio
async def test_same_domain_fallback_matches_sibling_mailbox(db_session: AsyncSession):
    # We emailed info@shop.com; john@shop.com (never emailed directly) replies.
    await _send(db_session, "info@shop.com")
    await db_session.commit()

    info = await get_reply_tracker()._match_reply_to_send(
        db_session, "john@shop.com", "Re: Intro"
    )
    assert info is not None, "same-domain reply should match the company send"

    # The original send is now marked replied.
    send = (
        await db_session.execute(
            select(EmailSend).where(EmailSend.to_email == "info@shop.com")
        )
    ).scalar_one()
    assert send.status == "replied"


@pytest.mark.asyncio
async def test_same_domain_does_not_match_free_webmail(db_session: AsyncSession):
    # We emailed alice@gmail.com; a different gmail user must NOT match.
    await _send(db_session, "alice@gmail.com")
    await db_session.commit()

    info = await get_reply_tracker()._match_reply_to_send(
        db_session, "bob@gmail.com", "Re: Intro"
    )
    assert info is None


@pytest.mark.asyncio
async def test_no_send_to_domain_returns_none(db_session: AsyncSession):
    await _send(db_session, "info@shop.com")
    await db_session.commit()

    info = await get_reply_tracker()._match_reply_to_send(
        db_session, "someone@unrelated.com", "Re: Intro"
    )
    assert info is None


# ─── #3 end-to-end: SMTP inbox sweep skips newsletters ───────────────────────


class _FakeReader:
    def __init__(self, account, messages):
        self._messages = messages

    def fetch_recent(self, days):
        return self._messages


@pytest.mark.asyncio
async def test_smtp_sweep_skips_automated_and_matches_prospect(
    db_session: AsyncSession, monkeypatch
):
    from app.models.smtp_account import SmtpAccount

    acct = SmtpAccount(
        id=uuid.uuid4(), email="rep@apprend.tech", host="smtp.x.com", port=587,
        username="rep@apprend.tech", password_encrypted="x", status="connected",
        imap_enabled=True,
    )
    db_session.add(acct)
    await db_session.flush()
    # A real prospect we emailed (matching is by EmailSend.to_email).
    await _send(db_session, "pat@prospect.co")
    await db_session.commit()

    messages = [
        {"from_email": "newsletter@substack.com", "subject": "Weekly digest"},
        {"from_email": "no-reply@squarespace.com", "subject": "Your site stats"},
        {"from_email": "mailer-daemon@googlemail.com", "subject": "Delivery Status Notification"},
        # The SMTP reader yields a bare address (via email.utils.parseaddr).
        {"from_email": "pat@prospect.co", "subject": "Re: Intro"},
    ]
    monkeypatch.setattr(
        "app.services.reply_tracker.SmtpMailboxReader",
        lambda account: _FakeReader(account, messages),
    )

    tracker = ReplyTracker()
    replies = await tracker._check_smtp_account_replies(db_session, acct)

    # Only the genuine prospect reply is matched; all bulk/daemon mail skipped.
    assert len(replies) == 1, replies
    assert replies[0]["from_email"] == "pat@prospect.co"
