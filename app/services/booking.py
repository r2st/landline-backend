"""Feature 4 — Calendar Booking Injection.

Interest-gated insertion of a per-sender scheduling link into follow-ups. A
booking link in a *first* cold email reads as presumptuous and hurts reply
rates; the same link *after* the prospect opens/clicks/replies converts. The
gate here is the single source of truth both the ``{{booking_link}}`` template
variable and the AI follow-up path consult, so they never disagree.

Public surface:

* :func:`should_inject_booking_link` — the interest gate (interested reply,
  link click, or repeated opens).
* :func:`validate_booking_url` — https + known-scheduler-host allowlist check,
  run on save.
* :func:`resolve_booking_url` — the fallback chain: sender ``booking_url`` →
  global ``settings.scheduling_link`` → empty.
* :func:`apply_booking_placeholder` — the render rule that fills or cleanly
  strips a ``{{booking_link}}`` scaffolding line.
* :func:`collapse_booking_links` — post-render safety: collapse a body that
  contains the booking URL more than once down to a single occurrence.
"""

import logging
import re
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.gmail_account import GmailAccount
from app.models.outreach import OutreachEvent
from app.models.smtp_account import SmtpAccount

logger = logging.getLogger(__name__)

# Matches ``{{booking_link}}`` with any inner whitespace ("{{ booking_link }}").
_BOOKING_VAR_RE = re.compile(r"\{\{\s*booking_link\s*\}\}")


def validate_booking_url(url: str | None, allowed_hosts: list[str] | None = None) -> tuple[bool, str]:
    """Validate a booking URL: must be ``https`` and on the scheduler allowlist.

    Returns ``(ok, error)`` — ``error`` is empty on success. A host matches when
    it equals an allowlisted host or is a subdomain of one (so
    ``meetings.hubspot.com`` passes against ``hubspot.com``). An empty/blank URL
    is treated as "clear the field" and is considered valid.
    """
    if url is None or not url.strip():
        # Empty means "no booking link configured" — a valid state (clears it).
        return True, ""

    parsed = urlparse(url.strip())
    if parsed.scheme != "https":
        return False, "Booking URL must use https"
    host = (parsed.hostname or "").lower()
    if not host:
        return False, "Booking URL has no host"

    if allowed_hosts is None:
        allowed_hosts = get_settings().booking_scheduler_hosts
    for allowed in allowed_hosts:
        allowed = allowed.lower().strip()
        if allowed and (host == allowed or host.endswith("." + allowed)):
            return True, ""
    return False, f"Host '{host}' is not a recognized scheduler"


async def should_inject_booking_link(contact, enrollment, db: AsyncSession) -> bool:
    """The interest gate — True when a booking link is warranted for this contact.

    Any one of the following interest signals opens the gate:

    * a reply from the contact classified ``interested`` (Feature 1),
    * a recorded link click (an ``OutreachEvent`` ``clicked`` on the enrollment
      or an ``EmailSend.clicked_at`` for the contact), OR
    * repeated opens (``>= settings.booking_min_opens`` ``opened`` events on the
      enrollment).

    No signal → False, so cold, uninterested contacts never get a link. Both the
    ``{{booking_link}}`` template variable and the AI follow-up path call this so
    the rule is centralized.
    """
    # 1. Reply classified 'interested' (Feature 1) — the strongest signal.
    if contact is not None:
        row = (
            await db.execute(
                select(EmailReply.id)
                .where(
                    EmailReply.contact_id == contact.id,
                    EmailReply.reply_classification == "interested",
                )
                .limit(1)
            )
        ).first()
        if row is not None:
            return True

    # 2. Link click — from the enrollment's events or any send to the contact.
    if enrollment is not None:
        row = (
            await db.execute(
                select(OutreachEvent.id)
                .where(
                    OutreachEvent.enrollment_id == enrollment.id,
                    OutreachEvent.event_type == "clicked",
                )
                .limit(1)
            )
        ).first()
        if row is not None:
            return True
    if contact is not None:
        row = (
            await db.execute(
                select(EmailSend.id)
                .where(
                    EmailSend.contact_id == contact.id,
                    EmailSend.clicked_at.isnot(None),
                )
                .limit(1)
            )
        ).first()
        if row is not None:
            return True

    # 3. Repeated opens (soft signal, configurable threshold).
    if enrollment is not None:
        min_opens = max(1, getattr(get_settings(), "booking_min_opens", 2))
        opens = (
            await db.execute(
                select(func.count())
                .select_from(OutreachEvent)
                .where(
                    OutreachEvent.enrollment_id == enrollment.id,
                    OutreachEvent.event_type == "opened",
                )
            )
        ).scalar_one()
        if (opens or 0) >= min_opens:
            return True

    return False


async def resolve_booking_url(db: AsyncSession, sender_account_id, settings=None) -> str:
    """Resolve the booking URL for a sender: ``booking_url`` → ``scheduling_link`` → "".

    Loads the sending identity (Gmail first, then SMTP) by id and returns its
    configured ``booking_url``; failing that, the global ``settings.scheduling_link``;
    failing that, an empty string. Never raises — a lookup problem degrades to
    the global fallback so a send is never blocked on it.
    """
    if settings is None:
        settings = get_settings()

    account = None
    if sender_account_id is not None:
        try:
            account = (
                await db.execute(
                    select(GmailAccount).where(GmailAccount.id == sender_account_id)
                )
            ).scalar_one_or_none()
            if account is None:
                account = (
                    await db.execute(
                        select(SmtpAccount).where(SmtpAccount.id == sender_account_id)
                    )
                ).scalar_one_or_none()
        except Exception as exc:  # pragma: no cover - defensive
            logger.info("resolve_booking_url account lookup failed: %s", exc)
            account = None

    if account is not None and (getattr(account, "booking_url", None) or "").strip():
        return account.booking_url.strip()

    return (getattr(settings, "scheduling_link", "") or "").strip()


def apply_booking_placeholder(template: str | None, booking_url: str) -> str:
    """Fill or strip the ``{{booking_link}}`` scaffolding before Jinja rendering.

    * ``booking_url`` set → leave the placeholder in place; the caller passes
      ``booking_link=<url>`` in the render context so Jinja substitutes it.
    * ``booking_url`` empty (no URL configured or interest gate not met) → drop
      the *entire line* that carries the placeholder, so we never emit a dangling
      "Grab a time here: " with nothing after it.
    """
    if not template:
        return template or ""
    if booking_url:
        return template

    kept = [line for line in template.splitlines(keepends=True) if not _BOOKING_VAR_RE.search(line)]
    return "".join(kept)


def collapse_booking_links(body: str | None, booking_url: str) -> str:
    """Collapse a body containing the booking URL more than once to a single one.

    Post-render safety for AI-composed follow-ups: the model is told to include
    the link exactly once, but if it repeats it, keep the first occurrence and
    remove the rest (tidying up the leftover whitespace).
    """
    if not body or not booking_url or body.count(booking_url) <= 1:
        return body or ""

    head, _, tail = body.partition(booking_url)
    tail = tail.replace(booking_url, "")
    collapsed = head + booking_url + tail
    # Tidy doubled spaces left where a repeated link was removed.
    collapsed = re.sub(r"[ \t]{2,}", " ", collapsed)
    return collapsed
