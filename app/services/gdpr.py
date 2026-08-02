"""GDPR data-subject services: access-request export + right-to-erasure.

A *data subject* is identified by an email address. Landline stores their PII in
the ``contacts`` row plus everything linked to it (interactions, sends, replies,
enrollments, follow-ups, tasks, consent, bounces) and anything keyed directly on
the address (email sends/replies/bounces). Two operations:

* :func:`export_subject` — assemble every record we hold about the subject into a
  JSON-serialisable dict (a Subject Access Request).
* :func:`erase_subject` — soft-delete: scrub the PII in place across all those
  tables and stamp ``contacts.anonymized_at``, so rows survive for referential
  integrity / aggregate stats but carry no personal data, and the address can no
  longer be contacted.

The email match is case-insensitive. Multiple contact rows can share an address
(email is not unique) — all of them are covered.
"""

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, inspect as sa_inspect, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.models.interaction import Interaction
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.task import Task

logger = logging.getLogger(__name__)

# Placeholder written into scrubbed free-text/address columns.
_REDACTED = "[redacted]"


def _serialize(obj) -> dict:
    """Turn an ORM row into a JSON-safe dict (UUID/datetime → str).

    Reads only already-loaded attributes — an unloaded column is reported as
    ``None`` rather than triggering a lazy DB load, which is illegal under an
    async session (``MissingGreenlet``). In the request path every column is
    loaded by the SELECT, so this only matters for oddly-hydrated instances.
    """
    unloaded = sa_inspect(obj).unloaded
    out: dict = {}
    for col in obj.__table__.columns:
        if col.key in unloaded:
            out[col.name] = None
            continue
        val = getattr(obj, col.name)
        if isinstance(val, (uuid.UUID, datetime)):
            val = str(val)
        out[col.name] = val
    return out


def _email_or_contact(model, email_col, email: str, contact_ids: list):
    """WHERE that matches rows keyed by the address OR by any of the contact ids."""
    conditions = [func.lower(email_col) == email]
    if contact_ids:
        conditions.append(model.contact_id.in_(contact_ids))
    return or_(*conditions)


# Serialising a row reads every column, so any instance already in the session's
# identity map must be fully refreshed — an unloaded server-default column (e.g.
# created_at) would otherwise trigger a lazy load, which async SQLAlchemy forbids
# (MissingGreenlet). ``populate_existing`` forces a full refresh from the row.
_LOAD = {"populate_existing": True}


async def _contacts_for_email(db: AsyncSession, email: str) -> list[Contact]:
    return list(
        (
            await db.execute(
                select(Contact)
                .where(func.lower(Contact.email) == email)
                .execution_options(**_LOAD)
            )
        ).scalars().all()
    )


async def export_subject(db: AsyncSession, email: str) -> dict:
    """Assemble every record held about ``email`` into a serialisable dict."""
    email = (email or "").strip().lower()
    contacts = await _contacts_for_email(db, email)
    contact_ids = [c.id for c in contacts]
    company_ids = {c.company_id for c in contacts if c.company_id}

    companies = []
    if company_ids:
        companies = list(
            (
                await db.execute(
                    select(Company).where(Company.id.in_(company_ids)).execution_options(**_LOAD)
                )
            ).scalars().all()
        )

    async def _by_contact(model):
        if not contact_ids:
            return []
        return list(
            (
                await db.execute(
                    select(model).where(model.contact_id.in_(contact_ids)).execution_options(**_LOAD)
                )
            ).scalars().all()
        )

    interactions = await _by_contact(Interaction)
    follow_ups = await _by_contact(FollowUp)
    tasks = await _by_contact(Task)
    consent = await _by_contact(ConsentRecord)
    enrollments = await _by_contact(OutreachEnrollment)

    enrollment_ids = [e.id for e in enrollments]
    events = []
    if enrollment_ids:
        events = list(
            (
                await db.execute(
                    select(OutreachEvent)
                    .where(OutreachEvent.enrollment_id.in_(enrollment_ids))
                    .execution_options(**_LOAD)
                )
            ).scalars().all()
        )

    # Email tables can be keyed by the address directly (a send/reply/bounce that
    # never got a Contact row), so match on both the contact ids and the address.
    sends = list(
        (
            await db.execute(
                select(EmailSend)
                .where(_email_or_contact(EmailSend, EmailSend.to_email, email, contact_ids))
                .execution_options(**_LOAD)
            )
        ).scalars().all()
    )
    replies = list(
        (
            await db.execute(
                select(EmailReply)
                .where(_email_or_contact(EmailReply, EmailReply.from_email, email, contact_ids))
                .execution_options(**_LOAD)
            )
        ).scalars().all()
    )
    bounces = list(
        (
            await db.execute(
                select(BounceRecord)
                .where(func.lower(BounceRecord.recipient_email) == email)
                .execution_options(**_LOAD)
            )
        ).scalars().all()
    )

    return {
        "subject_email": email,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "record_counts": {
            "contacts": len(contacts),
            "interactions": len(interactions),
            "email_sends": len(sends),
            "email_replies": len(replies),
            "outreach_enrollments": len(enrollments),
            "outreach_events": len(events),
            "follow_ups": len(follow_ups),
            "tasks": len(tasks),
            "consent_records": len(consent),
            "bounce_records": len(bounces),
        },
        "contacts": [_serialize(c) for c in contacts],
        "companies": [_serialize(c) for c in companies],
        "interactions": [_serialize(x) for x in interactions],
        "email_sends": [_serialize(x) for x in sends],
        "email_replies": [_serialize(x) for x in replies],
        "outreach_enrollments": [_serialize(x) for x in enrollments],
        "outreach_events": [_serialize(x) for x in events],
        "follow_ups": [_serialize(x) for x in follow_ups],
        "tasks": [_serialize(x) for x in tasks],
        "consent_records": [_serialize(x) for x in consent],
        "bounce_records": [_serialize(x) for x in bounces],
    }


