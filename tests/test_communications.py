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


# --- The other send table -----------------------------------------------------
#
# ``get_contact_communications`` read ``EmailSend`` alone. The sequence engine
# writes ``OutreachEvent(event_type='sent')`` and no ``EmailSend`` row, and that
# is where the bulk of outreach volume goes — so this "unified" timeline showed
# a cadenced prospect's replies with nothing they were replying to. Same
# half-migrated read the inbox and the dashboard tiles each had.


async def _sequence_send(
    db, contact, *, acct, subject: str, days_ago: int, sender_in_metadata=True
):
    """One cadence step, recorded the way ``send_sequence_step`` records it."""
    from app.models.outreach import OutreachEnrollment, OutreachEvent
    from app.models.sequence import OutreachSequence

    seq = OutreachSequence(id=uuid.uuid4(), name=f"Seq {subject}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        sender_account_id=None if sender_in_metadata else acct.id,
    )
    db.add(enr)
    await db.flush()
    meta = {"subject": subject, "to_email": contact.email}
    if sender_in_metadata:
        meta["sender_account_id"] = str(acct.id)
        meta["from_email"] = acct.email
    db.add(OutreachEvent(
        id=uuid.uuid4(),
        enrollment_id=enr.id,
        step_number=1,
        event_type="sent",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        event_metadata=meta,
    ))
    await db.commit()
    return enr


@pytest.mark.asyncio
async def test_communications_includes_sequence_sends(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    """A cadence step appears in the timeline, attributed and subject-carrying.

    Mutation control: reading ``EmailSend`` alone returns 4 entries and no
    "Step 1 — quick question" anywhere.
    """
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _seed_history(db_session, sample_contact, sample_company, agent_user, acct)
    await _sequence_send(
        db_session, sample_contact, acct=acct,
        subject="Step 1 — quick question", days_ago=4,
    )

    data = (await client.get(f"{CONTACTS}/{sample_contact.id}/communications")).json()
    assert data["total"] == 5

    step = next(
        e for e in data["communications"]
        if e["subject"] == "Step 1 — quick question"
    )
    assert step["type"] == "email_sent"
    assert step["direction"] == "outbound"
    assert step["status"] == "sent"
    assert step["email_account"] == "sales@company.com"
    # No user of its own — a cadence step is sent by the system, so it is
    # attributed to whoever owns the mailbox it left from.
    assert step["user_id"] == str(agent_user.id)
    assert step["user_name"] == agent_user.name


@pytest.mark.asyncio
async def test_sequence_send_sorts_into_the_timeline_by_its_own_timestamp(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    """The step predates the manual send, so it must sort below it — not append.

    A union that simply concatenated would still return the right *count* while
    putting the outbound step after the reply it prompted.
    """
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _seed_history(db_session, sample_contact, sample_company, agent_user, acct)
    await _sequence_send(
        db_session, sample_contact, acct=acct, subject="Oldest touch", days_ago=10
    )

    data = (await client.get(f"{CONTACTS}/{sample_contact.id}/communications")).json()
    assert data["communications"][-1]["subject"] == "Oldest touch"


@pytest.mark.asyncio
async def test_sequence_send_falls_back_to_the_enrollment_mailbox(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    """Events recorded before the dispatcher merged its result carry no sender.

    Those rows are in the database now, and dropping their attribution would
    show a send from nobody rather than from the mailbox it went out on.
    """
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _sequence_send(
        db_session, sample_contact, acct=acct, subject="Legacy step",
        days_ago=2, sender_in_metadata=False,
    )

    data = (await client.get(f"{CONTACTS}/{sample_contact.id}/communications")).json()
    step = next(e for e in data["communications"] if e["subject"] == "Legacy step")
    assert step["email_account"] == "sales@company.com"
    assert step["user_id"] == str(agent_user.id)


@pytest.mark.asyncio
async def test_sequence_sends_for_another_contact_do_not_leak_in(
    client: AsyncClient, db_session, sample_company, sample_contact, agent_user
):
    """The union is joined through the enrollment, so it must filter on it."""
    from app.models.contact import Contact

    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    other = Contact(
        id=uuid.uuid4(), company_id=sample_company.id,
        first_name="Someone", last_name="Else", email="else@example.com",
    )
    db_session.add(other)
    await db_session.flush()
    await _sequence_send(
        db_session, other, acct=acct, subject="Not yours", days_ago=1
    )
    await _sequence_send(
        db_session, sample_contact, acct=acct, subject="Yours", days_ago=1
    )

    data = (await client.get(f"{CONTACTS}/{sample_contact.id}/communications")).json()
    subjects = {e["subject"] for e in data["communications"]}
    assert "Yours" in subjects
    assert "Not yours" not in subjects


@pytest.mark.asyncio
async def test_mine_only_still_sees_a_sequence_step_from_your_mailbox(
    client: AsyncClient, db_session, sample_company, sample_contact,
    agent_user, admin_user, auth_headers,
):
    """``mine_only`` filters on the attributed user, which the union supplies."""
    acct = await _gmail_account(db_session, "sales@company.com", agent_user.id)
    await _sequence_send(
        db_session, sample_contact, acct=acct, subject="Mine", days_ago=1
    )

    mine = await client.get(
        f"{CONTACTS}/{sample_contact.id}/communications?mine_only=true",
        headers=auth_headers(agent_user),
    )
    assert mine.json()["total"] == 1

    theirs = await client.get(
        f"{CONTACTS}/{sample_contact.id}/communications?mine_only=true",
        headers=auth_headers(admin_user),
    )
    assert theirs.json()["total"] == 0
