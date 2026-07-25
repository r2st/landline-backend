"""Out-of-office: pause the sequence, then bring it back.

Detection already exists and is deterministic —
:func:`app.services.ai_service.keyword_prefilter` matches autoresponder markers
without an LLM, and :func:`app.services.ai_service.extract_return_date` parses
the return date out of the body. What was missing is the *response*.

The old behaviour left the enrollment ``active`` and pushed ``next_send_at``
forward. That works — the queue skips it until the date — but it is invisible:
every dashboard, every "active enrollments" count, and every operator reading
the pipeline sees a live sequence that is in fact frozen for two weeks. Worse,
anything that re-computes ``next_send_at`` (an adaptive-delay recalculation, a
manual re-queue, a resumed kill switch) silently undoes the hold and mails
someone who is on parental leave.

So an OOO now genuinely **pauses**: ``status='paused'``, ``pause_reason=
'out_of_office'``, and ``next_send_at`` carrying the resume date. The queue only
dispatches ``active``, so the hold is structural rather than a date that happens
to be in the future.

``pause_reason`` is what makes the automatic resume safe. A sweep that resumed
every paused enrollment whose date had passed would also resume the ones paused
for having no email address, a dead domain, a consent block, or because an
operator paused them on purpose. Only pauses this module created are ever undone
by this module.

Where no return date is parseable the resume is set ``OOO_DEFAULT_PAUSE_DAYS``
out — long enough to clear a typical absence, short enough that a mis-parse
costs a week of cadence rather than the contact.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment

logger = logging.getLogger(__name__)

# The marker on ``OutreachEnrollment.pause_reason`` that this module owns.
OOO_PAUSE_REASON = "out_of_office"

# Fallback hold when the autoresponder didn't say when they're back.
OOO_DEFAULT_PAUSE_DAYS = 7

# Never trust a parsed return date beyond this horizon. Autoresponders carry all
# sorts of dates — a signature block, a conference, a copyright year — and a
# mis-parse that lands in 2031 would retire the contact permanently. Anything
# past the cap falls back to the default hold.
OOO_MAX_PAUSE_DAYS = 90


def resolve_resume_at(
    return_date: Optional[str], *, now: Optional[datetime] = None
) -> datetime:
    """Turn a parsed return date into the moment to resume sending.

    Applies three guards, in order: unparseable → default hold; already in the
    past (a stale autoresponder, or a date parsed out of a signature) → default
    hold; absurdly far out → default hold. The result is always strictly in the
    future, so an OOO can only ever delay a sequence, never fire one immediately
    or shelve it forever.
    """
    now = now or datetime.now(timezone.utc)
    fallback = now + timedelta(days=OOO_DEFAULT_PAUSE_DAYS)

    if not return_date:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(return_date))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    if parsed <= now:
        return fallback
    if parsed > now + timedelta(days=OOO_MAX_PAUSE_DAYS):
        logger.info(
            "OOO return date %s is beyond the %d-day cap — using the default hold",
            return_date, OOO_MAX_PAUSE_DAYS,
        )
        return fallback
    return parsed


async def pause_for_ooo(
    db: AsyncSession,
    contact_id: Optional[UUID],
    *,
    return_date: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Pause a contact's live sequences until they're back.

    Only ``active`` enrollments are touched — an already-paused, completed,
    replied or unsubscribed enrollment is not something an autoresponder should
    revive. Returns ``{"paused": n, "resume_at": iso}``.
    """
    now = now or datetime.now(timezone.utc)
    resume_at = resolve_resume_at(return_date, now=now)

    if contact_id is None:
        return {"paused": 0, "resume_at": resume_at.isoformat()}

    enrollments = list(
        (
            await db.execute(
                select(OutreachEnrollment).where(
                    OutreachEnrollment.contact_id == contact_id,
                    OutreachEnrollment.status == "active",
                )
            )
        ).scalars().all()
    )
    for enr in enrollments:
        enr.status = "paused"
        enr.pause_reason = OOO_PAUSE_REASON
        enr.next_send_at = resume_at

    if enrollments:
        logger.info(
            "Paused %d enrollment(s) for contact %s until %s (out of office)",
            len(enrollments), contact_id, resume_at.isoformat(),
        )
    return {"paused": len(enrollments), "resume_at": resume_at.isoformat()}


async def resume_due(
    db: AsyncSession, *, now: Optional[datetime] = None, limit: int = 500
) -> dict:
    """Reactivate OOO-paused enrollments whose return date has arrived.

    Deliberately narrow: ``pause_reason == 'out_of_office'`` **and** status is
    still ``paused``. Anything paused for another reason, or moved on since (the
    contact replied, unsubscribed, bounced), is left exactly where it is.

    ``pause_reason`` is cleared on resume so a later pause-for-a-different-reason
    can't be un-paused by this sweep on the strength of a stale marker.

    Returns ``{"resumed": n, "enrollment_ids": [...]}``.
    """
    now = now or datetime.now(timezone.utc)

    due = list(
        (
            await db.execute(
                select(OutreachEnrollment)
                .where(
                    OutreachEnrollment.status == "paused",
                    OutreachEnrollment.pause_reason == OOO_PAUSE_REASON,
                    OutreachEnrollment.next_send_at.isnot(None),
                    OutreachEnrollment.next_send_at <= now,
                )
                .order_by(OutreachEnrollment.next_send_at.asc())
                .limit(limit)
            )
        ).scalars().all()
    )

    resumed_ids: list[str] = []
    for enr in due:
        enr.status = "active"
        enr.pause_reason = None
        # Due right now: the next queue sweep picks it up on its own cadence,
        # inside the send window and under the daily cap like any other send.
        enr.next_send_at = now
        resumed_ids.append(str(enr.id))

    if resumed_ids:
        logger.info("Resumed %d enrollment(s) after out-of-office", len(resumed_ids))
    return {"resumed": len(resumed_ids), "enrollment_ids": resumed_ids}