def _scrub(obj, fields, value=None) -> None:
    for f in fields:
        if hasattr(obj, f):
            setattr(obj, f, value if value is not None else _REDACTED)


async def erase_subject(db: AsyncSession, email: str) -> dict:
    """Right-to-erasure: scrub the subject's PII in place across all tables.

    Idempotent — running it again on an already-anonymised subject is a no-op
    that reports zero new scrubs. Commits once at the end.
    """
    email = (email or "").strip().lower()
    contacts = await _contacts_for_email(db, email)
    contact_ids = [c.id for c in contacts]
    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    # --- Suppress the ADDRESS before anything scrubs it -----------------------
    # Two things matter here: that it happens at all, and that what it records is
    # the address the *request* named — never one read back off a contact row,
    # because the loop below rewrites those. Sitting above the scrub is what
    # keeps the second part obviously true rather than merely true today.
    #
    # The block below sets ``suppressed``/``unsubscribed`` and then overwrites
    # ``c.email`` with an ``anonymized+<id>@redacted.invalid`` placeholder. The
    # flags survive; the address they were about does not. That left nothing in
    # the system mapping this person to "never contact", so the next time the
    # scraper found them — from the same public source that found them
    # originally — it created a fresh contact with ``suppressed=False`` and the
    # sequence engine cold-emailed someone who had exercised their right to be
    # forgotten. That is worse than the re-scrape hole a plain unsubscribe had:
    # erasure doesn't just fail to be durable, it deletes the only link.
    #
    # ``SuppressionEntry`` is keyed by address and outlives every contact row.
    # Keeping the minimum needed to *honour* an erasure is the recognised
    # carve-out — you cannot avoid mailing someone without knowing who not to
    # mail — and the send gate already reads this list on every path.
    #
    # Unconditional, not gated on ``contacts``: an erasure request for an address
    # we hold no row for (or whose rows a previous erasure already anonymised,
    # before this wrote anything) still has to be honoured if that address is
    # ever rediscovered.
    from app.services import suppression as _suppression

    _, suppression_status = await _suppression.add_entry(
        db, email, reason="erasure", source="manual"
    )
    counts["suppression_entry"] = 1 if suppression_status == "added" else 0

    # --- The contacts themselves: scrub PII, block, stamp anonymized_at --------
    erased_contacts = 0
    for c in contacts:
        if c.anonymized_at is not None:
            continue  # already erased
        c.first_name = None
        c.last_name = None
        c.email = f"anonymized+{c.id}@redacted.invalid"
        c.phone = None
        c.mobile = None
        c.fax = None
        c.additional_phones = None
        c.linkedin_url = None
        c.website = None
        c.address = None
        c.city = None
        c.state = None
        c.postal_code = None
        c.title = None
        c.department = None
        c.notes = None
        c.card_image = None
        c.timezone = None
        c.email_verification_meta = {}
        # Compliance: a forgotten subject must never receive mail again.
        c.unsubscribed = True
        c.suppressed = True
        c.anonymized_at = now
        erased_contacts += 1
    counts["contacts"] = erased_contacts

    async def _scrub_by_contact(model, fields):
        if not contact_ids:
            return 0
        rows = list(
            (
                await db.execute(select(model).where(model.contact_id.in_(contact_ids)))
            ).scalars().all()
        )
        for r in rows:
            _scrub(r, fields)
        return len(rows)

    counts["interactions"] = await _scrub_by_contact(Interaction, ("subject", "notes", "content"))
    counts["follow_ups"] = await _scrub_by_contact(FollowUp, ("reason", "notes"))
    counts["tasks"] = await _scrub_by_contact(Task, ("title", "description"))

    # --- Email-keyed tables (match by contact OR address) ---------------------
    sends = list(
        (
            await db.execute(
                select(EmailSend).where(_email_or_contact(EmailSend, EmailSend.to_email, email, contact_ids))
            )
        ).scalars().all()
    )
    for s in sends:
        s.to_email = "redacted@redacted.invalid"
        _scrub(s, ("subject", "body_html", "error"))
    counts["email_sends"] = len(sends)

    replies = list(
        (
            await db.execute(
                select(EmailReply).where(_email_or_contact(EmailReply, EmailReply.from_email, email, contact_ids))
            )
        ).scalars().all()
    )
    for r in replies:
        r.from_email = "redacted@redacted.invalid"
        if r.to_email:
            r.to_email = "redacted@redacted.invalid"
        _scrub(r, ("subject", "body_text", "body_html", "snippet"))
    counts["email_replies"] = len(replies)

    bounces = list(
        (
            await db.execute(
                select(BounceRecord).where(func.lower(BounceRecord.recipient_email) == email)
            )
        ).scalars().all()
    )
    for b in bounces:
        b.recipient_email = "redacted@redacted.invalid"
        _scrub(b, ("diagnostic",))
    counts["bounce_records"] = len(bounces)

    await db.commit()

    logger.info("GDPR erasure for %s: %s", email, counts)
    return {
        "subject_email": email,
        "erased_at": now.isoformat(),
        "anonymized_contacts": erased_contacts,
        "scrubbed": counts,
    }
