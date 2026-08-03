"""Unified, user-attributed communication history for a contact/client.

Aggregates every touchpoint with a contact — outbound emails, inbound replies,
calls, and logged interactions — into a single timeline that records *who*
(which user) communicated *what*, *when*, and *via which email account*.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call import CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import NON_WIRE_STATUS_VALUES, EmailSend
from app.models.gmail_account import GmailAccount
from app.models.interaction import Interaction
from app.models.user import User


async def _load_user_map(
    db: AsyncSession, user_ids: set[uuid.UUID]
) -> dict[uuid.UUID, User]:
    """Load User rows for the given ids into an {id: User} map."""
    ids = {u for u in user_ids if u is not None}
    if not ids:
        return {}
    rows = (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all()
    return {u.id: u for u in rows}


async def _load_gmail_map(
    db: AsyncSession,
) -> dict[uuid.UUID, GmailAccount]:
    """Load all Gmail accounts into an {id: account} map (email + owner)."""
    rows = (await db.execute(select(GmailAccount))).scalars().all()
    return {a.id: a for a in rows}


async def get_contact_communications(
    db: AsyncSession,
    contact_id: uuid.UUID,
    *,
    user_id: Optional[uuid.UUID] = None,
) -> Optional[dict]:
    """Build the full communication history for a contact.

    When *user_id* is given, only communications attributed to that user are
    returned (e.g. an agent viewing their own history with a client).

    Returns ``None`` if the contact does not exist.
    """
    contact = (
        await db.execute(select(Contact).where(Contact.id == contact_id))
    ).scalar_one_or_none()
    if contact is None:
        return None

    company_name = None
    if contact.company_id:
        company_name = (
            await db.execute(select(Company.name).where(Company.id == contact.company_id))
        ).scalar_one_or_none()

    gmail_map = await _load_gmail_map(db)

    entries: list[dict] = []
    involved_users: set[uuid.UUID] = set()

    # --- Outbound emails -----------------------------------------------------
    sends = (
        await db.execute(
            select(EmailSend).where(
                EmailSend.contact_id == contact_id,
                EmailSend.status.notin_(NON_WIRE_STATUS_VALUES),
            )
        )
    ).scalars().all()
    for s in sends:
        acct = gmail_map.get(s.sender_account_id) if s.sender_account_id else None
        # Attribute to the recorded sender, else the sending mailbox's owner.
        uid = s.user_id or (acct.user_id if acct else None)
        involved_users.add(uid)
        entries.append({
            "id": str(s.id),
            "type": "email_sent",
            "direction": "outbound",
            "timestamp": s.sent_at,
            "subject": s.subject,
            "summary": None,
            "status": s.status,
            "user_id": uid,
            "email_account_id": s.sender_account_id,
            "email_account": (acct.email if acct else s.from_email),
            "contact_id": contact_id,
            "company_id": contact.company_id,
        })

    # --- Inbound replies -----------------------------------------------------
    # Match by contact link OR by the contact's email address (unlinked replies).
    reply_filters = [EmailReply.contact_id == contact_id]
    if contact.email:
        reply_filters.append(func.lower(EmailReply.from_email) == contact.email.lower())
    replies = (
        await db.execute(select(EmailReply).where(or_(*reply_filters)))
    ).scalars().all()
    for r in replies:
        acct = gmail_map.get(r.gmail_account_id) if r.gmail_account_id else None
        uid = acct.user_id if acct else None
        involved_users.add(uid)
        entries.append({
            "id": str(r.id),
            "type": "email_received",
            "direction": "inbound",
            "timestamp": r.received_at,
            "subject": r.subject,
            "summary": r.snippet,
            "status": "received",
            "user_id": uid,
            "email_account_id": r.gmail_account_id,
            "email_account": (acct.email if acct else r.to_email),
            "contact_id": contact_id,
            "company_id": contact.company_id,
        })

    # --- Calls ---------------------------------------------------------------
    calls = (
        await db.execute(select(CallTask).where(CallTask.contact_id == contact_id))
    ).scalars().all()
    for c in calls:
        involved_users.add(c.user_id)
        entries.append({
            "id": str(c.id),
            "type": "call",
            "direction": "outbound",
            "timestamp": c.completed_at or c.scheduled_at or c.created_at,
            "subject": f"Call ({c.status})",
            "summary": c.notes,
            "status": c.outcome or c.status,
            "user_id": c.user_id,
            "email_account_id": None,
            "email_account": None,
            "contact_id": contact_id,
            "company_id": c.company_id,
        })

    # --- Logged interactions -------------------------------------------------
    interactions = (
        await db.execute(select(Interaction).where(Interaction.contact_id == contact_id))
    ).scalars().all()
    for it in interactions:
        involved_users.add(it.user_id)
        entries.append({
            "id": str(it.id),
            "type": "interaction",
            "direction": "internal",
            "timestamp": it.occurred_at or it.created_at,
            "subject": it.subject or it.interaction_type,
            "summary": it.content or it.notes,
            "status": it.interaction_type,
            "user_id": it.user_id,
            "email_account_id": None,
            "email_account": None,
            "contact_id": contact_id,
            "company_id": it.company_id,
        })

    # Optionally scope to a single user.
    if user_id is not None:
        entries = [e for e in entries if e["user_id"] == user_id]

    # Resolve user names/emails.
    user_map = await _load_user_map(db, involved_users)
    for e in entries:
        u = user_map.get(e["user_id"]) if e["user_id"] else None
        e["user_name"] = u.name if u else None
        e["user_email"] = u.email if u else None

    # Newest first; robust to naive/aware datetimes and missing timestamps.
    def _sort_key(e: dict) -> float:
        ts: Optional[datetime] = e["timestamp"]
        if ts is None:
            return float("-inf")
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()

    entries.sort(key=_sort_key, reverse=True)

    return {
        "contact_id": contact_id,
        "contact_name": contact.full_name,
        "company_id": contact.company_id,
        "company_name": company_name,
        "total": len(entries),
        "communications": entries,
    }
