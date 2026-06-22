"""Unified outreach email dispatch: Gmail-as-user, with SES fallback.

Supports multiple sending accounts with round-robin rotation. Picks sender:

    1. An explicitly requested connected Gmail account (``sender_account_id``)
    2. Round-robin rotation across all ``sending_enabled`` + connected accounts
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

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.services.email_sender import EmailSender
from app.services.gmail_sender import GmailSender
from app.services.smtp_sender import SmtpSender

logger = logging.getLogger(__name__)


def _make_sender(account):
    """Return the transport sender object for a Gmail or SMTP account."""
    if isinstance(account, SmtpAccount):
        return SmtpSender(account)
    return GmailSender(account)


def _transport_for(account) -> str:
    return "smtp" if isinstance(account, SmtpAccount) else "gmail"


def _rotation_key(account):
    """Sort key for round-robin: never-used first, then least-recently-used.

    Avoids comparing ``None`` directly (Python 3 forbids it) by sorting on a
    has-been-used flag first, then falling back to ``connected_at``.
    """
    last_used = account.last_used_at
    connected = account.connected_at
    return (0 if last_used is None else 1, last_used or connected, connected)


@dataclass
class SenderInfo:
    """Describes who outreach mail will be sent as."""

    transport: str  # "gmail" | "smtp" | "ses"
    from_email: str
    display_name: Optional[str] = None
    account_id: Optional[uuid.UUID] = None
    connected: bool = False


async def get_active_account(db: AsyncSession):
    """The primary sender across all account types (Gmail preferred, then SMTP)."""
    gmail = (
        await db.execute(
            select(GmailAccount).where(
                GmailAccount.is_active == True,  # noqa: E712
                GmailAccount.status == "connected",
            )
        )
    ).scalar_one_or_none()
    if gmail is not None:
        return gmail
    return (
        await db.execute(
            select(SmtpAccount).where(
                SmtpAccount.is_active == True,  # noqa: E712
                SmtpAccount.status == "connected",
            )
        )
    ).scalar_one_or_none()


async def get_account(db: AsyncSession, account_id: uuid.UUID):
    """Look up a sender by id across both Gmail and SMTP accounts."""
    gmail = (
        await db.execute(select(GmailAccount).where(GmailAccount.id == account_id))
    ).scalar_one_or_none()
    if gmail is not None:
        return gmail
    return (
        await db.execute(select(SmtpAccount).where(SmtpAccount.id == account_id))
    ).scalar_one_or_none()


async def get_sending_accounts(db: AsyncSession) -> list:
    """Return all sending-enabled, connected senders (Gmail + SMTP)."""
    gmail = (
        await db.execute(
            select(GmailAccount).where(
                GmailAccount.sending_enabled == True,  # noqa: E712
                GmailAccount.status == "connected",
            )
        )
    ).scalars().all()
    smtp = (
        await db.execute(
            select(SmtpAccount).where(
                SmtpAccount.sending_enabled == True,  # noqa: E712
                SmtpAccount.status == "connected",
            )
        )
    ).scalars().all()
    return list(gmail) + list(smtp)


async def get_next_rotation_account(db: AsyncSession):
    """Pick the least-recently-used sending-enabled sender for round-robin.

    Considers every sending-enabled, connected account across Gmail and SMTP so
    volume is spread evenly regardless of transport. Brand-new accounts (never
    used) are picked first.
    """
    accounts = await get_sending_accounts(db)
    if not accounts:
        return None
    return min(accounts, key=_rotation_key)


async def resolve_sender_info(
    db: AsyncSession, sender_account_id: Optional[uuid.UUID] = None
) -> SenderInfo:
    """Describe the sender that would be used for an outreach send right now."""
    account = None
    if sender_account_id is not None:
        account = await get_account(db, sender_account_id)

    # Try rotation (least recently used sending-enabled account)
    if account is None:
        account = await get_next_rotation_account(db)

    # Fallback to active account
    if account is None:
        account = await get_active_account(db)

    if account is not None and account.status == "connected":
        return SenderInfo(
            transport=_transport_for(account),
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
    settings = get_settings()
    serializer = URLSafeTimedSerializer(settings.secret_key or "landline-unsubscribe-secret")
    token = serializer.dumps(contact_email, salt="unsubscribe")
    unsubscribe_url = f"https://landline-api.apprendtechnologies.com:8443/api/v1/track/unsubscribe?token={token}"
    return (
        "\n\n---\n"
        "Apprend Technologies | Manufacturing Software Solutions\n"
        f"To unsubscribe, reply with UNSUBSCRIBE or click: "
        f"{unsubscribe_url}\n"
        "Apprend Technologies, Toronto, ON, Canada"
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

    # Use rotation: pick least-recently-used sending-enabled account
    if account is None:
        account = await get_next_rotation_account(db)

    # Last resort: the primary active account (even if sending_enabled is False)
    if account is None:
        account = await get_active_account(db)

    if include_unsubscribe:
        footer = _unsubscribe_footer(to_email)
        body_html = body_html + footer.replace("\n", "<br>")
        if body_text:
            body_text = body_text + footer

    if account is not None:
        sender = _make_sender(account)
        result = await asyncio.to_thread(
            sender.send_email,
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            reply_to=reply_to,
        )
        # Update usage tracking
        account.last_used_at = datetime.now(timezone.utc)
        account.daily_send_count = (account.daily_send_count or 0) + 1
        if result.get("status") == "error":
            account.status = "error"
        return result

    # Fallback: Amazon SES
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
