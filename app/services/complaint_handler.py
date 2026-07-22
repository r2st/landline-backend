"""Complaint handling: suppress a recipient who filed a spam/abuse complaint.

A complaint is the strongest do-not-contact signal there is — the recipient told
their mailbox provider our mail is spam. Continuing to mail them tanks sender
reputation far faster than a bounce and risks a blocklisting. So a complaint is
treated like a hard bounce, but as a *compliance* stop rather than a delivery
failure:

* the contact is suppressed immediately (``suppressed`` + legacy ``unsubscribed``
  do-not-email gates),
* every active/paused enrollment is stopped as ``unsubscribed``,
* the address is added to the org suppression LIST (``reason="complaint"``) so a
  future import or a manually-added contact with the same address is never mailed,
* a revoked ``ConsentRecord`` is written for the audit trail.

Idempotent: re-running for an already-suppressed contact only re-asserts the
terminal state. The ingestion sweep dedups on the source message before ever
calling here, so counters can't be inflated by polling frequency.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment

logger = logging.getLogger(__name__)


async def _contacts_for_email(db: AsyncSession, email: str) -> list[Contact]:
    """All contacts with this email (case-insensitive)."""
    email = (email or "").strip().lower()
    if not email:
        return []
    rows = await db.execute(select(Contact).where(func.lower(Contact.email) == email))
    return list(rows.scalars().all())


async def _suppress_contact(db: AsyncSession, contact: Contact, reason: str) -> int:
    """Mark a contact do-not-email and stop its active/paused enrollments.

    Returns the number of enrollments stopped. Idempotent.
    """
    contact.suppressed = True
    contact.unsubscribed = True
    now = datetime.now(timezone.utc)

    db.add(
        ConsentRecord(
            contact_id=contact.id,
            consent_type="none",
            basis=(reason or "spam complaint")[:2000],
            granted_at=None,
            revoked_at=now,
        )
    )

    rows = await db.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.contact_id == contact.id,
            OutreachEnrollment.status.in_(("active", "paused")),
        )
    )
    stopped = 0
    for enr in rows.scalars().all():
        enr.status = "unsubscribed"
        enr.completed_at = now
        stopped += 1
    return stopped


async def record_complaint(
    db: AsyncSession,
    email: str,
    *,
    feedback_type: str = "abuse",
    diagnostic: str = "",
) -> dict:
    """Record a complaint for ``email`` and suppress the complainer everywhere.

    Always adds the address to the org suppression list (so even an address with
    no matching contact row is never mailed), then suppresses every matching
    contact and stops its enrollments. Returns a summary dict.
    """
    normalized = (email or "").strip().lower()
    if not normalized:
        return {"email": email, "matched_contacts": 0, "suppressed": 0,
                "enrollments_stopped": 0, "list_status": "invalid"}

    # Belt-and-braces: list the address regardless of whether a contact exists.
    from app.services import suppression as _suppression

    _, list_status = await _suppression.add_entry(
        db, normalized, reason="complaint", source="auto"
    )

    contacts = await _contacts_for_email(db, normalized)
    reason = f"Spam complaint ({feedback_type}): {diagnostic or ''}".strip()

    suppressed = 0
    enrollments_stopped = 0
    for contact in contacts:
        was_suppressed = bool(getattr(contact, "suppressed", False))
        enrollments_stopped += await _suppress_contact(db, contact, reason)
        if not was_suppressed:
            suppressed += 1

    await db.flush()
    logger.info(
        "Recorded %s complaint for %s — %d contact(s), suppressed=%d, enrollments_stopped=%d, list=%s",
        feedback_type, normalized, len(contacts), suppressed, enrollments_stopped, list_status,
    )
    return {
        "email": normalized,
        "feedback_type": feedback_type,
        "matched_contacts": len(contacts),
        "suppressed": suppressed,
        "enrollments_stopped": enrollments_stopped,
        "list_status": list_status,
    }
