"""Unified outreach email dispatch: Gmail-as-user, with SES fallback.

Supports multiple sending accounts with round-robin rotation. Picks sender:

    1. An explicitly requested connected Gmail account (``sender_account_id``)
    2. Round-robin rotation across all ``sending_enabled`` + connected accounts
    3. Amazon SES (legacy fallback, used when no Gmail account is connected)

The CAN-SPAM unsubscribe footer is appended here so every transport sends the
same compliant body.
"""

import asyncio
import html as html_mod
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession


from app.config import get_settings
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.models.email_signature import EmailSignature
from app.services.email_sender import EmailSender
from app.services.gmail_sender import GmailSender
from app.services.smtp_sender import SmtpSender

logger = logging.getLogger(__name__)


def _html_to_plain_text(html_body: str) -> str:
    """Convert HTML email body to clean plain text.

    Strips tags, converts common HTML elements to readable text equivalents,
    and collapses excessive whitespace.  Used to generate the ``text/plain``
    MIME alternative so that the plain-text part never contains raw HTML tags
    — a well-known spam signal.
    """
    text = html_body

    # Block-level breaks
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr[^>]*>', '\n---\n', text, flags=re.IGNORECASE)

    # Links: keep the text and URL
    text = re.sub(
        r'<a\s[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
        r'\2 (\1)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Strip all remaining tags
    text = re.sub(r'<[^>]+>', '', text)

    # Decode HTML entities
    text = html_mod.unescape(text)

    # Collapse runs of whitespace (but preserve intentional double-newlines)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def _is_html(text: str) -> bool:
    """Check if text contains block-level HTML tags (is already formatted HTML)."""
    return bool(re.search(r'<(p|div|ul|ol|h[1-6]|table|br|hr)\b', text, re.IGNORECASE))


def _plain_text_to_html(text: str) -> str:
    """Convert plain text with newlines to simple HTML with <br> tags.

    This mimics how Gmail composes emails — each newline becomes a <br>,
    producing a natural email look without marketing-style paragraph spacing.
    """
    # Double newlines → paragraph break (two <br>s)
    # Single newlines → line break (one <br>)
    return text.replace('\n', '<br>\n')


def _inject_inline_styles(html: str) -> str:
    """Add inline styles to common HTML tags for email client compatibility."""
    replacements = [
        (r'<p(?=[\s>])', '<p style="margin:0 0 10px 0"'),
        (r'<ul(?=[\s>])', '<ul style="margin:0 0 10px 0;padding-left:24px"'),
        (r'<ol(?=[\s>])', '<ol style="margin:0 0 10px 0;padding-left:24px"'),
        (r'<li(?=[\s>])', '<li style="margin:0 0 4px 0"'),
        (r'<strong(?=[\s>])', '<strong style="font-weight:bold"'),
        (r'<b(?=[\s>])', '<b style="font-weight:bold"'),
        (r'<em(?=[\s>])', '<em style="font-style:italic"'),
        (r'<i(?=[\s>])', '<i style="font-style:italic"'),
        (r'<a(?=[\s>])', '<a style="color:#1a73e8;text-decoration:underline"'),
        (r'<h2(?=[\s>])', '<h2 style="font-size:18px;font-weight:bold;margin:16px 0 8px 0"'),
        (r'<h3(?=[\s>])', '<h3 style="font-size:16px;font-weight:bold;margin:14px 0 6px 0"'),
        (r'<hr(?=[\s>/])', '<hr style="border:none;border-top:1px solid #dadce0;margin:16px 0"'),
        (r'<blockquote(?=[\s>])', '<blockquote style="border-left:3px solid #dadce0;padding-left:12px;margin:0 0 10px 0;color:#5f6368"'),
    ]
    for pattern, replacement in replacements:
        html = re.sub(pattern, replacement, html)
    return html


def wrap_html_email(body_html: str) -> str:
    """Wrap email content in a simple HTML document that looks like a normal Gmail email.

    No marketing-style card layout — just clean, simple formatting that matches
    how Gmail composes and sends emails.
    """
    # Convert plain text to HTML if needed
    if not _is_html(body_html):
        body_html = _plain_text_to_html(body_html)

    styled = _inject_inline_styles(body_html)
    return (
        '<!DOCTYPE html>'
        '<html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        '</head>'
        '<body style="margin:0;padding:0">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        'line-height:1.5;color:#222222">'
        f'{styled}'
        '</div>'
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
    """The primary sender across all account types (Gmail preferred, then SMTP).

    Takes the **oldest** connected mailbox of the preferred transport, rather
    than asserting there is only one.

    Both queries used ``scalar_one_or_none``, which reads as the careful option
    and is in fact an assertion that at most one row matches — raising
    ``MultipleResultsFound`` the moment a second does. Nothing promises that
    here: ``gmail_accounts`` is unique on ``email`` and ``google_sub`` and on
    nothing else, and :func:`get_sending_accounts` directly below returns a
    *list* of connected accounts because multi-mailbox sending is the design.
    Rotation, per-account warm-up ramps and per-account sender health all exist
    to spread volume over several. So the second mailbox a tenant connected
    turned this into a 500.

    It survived because the outreach path reaches this only as a fallback, after
    :func:`get_next_rotation_account` finds nothing — which needs connected
    accounts with ``sending_enabled=False``. ``welcome_email`` and
    ``notifications`` have no rotation step and call this outright, so for them
    a second mailbox was an unconditional failure.

    Oldest-first (``connected_at``, then ``id`` to break a tie) because this
    answers "who is the established primary sender", and because an arbitrary
    winner is its own bug: the caller is choosing an outreach *identity*, and
    alternating between mailboxes at random is exactly what the rotation and
    warm-up machinery exists to do deliberately instead.
    """
    for model in (GmailAccount, SmtpAccount):
        account = (
            await db.execute(
                select(model)
                .where(
                    model.is_active == True,  # noqa: E712
                    model.status == "connected",
                )
                .order_by(model.connected_at.asc(), model.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if account is not None:
            return account
    return None


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


async def _filter_by_sender_health(db: AsyncSession, accounts: list) -> list:
    """Drop accounts that shouldn't send right now: deliverability health + warm-up.

    Excludes accounts whose reputation is ``critical`` and accounts that have
    already hit their **effective** daily cap — the lower of the reputation-
    throttled cap (feature 3.3) and the per-account **warm-up** ramp (based on
    ``connected_at`` age). A brand-new mailbox is therefore held to its warm-up
    volume even before any SenderHealth assessment exists. Fail-open: accounts
    with non-numeric send counts (mocks) or no data are never excluded.
    """
    from app.models.sender_health import SenderHealth  # local import avoids a cycle
    from app.services import warmup as warmup_svc
    from app.services.send_limiter import get_send_limiter

    if not accounts:
        return accounts
    ids = [a.id for a in accounts]
    rows = (
        await db.execute(select(SenderHealth).where(SenderHealth.account_id.in_(ids)))
    ).scalars().all()
    health = {(r.account_type, r.account_id): r for r in rows}

    base_limit = get_settings().max_emails_per_day
    limiter = get_send_limiter()
    keep = []
    for a in accounts:
        h = health.get((_transport_for(a), a.id))
        if h is not None and h.status == "critical":
            continue
        throttle = h.throttle_factor if h is not None else 1.0
        effective = max(1, round(base_limit * throttle))
        # Persisted warm-up plan (Feature 1) is authoritative; else config ramp.
        warmup = await warmup_svc.plan_daily_limit(db, a)
        if warmup is None:
            warmup = limiter.account_warmup_limit(a)
        if warmup is not None:
            effective = min(effective, warmup)
        try:
            if int(a.daily_send_count or 0) >= effective:
                continue
        except (TypeError, ValueError):
            # Non-numeric send count (e.g. a bare mock) → don't exclude.
            pass
        keep.append(a)
    return keep


async def get_next_rotation_account(db: AsyncSession):
    """Pick the least-recently-used sending-enabled sender for round-robin.

    Considers every sending-enabled, connected account across Gmail and SMTP so
    volume is spread evenly regardless of transport. Brand-new accounts (never
    used) are picked first. Accounts flagged unhealthy by the deliverability
    optimizer (feature 3.3) are skipped when healthier accounts are available.
    """
    accounts = await get_sending_accounts(db)
    if not accounts:
        return None
    # Prefer healthy accounts; never halt sending entirely if all are throttled.
    pool = await _filter_by_sender_health(db, accounts) or accounts
    return min(pool, key=_rotation_key)


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
    # No Gmail/SMTP account connected yet. Report the intended primary sender
    # (apprend.tech) so the deliverability + test-send endpoints check the domain
    # outreach will actually send from once OAuth is connected. Fall back to the
    # SES identity only if no primary sender is configured.
    settings = get_settings()
    return SenderInfo(
        transport="ses",
        from_email=settings.primary_sender_email or settings.ses_sender_email,
        connected=False,
    )


def _make_unsubscribe_url(contact_email: str) -> str:
    """Generate the signed unsubscribe URL for a contact.

    The token comes from :mod:`app.services.opt_out`, which owns both minting and
    verification. This used to sign with ``secret_key or
    "landline-unsubscribe-secret"`` while the endpoint verified with
    ``secret_key`` or a *random per-process* key — so on any box without a
    configured ``secret_key``, every unsubscribe link we sent was signed with a
    key the endpoint would reject. It also carries the sending org now, so the
    opt-out is scoped to the sender the recipient is actually replying to.
    """
    from app.services.opt_out import make_unsubscribe_url

    return make_unsubscribe_url(contact_email)


def _unsubscribe_footer_text(contact_email: str) -> str:
    """Plain-text unsubscribe footer for the text/plain part."""
    unsubscribe_url = _make_unsubscribe_url(contact_email)
    settings = get_settings()
    return (
        "\n\n—\n"
        f"{settings.company_name}\n"
        f"Don't want these emails? Reply UNSUBSCRIBE or click: "
        f"{unsubscribe_url}\n"
        f"{settings.company_postal_address}"
    )


def _unsubscribe_footer_html(contact_email: str) -> str:
    """HTML unsubscribe footer with proper inline styles."""
    unsubscribe_url = _make_unsubscribe_url(contact_email)
    settings = get_settings()
    address = html_mod.escape(settings.company_postal_address)
    name = html_mod.escape(settings.company_name)
    return (
        '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0 12px 0">'
        '<p style="margin:0 0 4px 0;font-size:11px;color:#9ca3af">'
        f'{name} &middot; {address}</p>'
        '<p style="margin:0;font-size:11px;color:#9ca3af">'
        'Don\'t want these emails? Reply UNSUBSCRIBE or '
        f'<a href="{unsubscribe_url}" style="color:#9ca3af">unsubscribe here</a></p>'
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
    thread_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[str] = None,
    bypass_kill_switch: bool = False,
    skip_spam_scan: bool = False,
) -> dict:
    """Send one outreach email via the resolved sender.

    Returns a dict with at least ``status`` ("sent" | "skipped" | "halted" |
    "error") plus ``transport`` and ``sender`` so callers can record how it went.

    The **global sending kill switch** is checked first: when engaged, nothing is
    put on the wire and ``{"status": "halted", ...}`` is returned. This is the
    single choke point every outreach path funnels through, so the switch covers
    sequence steps, the outreach queue, and manual sends alike. ``bypass_kill_switch``
    exists only for internal callers that must still work while paused (none in
    normal operation).
    """
    if not bypass_kill_switch:
        from app.services import system_settings

        if not await system_settings.is_sending_enabled(db):
            logger.warning(
                "Send to %s blocked: global sending kill switch is engaged", to_email
            )
            return {
                "status": "halted",
                "reason": "Global sending kill switch is engaged — all outbound email is paused.",
                "transport": "none",
                "sender": None,
            }

    # --- Suppression list (Feature 2) -----------------------------------------
    # The do-not-contact list is a compliance gate: an address on it must never
    # receive ANY outbound mail. Checked here, the single choke point every
    # outreach path funnels through, so it covers sequence steps, the queue and
    # manual sends alike — and enforced even for kill-switch-bypass callers,
    # because suppression is stricter than the operational pause.
    from app.services import suppression

    if await suppression.is_suppressed(db, to_email):
        logger.info("Send to %s blocked: address is on the suppression list", to_email)
        return {
            "status": "suppressed",
            "reason": "Recipient is on the do-not-contact suppression list.",
            "transport": "none",
            "sender": None,
        }

    # --- Pre-send spam gate ---------------------------------------------------
    # The backstop for every path that doesn't scan its own copy first (manual
    # compose, template blasts, smart-reply auto-sends). The sequence path scans
    # the body *before* tracking is baked in — a rewrapped link set would read as
    # "too many links" here — so it passes ``skip_spam_scan=True`` rather than
    # being scored twice on copy it already cleared.
    if not skip_spam_scan:
        from app.services import spam_guard

        allowed, verdict = spam_guard.scan_for_send(
            subject, body_html, has_unsubscribe=include_unsubscribe
        )
        if not allowed:
            logger.error("Send to %s blocked by spam gate: %s", to_email, verdict.reason())
            return {
                "status": "skipped",
                "reason": verdict.reason(),
                "spam_blocked": True,
                "spam_scan": verdict.as_metadata(),
                "transport": "none",
                "sender": None,
            }

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

    # --- Warm-up gate (Feature 1) ---------------------------------------------
    # A new mailbox on a warm-up ramp must not exceed today's ramped cap. This is
    # the definitive per-account gate: it runs on the resolved account regardless
    # of how it was chosen (explicit id, rotation, or fallback). Graduated / no
    # plan → no constraint. Skipped for internal bypass callers.
    if account is not None and not bypass_kill_switch:
        from app.services import warmup

        allowed, warmup_reason = await warmup.check_send_allowed(db, account)
        if not allowed:
            logger.info(
                "Send to %s held by warm-up on %s: %s",
                to_email, account.email, warmup_reason,
            )
            return {
                "status": "skipped",
                "reason": warmup_reason,
                "transport": _transport_for(account),
                "sender": account.email,
            }

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

    # Build the unsubscribe URL once — used for both footer and headers
    unsubscribe_url = None
    if include_unsubscribe:
        unsubscribe_url = _make_unsubscribe_url(to_email)
        footer_html = _unsubscribe_footer_html(to_email)
        body_html = body_html + footer_html

    # Auto-generate clean plain text from HTML (never send HTML in text/plain)
    body_text = _html_to_plain_text(body_html)
    if include_unsubscribe:
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
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            list_unsubscribe_url=unsubscribe_url,
        )
        # Capture the sender email and account id for thread tracking
        result["from_email"] = account.email
        result["sender_account_id"] = str(account.id)
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
            # Auto-start the warm-up ramp on the first real send so the daily
            # cap ratchets up from the true first-send date (not a stale config).
            from app.services import system_settings
            await system_settings.ensure_warmup_started(db)
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
    if result.get("status") == "sent":
        from app.services import system_settings
        await system_settings.ensure_warmup_started(db)
    return result
