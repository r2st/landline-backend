"""Tests for the per-client communication history (user-attributed timeline)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallTask
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.interaction import Interaction

CONTACTS = "/api/v1/contacts"


async def _gmail_account(db: AsyncSession, email: str, owner_id) -> GmailAccount:
    acct = GmailAccount(
        id=uuid.uuid4(),
        user_id=owner_id,
        email=email,
        google_sub=f"sub-{email}",
        display_name=email.split("@")[0],
        refresh_token_encrypted="not-a-real-token",
    )
    db.add(acct)
    await db.flush()
    return acct


async def _seed_history(db, contact, company, user, acct):
    """Create one of each communication type tied to *contact*."""
    now = datetime.now(timezone.utc)
    db.add(EmailSend(
        id=uuid.uuid4(), contact_id=contact.id, user_id=user.id,
        sender_account_id=acct.id, from_email=acct.email, to_email=contact.email,
        subject="Intro from us", status="sent", gmail_message_id="cm-send-1",
        sent_at=now - timedelta(days=3),
    ))
    db.add(EmailReply(
        id=uuid.uuid4(), contact_id=contact.id, gmail_account_id=acct.id,
        from_email=contact.email, to_email=acct.email, subject="Re: Intro from us",
        snippet="Sounds good", gmail_message_id="cm-reply-1",
        received_at=now - timedelta(days=2), is_read=False,
    ))
    db.add(CallTask(
        id=uuid.uuid4(), company_id=company.id, contact_id=contact.id, user_id=user.id,
        status="completed", priority="warm", outcome="connected",
        notes="Great discovery call", completed_at=now - timedelta(days=1),
    ))
    db.add(Interaction(
        id=uuid.uuid4(), company_id=company.id, contact_id=contact.id, user_id=user.id,
        interaction_type="meeting", subject="Met at trade show",
        content="In-person intro", occurred_at=now,
    ))
    await db.commit()


@pytest.mark.asyncio
async def test_communications_unifies_all_channels(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _seed_history(db_session, sample_contact, sample_company, agent_user, acct)

    resp = await client.get(f"{CONTACTS}/{sample_contact.id}/communications")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 4
    types = {e["type"] for e in data["communications"]}
    assert types == {"email_sent", "email_received", "call", "interaction"}
    # Newest first — the interaction (occurred now) leads.
    assert data["communications"][0]["type"] == "interaction"


@pytest.mark.asyncio
async def test_communications_attributes_user_and_account(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _seed_history(db_session, sample_contact, sample_company, agent_user, acct)

    resp = await client.get(f"{CONTACTS}/{sample_contact.id}/communications")
    data = resp.json()

    sent = next(e for e in data["communications"] if e["type"] == "email_sent")
    assert sent["user_id"] == str(agent_user.id)
    assert sent["user_name"] == agent_user.name
    assert sent["user_email"] == agent_user.email
    assert sent["email_account"] == "sales@company.com"
    assert sent["direction"] == "outbound"

    received = next(e for e in data["communications"] if e["type"] == "email_received")
    assert received["direction"] == "inbound"
    # Inbound mail is attributed to the mailbox owner.
    assert received["user_id"] == str(agent_user.id)
    assert received["email_account"] == "sales@company.com"


@pytest.mark.asyncio
async def test_communications_mine_only_filters_by_user(
    client: AsyncClient, db_session, sample_company, sample_contact,
    agent_user, admin_user, auth_headers,
):
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _seed_history(db_session, sample_contact, sample_company, agent_user, acct)

    # admin has no communications with this contact → mine_only returns none.
    resp = await client.get(
        f"{CONTACTS}/{sample_contact.id}/communications?mine_only=true",
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # agent sees their four.
    resp2 = await client.get(
        f"{CONTACTS}/{sample_contact.id}/communications?mine_only=true",
        headers=auth_headers(agent_user),
    )
    assert resp2.json()["total"] == 4


@pytest.mark.asyncio
async def test_communications_unknown_contact_404(client: AsyncClient):
    resp = await client.get(f"{CONTACTS}/{uuid.uuid4()}/communications")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_email_compose_attributes_sender(
    client: AsyncClient, db_session, sample_contact, agent_user, auth_headers
):
    """A send composed by an authenticated user records that user as sender.

    (The actual delivery is skipped in tests — no SES/Gmail configured — but the
    ``email_sends`` row is still created and attributed.)"""
    from sqlalchemy import select
    from app.models.email_template import EmailSend, EmailTemplate

    template = EmailTemplate(
        id=uuid.uuid4(), name="Default", subject="Hello {{first_name}}",
        body="<p>Hi {{first_name}}</p>", is_default=True,
    )
    db_session.add(template)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/email-compose",
        json={"template_id": str(template.id), "contact_ids": [str(sample_contact.id)]},
        headers=auth_headers(agent_user),
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(EmailSend).where(EmailSend.contact_id == sample_contact.id)
        )
    ).scalars().first()
    assert row is not None
    assert row.user_id == agent_user.id
