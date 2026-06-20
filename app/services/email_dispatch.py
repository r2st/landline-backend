"""Unified outreach email dispatch: Gmail-as-user, with SES fallback.

A single entry point — :func:`send_outreach_email` — used by both the compose
endpoint and the Celery sequence task. It picks the sender in this order:

    1. An explicitly requested connected Gmail account (``sender_account_id``)
    2. The currently active connected Gmail account
    3. Amazon SES (legacy fallback, used when no Gmail account is connected)

The CAN-SPAM unsubscribe footer is appended here so every transport sends the
same compliant body.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.gmail_account import GmailAccount
from app.services.email_sender import EmailSender
from app.services.gmail_sender import GmailSender

logger = logging.getLogger(__name__)


@dataclass
class SenderInfo:
    """Describes who outreach mail will be sent as."""

    transport: str  # "gmail" | "ses"
    from_email: str
    display_name: Optional[str] = None
    account_id: Optional[uuid.UUID] = None
    connected: bool = False  # True when a Gmail account is the sender


async def get_active_account(db: AsyncSession) -> Optional[GmailAccount]:
    result = await db.execute(
        select(GmailAccount).where(
            GmailAccount.is_active == True,  # noqa: E712
            GmailAccount.status == "connected",
        )
    )
    return result.scalar_one_or_none()


async def get_account(db: AsyncSession, account_id: uuid.UUID) -> Optional[GmailAccount]:
    result = await db.execute(
        select(GmailAccount).where(GmailAccount.id == account_id)
    )
    return result.scalar_one_or_none()


async def resolve_sender_info(
    db: AsyncSession, sender_account_id: Optional[uuid.UUID] = None
) -> SenderInfo:
    """Describe the sender that would be used for an outreach send right now."""
    account = None
    if sender_account_id is not None:
        account = await get_account(db, sender_account_id)
    if account is None:
        account = await get_active_account(db)

    if account is not None and account.status == "connected":
        return SenderInfo(
            transport="gmail",
            from_email=account.email,
            display_name=account.display_name,
            account_id=account.id,
            connected=True,
        )
    return SenderInfo(
        transport="ses",
        from_email=get_settings().ses_sender_email,
        connected=False,
    )


def _unsubscribe_footer(contact_email: str) -> str:
    return (
        "\n\n---\n"
        "Apprend Technologies | Manufacturing Software Solutions\n"
        f"To unsubscribe, reply with 'UNSUBSCRIBE' or click: "
        f"https://landline.apprendtechnologies.com/unsubscribe?email={contact_email}\n"
        "Apprend Technologies, 123 Innovation Drive, Suite 400, Toronto, ON M5V 1A1, Canada"
    )


async def send_outreach_email(
    db: AsyncSession,
    to_email: str,
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,
    reply_to: Optional[str] = None,
    sender_account_id: Optional[uuid.UUID] = None,
    include_unsubscribe: bool = True,
) -> dict:
    """Send one outreach email via the resolved sender.

    Returns a dict with at least ``status`` ("sent" | "skipped" | "error") plus
    ``transport`` and ``sender`` so callers can record how it went.
    """
    account = None
    if sender_account_id is not None:
        account = await get_account(db, sender_account_id)
        if account is None or account.status != "connected":
            account = None
    if account is None:
        account = await get_active_account(db)

    if include_unsubscribe:
        footer = _unsubscribe_footer(to_email)
        body_html = body_html + footer.replace("\n", "<br>")
        if body_text:
            body_text = body_text + footer

    if account is not None:
        sender = GmailSender(account)
        result = await asyncio.to_thread(
            sender.send_email,
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            reply_to=reply_to,
        )
        # Record usage; mark the account as errored if Google rejected us so the
        # UI can prompt a re-connect and future sends fall back to SES.
        account.last_used_at = datetime.now(timezone.utc)
        if result.get("status") == "error":
            account.status = "error"
        return result

    # Fallback: Amazon SES. The footer is already appended above.
    ses = EmailSender()
    result = await asyncio.to_thread(
        ses.send_email,
        to_email=to_email,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        reply_to=reply_to,
        include_unsubscribe=False,
    )
    result.setdefault("transport", "ses")
    result.setdefault("sender", ses.sender)
    return result
