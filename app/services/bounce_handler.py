"""Bounce handling: classify hard/soft bounces and suppress dead addresses.

When outreach bounces we must stop emailing that address — repeatedly hitting
an invalid mailbox tanks sender reputation and gets the domain blocklisted.

* A **hard** bounce (permanent: unknown user, mailbox not found, 5.x.x) → the
  contact is suppressed immediately (``unsubscribed = True``, the existing
  do-not-email gate) and its active enrollments are set to ``bounced``.
* A **soft** bounce (transient: mailbox full, greylisted, 4.x.x) → tracked; the
  contact is only suppressed after ``soft_bounce_suppress_threshold`` of them,
  since transient failures often clear on their own.

Both increment ``Contact.bounce_count`` and stamp ``last_bounce_type`` /
``last_bounced_at`` for the deliverability report.

Classification is heuristic on the SMTP status code + diagnostic text, which is
what bounce webhooks (SES, Postmark) and mailer-daemon DSNs give us.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)

# Permanent-failure phrases seen in DSN diagnostics.
_HARD_PHRASES = (
    "user unknown", "no such user", "unknown user", "recipient not found",
    "mailbox not found", "does not exist", "no such mailbox", "invalid recipient",
    "address rejected", "recipient rejected", "no mailbox", "account disabled",
    "account has been disabled", "user not found", "domain not found",
    "does not accept mail", "relay access denied", "no such address",
)
# Transient-failure phrases.
_SOFT_PHRASES = (
    "mailbox full", "over quota", "quota exceeded", "insufficient storage",
    "temporarily", "try again", "greylist", "grey-list", "deferred",
    "timed out", "connection timed out", "rate limit", "too many", "throttl",
    "service unavailable", "temporary failure", "temporary local problem",
)


def classify_bounce(status_code: Optional[str] = None, diagnostic: str = "") -> str:
    """Return ``"hard"`` or ``"soft"`` for a bounce.

    Priority: an explicit SMTP status class (5.x.x → hard, 4.x.x → soft) wins;
    otherwise the diagnostic text is matched against known phrases. Ambiguous
    input defaults to ``"soft"`` — we would rather retry a live address than
    permanently suppress a contact on a vague signal.
    """
    code = (status_code or "").strip()
    # Enhanced status code like "5.1.1" or classic "550".
    m = re.match(r"\s*([245])", code)
    if m:
        cls = m.group(1)
        if cls == "5":
            return "hard"
        if cls in ("4", "2"):
            # 2.x.x isn't a bounce, but if labelled as one treat as soft/transient.
            return "soft"

    text = (diagnostic or "").lower()
    if any(p in text for p in _HARD_PHRASES):
        return "hard"
    if any(p in text for p in _SOFT_PHRASES):
        return "soft"
    return "soft"


async def _contacts_for_email(db: AsyncSession, email: str) -> list[Contact]:
    """All contacts with this email (case-insensitive)."""
    email = (email or "").strip().lower()
    if not email:
        return []
    rows = await db.execute(
        select(Contact).where(func.lower(Contact.email) == email)
    )
    return list(rows.scalars().all())


async def _suppress_contact(db: AsyncSession, contact: Contact) -> int:
    """Mark a contact do-not-email and stop its active enrollments.

    Returns the number of enrollments paused. Idempotent.
    """
    contact.unsubscribed = True
    rows = await db.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.contact_id == contact.id,
            OutreachEnrollment.status == "active",
        )
    )
    paused = 0
    now = datetime.now(timezone.utc)
    for enr in rows.scalars().all():
        enr.status = "bounced"
        enr.completed_at = now
        paused += 1
        # Book the bounce on the funnel too. ``funnel_analytics`` derives both
        # ``bounce_rate`` and ``delivered`` (= sent − bounced) from a "bounced"
        # OutreachEvent, and nothing in the codebase wrote one — so the funnel
        # reported 0 bounces and 100% delivered however many addresses were
        # actually dead. Measured in production 2026-07-24: one real hard bounce
        # on the enrollment, ``bounce_rate: 0.0`` on the funnel.
        #
        # Recorded against the step that was last sent, which is the step whose
        # send bounced. Deliberately not deduplicated here: ``record_bounce``
        # only reaches this for an *active* enrollment and flips it to "bounced"
        # in the same breath, so a second DSN for the same address finds nothing
        # active and writes nothing.
        db.add(
            OutreachEvent(
                enrollment_id=enr.id,
                step_number=enr.current_step,
                event_type="bounced",
                occurred_at=now,
                event_metadata={"email": contact.email, "source": "bounce_handler"},
            )
        )
    return paused


async def record_bounce(
    db: AsyncSession,
    email: str,
    *,
    status_code: Optional[str] = None,
    diagnostic: str = "",
    kind: Optional[str] = None,
) -> dict:
    """Record a bounce for ``email`` and suppress the contact when warranted.

    ``kind`` may be passed to force ``"hard"``/``"soft"``; otherwise it's
    classified from ``status_code`` + ``diagnostic``. Returns a summary dict.
    """
    settings = get_settings()
    bounce_kind = kind if kind in ("hard", "soft") else classify_bounce(status_code, diagnostic)
    now = datetime.now(timezone.utc)

    contacts = await _contacts_for_email(db, email)
    if not contacts:
        logger.info("Bounce for %s matched no contact (kind=%s)", email, bounce_kind)
        return {"email": email, "kind": bounce_kind, "matched_contacts": 0,
                "suppressed": 0, "enrollments_paused": 0}

    suppressed = 0
    enrollments_paused = 0
    for contact in contacts:
        contact.bounce_count = (contact.bounce_count or 0) + 1
        contact.last_bounce_type = bounce_kind
        contact.last_bounced_at = now

        should_suppress = bounce_kind == "hard" or (
            contact.bounce_count >= settings.soft_bounce_suppress_threshold
        )
        if should_suppress and not contact.unsubscribed:
            enrollments_paused += await _suppress_contact(db, contact)
            suppressed += 1
        elif should_suppress:
            # Already unsubscribed — still ensure enrollments are stopped.
            enrollments_paused += await _suppress_contact(db, contact)

    await db.flush()
    logger.info(
        "Recorded %s bounce for %s — %d contact(s), suppressed=%d, enrollments_paused=%d",
        bounce_kind, email, len(contacts), suppressed, enrollments_paused,
    )
    return {
        "email": email,
        "kind": bounce_kind,
        "matched_contacts": len(contacts),
        "suppressed": suppressed,
        "enrollments_paused": enrollments_paused,
    }
