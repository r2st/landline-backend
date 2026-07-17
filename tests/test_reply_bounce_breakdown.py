"""Tests for the bounce-category breakdown on the reply detail endpoint.

``GET /dialtone/inbox/{id}`` returns a ``bounce`` block for replies classified
``bounce`` — the hard/soft category, DSN status + diagnostic code, and the
failed recipient — so an operator can tell a dead mailbox from a transient
failure without leaving the inbox. Diagnostics come from the matching
``BounceRecord`` when the sweep has ingested the same DSN; otherwise from the
reply's own classification metadata and the contact's bounce counters.
"""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.email_reply import EmailReply


def _now():
    return datetime.now(timezone.utc)


async def _make_bounce_reply(db, contact, *, meta=None):
    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=contact.id,
        company_id=contact.company_id,
        from_email="mailer-daemon@googlemail.com",
        to_email="suman.das@apprend.tech",
        subject="Delivery Status Notification (Failure)",
        body_text="Your message could not be delivered.",
        received_at=_now(),
        reply_classification="bounce",
        classification_confidence=1.0,
        classification_meta=meta or {},
    )
    db.add(reply)
    await db.flush()
    return reply


@pytest.mark.asyncio
async def test_bounce_breakdown_from_bounce_record(
    client: AsyncClient, db_session: AsyncSession, sample_contact
):
    # A hard bounce record for the contact's address (stored lowercased).
    db_session.add(BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-{uuid.uuid4().hex[:10]}",
        recipient_email=sample_contact.email.lower(),
        bounce_kind="hard",
        status_code="5.1.1",
        diagnostic="550 5.1.1 The email account that you tried to reach does not exist.",
        processed_at=_now(),
    ))
    reply = await _make_bounce_reply(db_session, sample_contact)

    resp = await client.get(f"/api/v1/dialtone/inbox/{reply.id}")
    assert resp.status_code == 200, resp.text
    bounce = resp.json()["bounce"]
    assert bounce is not None
    assert bounce["category"] == "hard"
    assert bounce["status_code"] == "5.1.1"
    assert "does not exist" in bounce["diagnostic"]
    assert bounce["recipient"] == sample_contact.email


@pytest.mark.asyncio
async def test_bounce_breakdown_falls_back_to_meta_when_no_record(
    client: AsyncClient, db_session: AsyncSession, sample_contact
):
    # No BounceRecord ingested — the reply's auto-action stash is the fallback.
    reply = await _make_bounce_reply(
        db_session, sample_contact,
        meta={"bounce": {"recipient": sample_contact.email, "kind": "soft", "suppressed": False}},
    )

    resp = await client.get(f"/api/v1/dialtone/inbox/{reply.id}")
    assert resp.status_code == 200, resp.text
    bounce = resp.json()["bounce"]
    assert bounce["category"] == "soft"
    # No DSN record, so no code — but the recipient still resolves from the contact.
    assert bounce["status_code"] is None
    assert bounce["diagnostic"] is None
    assert bounce["recipient"] == sample_contact.email
    assert bounce["suppressed"] is False


@pytest.mark.asyncio
async def test_bounce_record_preferred_over_meta_kind(
    client: AsyncClient, db_session: AsyncSession, sample_contact
):
    # Record says hard; stale meta says soft — the authoritative record wins.
    db_session.add(BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-{uuid.uuid4().hex[:10]}",
        recipient_email=sample_contact.email.lower(),
        bounce_kind="hard",
        status_code="5.0.0",
        diagnostic="permanent failure",
        processed_at=_now(),
    ))
    reply = await _make_bounce_reply(
        db_session, sample_contact, meta={"bounce": {"kind": "soft"}},
    )

    resp = await client.get(f"/api/v1/dialtone/inbox/{reply.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["bounce"]["category"] == "hard"


@pytest.mark.asyncio
async def test_non_bounce_reply_has_no_breakdown(
    client: AsyncClient, db_session: AsyncSession, sample_contact
):
    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=sample_contact.id,
        company_id=sample_contact.company_id,
        from_email=sample_contact.email,
        subject="Re: your email",
        body_text="Sure, let's talk.",
        received_at=_now(),
        reply_classification="interested",
        classification_confidence=0.9,
    )
    db_session.add(reply)
    await db_session.flush()

    resp = await client.get(f"/api/v1/dialtone/inbox/{reply.id}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["bounce"] is None


@pytest.mark.asyncio
async def test_bounce_breakdown_uses_latest_record(
    client: AsyncClient, db_session: AsyncSession, sample_contact
):
    # An older soft bounce and a newer hard bounce for the same address — the
    # most recent (hard) is what the operator should see.
    db_session.add(BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-old-{uuid.uuid4().hex[:8]}",
        recipient_email=sample_contact.email.lower(),
        bounce_kind="soft",
        status_code="4.2.2",
        diagnostic="mailbox full",
        processed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    ))
    db_session.add(BounceRecord(
        id=uuid.uuid4(),
        gmail_message_id=f"msg-new-{uuid.uuid4().hex[:8]}",
        recipient_email=sample_contact.email.lower(),
        bounce_kind="hard",
        status_code="5.1.1",
        diagnostic="no such user",
        processed_at=_now(),
    ))
    reply = await _make_bounce_reply(db_session, sample_contact)

    resp = await client.get(f"/api/v1/dialtone/inbox/{reply.id}")
    assert resp.status_code == 200, resp.text
    bounce = resp.json()["bounce"]
    assert bounce["category"] == "hard"
    assert bounce["status_code"] == "5.1.1"
