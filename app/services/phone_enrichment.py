"""Phone enrichment for warm leads.

A prospect who keeps engaging — opening several emails, or clicking / replying —
is worth a personal phone call, but Dialtone only has a number if a card scan or
a people-search happened to carry one. This module closes that gap: it finds the
warm leads that still lack a phone and enriches them one at a time via Apollo's
``/people/match`` (the existing enrichment client), then records where the
number came from so the UI can badge it and the sweep never pays twice.

The "warm" test mirrors the notification thresholds already used elsewhere:

* **3+ distinct opens** — an open is deduplicated per email (see the tracking
  pixel), so three opens means three *different* emails were opened, not one
  email re-rendered three times. That is a real signal, not inbox noise.
* **any click or reply** — a single click or reply is a stronger signal than
  opens and qualifies on its own.

Both open sources are counted (the sequence path logs ``OutreachEvent``; the
one-off path logs ``EmailSend.opened_at``), because a contact can have engaged
through either. Enrichment is attempted at most once per contact
(``phone_enriched_at`` is stamped on every attempt, hit or miss), so a lead with
no findable number is written off rather than retried on every sweep.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.services.enrichment import ApolloEnrichmentService
from app.services.notifications import get_notification_service

logger = logging.getLogger(__name__)

# A contact is "warm" at this many distinct opened emails. Matches
# ``NotificationService.notify_open``'s 3-open bar so the two signals agree.
OPEN_THRESHOLD = 3
# Cap the candidate scan so one sweep can't load the whole contacts table into
# memory. Newest contacts first — engagement is recent by nature.
CANDIDATE_SCAN_LIMIT = 2000


async def _candidate_contact_ids(db: AsyncSession) -> list[UUID]:
    """Contacts that still lack any phone and were never attempted.

    The selection set for the sweep: no ``phone``/``mobile``, ``phone_enriched_at
    IS NULL`` (never attempted), and still emailable (not suppressed /
    unsubscribed — a dead lead isn't worth a credit). Ordered newest-first and
    capped so the warm-signal pass below runs over a bounded set.
    """
    stmt = (
        select(Contact.id)
        .where(
            Contact.phone.is_(None),
            Contact.mobile.is_(None),
            Contact.phone_enriched_at.is_(None),
            Contact.suppressed.is_(False),
            or_(Contact.unsubscribed.is_(False), Contact.unsubscribed.is_(None)),
        )
        .order_by(Contact.created_at.desc())
        .limit(CANDIDATE_SCAN_LIMIT)
    )
    return [row[0] for row in (await db.execute(stmt)).all()]


async def _engaged_contact_ids(db: AsyncSession, candidates: list[UUID]) -> set[UUID]:
    """Subset of ``candidates`` with any click or reply (either send path)."""
    if not candidates:
        return set()
    engaged: set[UUID] = set()

    # Replies (the strongest signal) — one row per received reply.
    reply_ids = (
        await db.execute(
            select(EmailReply.contact_id)
            .where(EmailReply.contact_id.in_(candidates))
            .distinct()
        )
    ).all()
    engaged.update(r[0] for r in reply_ids if r[0] is not None)

    # One-off sends that were clicked or replied to.
    send_ids = (
        await db.execute(
            select(EmailSend.contact_id)
            .where(
                EmailSend.contact_id.in_(candidates),
                or_(EmailSend.clicked_at.isnot(None), EmailSend.replied_at.isnot(None)),
            )
            .distinct()
        )
    ).all()
    engaged.update(r[0] for r in send_ids if r[0] is not None)

    # Sequence steps that were clicked or replied to (OutreachEvent → enrollment).
    seq_ids = (
        await db.execute(
            select(OutreachEnrollment.contact_id)
            .join(OutreachEvent, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .where(
                OutreachEnrollment.contact_id.in_(candidates),
                OutreachEvent.event_type.in_(("clicked", "replied")),
            )
            .distinct()
        )
    ).all()
    engaged.update(r[0] for r in seq_ids if r[0] is not None)

    return engaged


async def _open_counts(db: AsyncSession, candidates: list[UUID]) -> dict[UUID, int]:
    """Distinct-open count per candidate, summed across both send paths."""
    if not candidates:
        return {}
    counts: dict[UUID, int] = {}

    # One-off sends: opened_at set means the (single) send was opened.
    send_rows = (
        await db.execute(
            select(EmailSend.contact_id)
            .where(
                EmailSend.contact_id.in_(candidates),
                EmailSend.opened_at.isnot(None),
            )
        )
    ).all()
    for (cid,) in send_rows:
        if cid is not None:
            counts[cid] = counts.get(cid, 0) + 1

    # Sequence opens: OutreachEvent rows are already deduplicated per
    # (enrollment, step) by the tracking pixel, so each row is one distinct open.
    seq_rows = (
        await db.execute(
            select(OutreachEnrollment.contact_id)
            .join(OutreachEvent, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .where(
                OutreachEnrollment.contact_id.in_(candidates),
                OutreachEvent.event_type == "opened",
            )
        )
    ).all()
    for (cid,) in seq_rows:
        if cid is not None:
            counts[cid] = counts.get(cid, 0) + 1

    return counts


async def warm_contacts_needing_phone(
    db: AsyncSession, limit: int = 25
) -> list[Contact]:
    """Warm leads (3+ opens or any click/reply) that still lack a phone.

    Returns loaded ``Contact`` rows (with their company) ready to enrich, up to
    ``limit``, newest-engagement-agnostic but bounded for a polite per-sweep API
    spend. Callers stamp ``phone_enriched_at`` so a returned contact is not
    handed back on the next sweep.
    """
    candidates = await _candidate_contact_ids(db)
    if not candidates:
        return []

    engaged = await _engaged_contact_ids(db, candidates)
    opens = await _open_counts(db, candidates)

    warm_ids = [
        cid
        for cid in candidates
        if cid in engaged or opens.get(cid, 0) >= OPEN_THRESHOLD
    ]
    if not warm_ids:
        return []
    warm_ids = warm_ids[:limit]

    rows = (
        await db.execute(select(Contact).where(Contact.id.in_(warm_ids)))
    ).scalars().all()
    return list(rows)


async def enrich_contact_phone(
    db: AsyncSession,
    contact: Contact,
    *,
    service: Optional[ApolloEnrichmentService] = None,
    notify: bool = True,
) -> dict:
    """Attempt to find and persist a phone number for one warm contact.

    Tries Apollo's per-person match first; if that yields nothing, falls back to
    the company's main line so the rep at least has *a* number to call. Stamps
    ``phone_enriched_at`` and ``phone_source`` regardless of outcome so the sweep
    is idempotent. Returns a small summary dict.
    """
    service = service or ApolloEnrichmentService()

    company: Optional[Company] = None
    if contact.company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalar_one_or_none()

    result = await service.match_person_phone(
        db,
        first_name=contact.first_name,
        last_name=contact.last_name,
        email=contact.email,
        domain=company.domain if company else None,
        organization_name=company.name if company else None,
    )

    found_phone: Optional[str] = None
    found_mobile: Optional[str] = None
    source: Optional[str] = None

    if result.get("status") == "found":
        found_phone = result.get("phone")
        found_mobile = result.get("mobile")
        source = "apollo"
    elif company is not None and company.phone:
        # Fallback: the company's main line. Not a direct dial, but a real number
        # a rep can call and ask for the contact. Marked distinctly so the UI can
        # show it as the company line, not a personal one.
        found_phone = company.phone
        source = "company"

    contact.phone_enriched_at = datetime.now(timezone.utc)
    if found_phone and not contact.phone:
        contact.phone = found_phone[:30]
    if found_mobile and not contact.mobile:
        contact.mobile = found_mobile[:30]
    if source:
        contact.phone_source = source

    await db.flush()

    if source and notify:
        try:
            await get_notification_service().notify_phone_found(
                contact_name=contact.full_name,
                company_name=company.name if company else "a company",
                phone=contact.mobile or contact.phone or found_phone or "",
            )
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.info("phone-found notify failed for contact %s: %s", contact.id, exc)

    return {
        "contact_id": str(contact.id),
        "status": result.get("status"),
        "source": source,
        "phone": contact.phone,
        "mobile": contact.mobile,
    }
