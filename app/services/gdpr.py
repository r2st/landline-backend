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

**The two operations must cover the same tables.** They are halves of one
obligation: what a subject access request has to hand over is what an erasure
request has to be able to remove. When they disagree, the export is the one
telling the truth — it is written by asking "where is this person's data?", and
erasure is written by listing columns. That is how the sequence engine's copy of
every message went missing for as long as it did: ``export_subject`` walked the
subject's enrollments and returned their ``OutreachEvent`` rows, while
``erase_subject`` scrubbed ``email_sends`` and stopped. Per "Sent mail lives in
two tables", the sequence path writes no ``EmailSend`` row at all — recipient,
subject and full ``body_html`` live in ``outreach_events.metadata`` — so the
channel carrying most of the volume was disclosed on request and never erased.
Adding a table to one of these functions means adding it to the other.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, inspect as sa_inspect, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_email_draft import AiEmailDraft
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

    **Walks the mapper, not the table.** A column's name and the attribute that
    maps it are two different strings, and ``OutreachEvent`` is where they
    differ: ``event_metadata: Mapped[dict] = mapped_column("metadata", ...)``,
    because ``metadata`` is taken on a declarative class. Reading the *column*
    name off the row therefore ran ``getattr(event, "metadata")`` and got
    ``Base.metadata`` — the SQLAlchemy registry — in place of the JSONB blob.

    Two consequences, and the loud one hid the quiet one. The export is returned
    straight out of ``GET /gdpr/export``, and a ``MetaData`` object is not JSON
    serialisable, so the endpoint **500'd for any subject who had been through a
    sequence** — which is every real prospect, since the sequence engine is the
    main sending path. Underneath that, the value it was substituting for was
    the recipient, subject line and full message body of every send: the single
    largest holding of the subject's data, silently absent from the disclosure
    the endpoint exists to make.

    Both halves are fixed by asking the mapper for the attribute and the column
    for the key, rather than assuming one string does both jobs. The output keys
    are unchanged (``metadata`` stays ``metadata``) — what changes is that the
    value under them is now the row's.
    """
    unloaded = sa_inspect(obj).unloaded
    out: dict = {}
    for prop in sa_inspect(obj).mapper.column_attrs:
        col = prop.columns[0]
        # ``prop.key`` is the Python attribute; ``col.name`` is the database
        # column. ``unloaded`` is keyed by the former, output by the latter.
        if prop.key in unloaded:
            out[col.name] = None
            continue
        val = getattr(obj, prop.key)
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
    # Held about the subject, so disclosed on request — and, because it is the
    # same obligation read from the other side, scrubbed by ``erase_subject``.
    # ``personalization`` in particular is the research agent's dossier on this
    # individual, which a subject access request is precisely for.
    ai_drafts = await _by_contact(AiEmailDraft)

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
            "ai_email_drafts": len(ai_drafts),
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
        "ai_email_drafts": [_serialize(x) for x in ai_drafts],
    }


def _scrub(obj, fields, value=None) -> None:
    for f in fields:
        if hasattr(obj, f):
            setattr(obj, f, value if value is not None else _REDACTED)


# Keys inside ``outreach_events.event_metadata`` that carry the subject's own
# data. Everything else in that blob is operational — ``status``, ``tracked``,
# ``ai_generated``, ``sender_account_id``, the Gmail thread/message ids the
# reply matcher and the reconciler key on — and is left alone, because erasure
# is meant to remove the person, not the record that a message was sent.
#
# ``personalization`` is the one that matters most and reads as harmless: it is
# where the research agent stores what it found out about this individual, which
# is the *most* sensitive thing in the row and is not a message body. ``error``
# is here because SMTP diagnostics quote the envelope recipient back verbatim,
# which is how an address survives a scrub of every column named after one.
_EVENT_PII_KEYS = (
    "to_email", "from_email", "recipient", "sender",
    "subject", "body_html", "body_text", "body", "snippet", "preview",
    "personalization", "error",
)


def _scrub_metadata(blob: Optional[dict]) -> tuple[dict, bool]:
    """Redact the PII keys in one ``event_metadata`` blob.

    Returns the new blob and whether anything changed, so an already-erased
    subject reports zero new scrubs rather than counting rows it rewrote to the
    identical value — this function is what makes the OutreachEvent pass as
    idempotent as the rest of :func:`erase_subject`.

    A new dict, never a mutation in place: ``event_metadata`` is a JSONB column
    and SQLAlchemy does not track mutations *inside* a plain dict, so scrubbing
    key-by-key on the loaded value changes the object and writes nothing.
    """
    if not blob:
        return {}, False
    scrubbed = dict(blob)
    changed = False
    for key in _EVENT_PII_KEYS:
        if key in scrubbed and scrubbed[key] not in (None, "", {}, _REDACTED):
            scrubbed[key] = _REDACTED
            changed = True
    return scrubbed, changed


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

    # --- Stop what is still running for this subject --------------------------
    # The fifth do-not-contact writer, and the one that was stopping nothing.
    # ``opt_out.record``, ``complaint_handler``, ``bounce_handler`` and the
    # reply path all close the subject's live enrollments; erasure — the
    # strongest of the six decisions — set the flags and left them enrolled.
    #
    # Suppressing the contact is what made that survivable rather than a leak:
    # the queue sweep stops an ``active`` enrollment whose contact is suppressed
    # the next time it comes due. But it only looks at ``active`` ones, and
    # ``pending_review`` is not a resting state — ``expire_stale_review_drafts``
    # un-parks it back to ``active``, stamps it due now and dispatches. So an
    # erased subject's parked enrollment was revived hours later and put back on
    # the send path, and the enrollments that *were* active stayed listed as
    # live outreach until their next due date, for someone who had exercised
    # their right to be forgotten. ``ACTIONABLE_STATUSES`` is the shared answer
    # to which enrollments a decision like this has to interrupt.
    #
    # ``unsubscribed``, never ``bounced``: a compliance stop is not a delivery
    # failure and must not move sender reputation metrics.
    #
    # Gated on ``contact_ids`` rather than on ``erased_contacts``: a re-run
    # against an already-anonymised subject scrubs no new columns and must still
    # close anything that has been enrolled since.
    from app.services.opt_out import stoppable_enrollments

    stopped = 0
    for enr in await stoppable_enrollments(db, contact_ids):
        enr.status = "unsubscribed"
        enr.completed_at = now
        stopped += 1
    counts["enrollments_stopped"] = stopped

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

    # --- The sequence engine's copy of every message it sent this subject -----
    #
    # This is the largest store of the subject's data in the system and erasure
    # never touched it. Per "Sent mail lives in two tables": the sequence path
    # writes **no** ``EmailSend`` row, and persists the recipient, the subject
    # line and the full rendered ``body_html`` in ``outreach_events.metadata``
    # instead. So scrubbing ``email_sends`` covered manual sends, template sends
    # and inbox replies — and left untouched the channel that carries the bulk of
    # the volume, in full, addressed by name.
    #
    # The asymmetry is what makes this a bug rather than a judgement call:
    # :func:`export_subject` directly above already walks the subject's
    # enrollments and returns their ``OutreachEvent`` rows in the Subject Access
    # Request. The two operations are halves of one obligation — what Article 15
    # says we must hand over is what Article 17 says we must be able to remove —
    # and one of them was reading a table the other could not see.
    #
    # Keyed through the enrollments rather than by address, because an
    # ``OutreachEvent`` has no email column of its own; the enrollment's
    # ``contact_id`` is the only link, and it is the same link the export uses.
    erased_events = 0
    if contact_ids:
        enrollment_ids = list(
            (
                await db.execute(
                    select(OutreachEnrollment.id).where(
                        OutreachEnrollment.contact_id.in_(contact_ids)
                    )
                )
            ).scalars().all()
        )
        for chunk_start in range(0, len(enrollment_ids), 500):
            chunk = enrollment_ids[chunk_start:chunk_start + 500]
            events = list(
                (
                    await db.execute(
                        select(OutreachEvent).where(
                            OutreachEvent.enrollment_id.in_(chunk)
                        )
                    )
                ).scalars().all()
            )
            for ev in events:
                scrubbed, changed = _scrub_metadata(ev.event_metadata)
                if changed:
                    ev.event_metadata = scrubbed
                    erased_events += 1
    counts["outreach_events"] = erased_events

    # AI drafts hold the same copy one step earlier, plus ``personalization`` —
    # the research agent's findings about this individual, which is the single
    # most sensitive column in the schema and belongs to no message.
    drafts = []
    if contact_ids:
        drafts = list(
            (
                await db.execute(
                    select(AiEmailDraft).where(AiEmailDraft.contact_id.in_(contact_ids))
                )
            ).scalars().all()
        )
    for d in drafts:
        _scrub(d, ("subject", "body", "static_subject", "static_body"))
        # ``personalization`` is dropped rather than redacted: unlike the event
        # blob, nothing reads a key out of this one — it is displayed whole — so
        # an empty object is the honest representation, and it leaves behind no
        # key names describing what was collected about someone who asked to be
        # forgotten.
        if d.personalization:
            d.personalization = {}
        scrubbed, changed = _scrub_metadata(d.send_result)
        if changed:
            d.send_result = scrubbed
    counts["ai_email_drafts"] = len(drafts)

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
