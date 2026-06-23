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
import re
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
from app.models.email_signature import EmailSignature
from app.services.email_sender import EmailSender
from app.services.gmail_sender import GmailSender
from app.services.smtp_sender import SmtpSender

logger = logging.getLogger(__name__)


def _inject_inline_styles(html: str) -> str:
    """Add inline styles to common HTML tags for email client compatibility."""
    replacements = [
        (r'<p(?=[\s>])', '<p style="margin:0 0 16px 0"'),
        (r'<ul(?=[\s>])', '<ul style="margin:0 0 16px 0;padding-left:24px"'),
        (r'<ol(?=[\s>])', '<ol style="margin:0 0 16px 0;padding-left:24px"'),
        (r'<li(?=[\s>])', '<li style="margin:0 0 8px 0"'),
        (r'<strong(?=[\s>])', '<strong style="font-weight:bold"'),
        (r'<b(?=[\s>])', '<b style="font-weight:bold"'),
        (r'<em(?=[\s>])', '<em style="font-style:italic"'),
        (r'<i(?=[\s>])', '<i style="font-style:italic"'),
        (r'<a(?=[\s>])', '<a style="color:#2563eb;text-decoration:underline"'),
        (r'<h2(?=[\s>])', '<h2 style="font-size:20px;font-weight:bold;margin:24px 0 12px 0"'),
        (r'<h3(?=[\s>])', '<h3 style="font-size:17px;font-weight:bold;margin:20px 0 10px 0"'),
        (r'<hr(?=[\s>/])', '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0"'),
        (r'<blockquote(?=[\s>])', '<blockquote style="border-left:3px solid #d1d5db;padding-left:16px;margin:0 0 16px 0;color:#6b7280"'),
    ]
    for pattern, replacement in replacements:
        html = re.sub(pattern, replacement, html)
    return html


def wrap_html_email(body_html: str) -> str:
    """Wrap an HTML fragment in a complete email-safe HTML document with inline styles."""
    styled = _inject_inline_styles(body_html)
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '</head>'
        '<body style="margin:0;padding:0;background-color:#f9fafb">'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb">'
        '<tr><td align="center" style="padding:24px 16px">'
        '<table width="600" cellpadding="0" cellspacing="0" '
        'style="max-width:600px;width:100%;background-color:#ffffff;border-radius:8px;overflow:hidden">'
        '<tr><td style="padding:32px 40px;font-family:Arial,Helvetica,sans-serif;'
        'font-size:15px;line-height:1.6;color:#333333">'
        f'{styled}'
        '</td></tr>'
        '</table>'
        '</td></tr>'
        '</table>'
        '</body></html>'
    )


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


def _unsubscribe_footer_text(contact_email: str) -> str:
    """Plain-text unsubscribe footer for the text/plain part."""
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


def _unsubscribe_footer_html(contact_email: str) -> str:
    """HTML unsubscribe footer with proper inline styles."""
    settings = get_settings()
    serializer = URLSafeTimedSerializer(settings.secret_key or "landline-unsubscribe-secret")
    token = serializer.dumps(contact_email, salt="unsubscribe")
    unsubscribe_url = f"https://landline-api.apprendtechnologies.com:8443/api/v1/track/unsubscribe?token={token}"
    return (
        '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">'
        '<p style="margin:0 0 4px 0;font-size:12px;color:#9ca3af">'
        'Apprend Technologies | Manufacturing Software Solutions</p>'
        '<p style="margin:0;font-size:12px;color:#9ca3af">'
        'To unsubscribe, reply with UNSUBSCRIBE or '
        f'<a href="{unsubscribe_url}" style="color:#9ca3af">click here</a></p>'
        '<p style="margin:0;font-size:11px;color:#9ca3af">'
        'Apprend Technologies, Toronto, ON, Canada</p>'
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
    signature_id: Optional[uuid.UUID] = None,
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

    # Append email signature (specific or default)
    signature = None
    if signature_id is not None:
        result = await db.execute(
            select(EmailSignature).where(EmailSignature.id == signature_id)
        )
        signature = result.scalar_one_or_none()
    if signature is None:
        result = await db.execute(
            select(EmailSignature).where(EmailSignature.is_default == True)  # noqa: E712
        )
        signature = result.scalar_one_or_none()
    if signature is not None and signature.content_html:
        body_html = body_html + "<br><br>" + signature.content_html

    if include_unsubscribe:
        footer_html = _unsubscribe_footer_html(to_email)
        body_html = body_html + footer_html
        if body_text:
            body_text = body_text + _unsubscribe_footer_text(to_email)

    # Wrap in a complete HTML email document with inline styles
    body_html = wrap_html_email(body_html)

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
        # Update usage tracking. Roll the per-account daily counter over at the
        # UTC date boundary so ``daily_send_count`` reflects *today's* sends
        # rather than growing without bound.
        now = datetime.now(timezone.utc)
        reset_at = account.daily_count_reset_at
        if reset_at is not None and reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if reset_at is None or reset_at.date() < now.date():
            account.daily_send_count = 0
            account.daily_count_reset_at = now
        account.last_used_at = now
        # Only count sends that actually went out (not skipped/errored) toward
        # the daily total.
        if result.get("status") == "sent":
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
