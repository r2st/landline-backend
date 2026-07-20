"""Smart-reply router: manual sweep trigger + operator approve-and-send.

The smart_reply service existed but had no HTTP entry point, and the Dialtone
inbox had no way to dispatch a suggested draft. Cover both new endpoints.
"""

import uuid
from datetime import datetime, timezone

import pytest

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.services import email_dispatch
from app.services.smart_reply import SmartReplyService


async def _seed_reply(db, **reply_kw) -> EmailReply:
    company = Company(id=uuid.uuid4(), name="Prospect Co", country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Pat", last_name="Prospect",
        email="pat@prospect.co", source="test",
    )
    db.add(contact)
    send = EmailSend(
        id=uuid.uuid4(), from_email="rep@apprend.tech", to_email="pat@prospect.co",
        subject="Intro", status="replied", sent_at=datetime.now(timezone.utc),
        contact_id=contact.id, gmail_thread_id="thread-1",
        provider_message_id="<send-1@apprend.tech>",
    )
    db.add(send)
    reply = EmailReply(
        id=uuid.uuid4(),
        email_send_id=send.id,
        contact_id=contact.id,
        company_id=company.id,
        from_email="pat@prospect.co",
        subject="Re: Intro",
        body_text="Sounds interesting, tell me more",
        received_at=datetime.now(timezone.utc),
        reply_classification="interested",
        gmail_thread_id="thread-1",
        gmail_message_id="<reply-1@prospect.co>",
        **reply_kw,
    )
    db.add(reply)
    await db.commit()
    return reply


@pytest.mark.asyncio
async def test_approve_and_send_dispatches_and_marks_handled(client, db_session, monkeypatch):
    reply = await _seed_reply(db_session)

    captured = {}

    async def fake_send(db, **kwargs):
        captured.update(kwargs)
        return {
            "status": "sent", "transport": "gmail", "sender": "rep@apprend.tech",
            "from_email": "rep@apprend.tech",
        }

    monkeypatch.setattr(email_dispatch, "send_outreach_email", fake_send)

    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/approve-send",
        json={"body": "Happy to — here's a quick overview..."},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    assert data["to_email"] == "pat@prospect.co"

    # Threading context flowed through to the dispatcher.
    assert captured["to_email"] == "pat@prospect.co"
    assert captured["thread_id"] == "thread-1"
    assert captured["subject"] == "Re: Re: Intro" or captured["subject"].startswith("Re:")
    assert captured["include_unsubscribe"] is False

    # Reply marked read + handled.
    await db_session.refresh(reply)
    assert reply.is_read is True
    assert reply.handled_action == "approved_reply_sent"


@pytest.mark.asyncio
async def test_approve_and_send_respects_kill_switch(client, db_session, monkeypatch):
    """A halted dispatch must not record a send or mark the reply handled."""
    reply = await _seed_reply(db_session)

    async def fake_send(db, **kwargs):
        return {"status": "halted", "reason": "kill switch engaged", "transport": "none"}

    monkeypatch.setattr(email_dispatch, "send_outreach_email", fake_send)

    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/approve-send",
        json={"body": "reply body"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "halted"

    await db_session.refresh(reply)
    assert reply.handled_action is None


@pytest.mark.asyncio
async def test_approve_and_send_custom_subject(client, db_session, monkeypatch):
    reply = await _seed_reply(db_session)
    captured = {}

    async def fake_send(db, **kwargs):
        captured.update(kwargs)
        return {"status": "sent", "transport": "gmail", "sender": "rep@apprend.tech"}

    monkeypatch.setattr(email_dispatch, "send_outreach_email", fake_send)
    resp = await client.post(
        f"/api/v1/dialtone/inbox/{reply.id}/approve-send",
        json={"subject": "Following up", "body": "hi"},
    )
    assert resp.status_code == 200
    assert captured["subject"] == "Following up"


@pytest.mark.asyncio
async def test_approve_and_send_unknown_reply_404(client):
    resp = await client.post(
        f"/api/v1/dialtone/inbox/{uuid.uuid4()}/approve-send",
        json={"body": "hi"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_process_endpoint_returns_summary(client, monkeypatch):
    async def fake_process(self, db):
        return [
            {"from_email": "a@x.com", "auto_sent": True},
            {"from_email": "b@y.com", "auto_sent": False},
        ]

    monkeypatch.setattr(SmartReplyService, "process_replies", fake_process)

    resp = await client.post("/api/v1/dialtone/smart-reply/process")
    assert resp.status_code == 200
    data = resp.json()
    assert data["processed"] == 2
    assert data["auto_sent"] == 1
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_process_endpoint_requires_auth(unauth_client):
    resp = await unauth_client.post("/api/v1/dialtone/smart-reply/process")
    assert resp.status_code == 401
