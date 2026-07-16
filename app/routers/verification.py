"""Email verification API (P3 — Hunter.io).

Three operator-facing endpoints under ``/dialtone/contacts``:

* ``POST /dialtone/contacts/{contact_id}/verify`` — verify one contact now.
* ``POST /dialtone/contacts/bulk-verify``         — verify a set of contacts.
* ``GET  /dialtone/contacts/verification-stats``  — the status breakdown.

Both POSTs spend real Hunter credits, so they are rate-limited *and* run under
the same daily budget gate as the background sweeps. Normal operation shouldn't
need them at all — ``app.tasks.verification`` verifies contacts within 15
minutes of import — they exist for the "verify this one now" case and for
backfilling on demand.

Kept in its own module rather than bolted onto ``dialtone.py``: that router is
the read-only Console feed, and these mutate contacts.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func as sqla_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models.contact import Contact
from app.rate_limit import limiter
from app.schemas.verification import (
    BulkVerifyRequest,
    BulkVerifyResponse,
    VerificationResult,
    VerificationStats,
)
from app.services.hunter_verification import (
    DELIVERABLE,
    UNDELIVERABLE,
    get_hunter_service,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/dialtone", tags=["verification"])

# On-demand verification spends credits — throttle it well below the sweeps.
VERIFY_RATE_LIMIT = "20/minute"
BULK_VERIFY_RATE_LIMIT = "5/minute"


def _to_result(contact: Contact, verdict: dict, *, eligible: bool) -> VerificationResult:
    return VerificationResult(
        contact_id=contact.id,
        email=contact.email,
        status=verdict["status"],
        result=verdict["result"],
        score=verdict["score"],
        source=verdict["source"],
        email_status=contact.email_status,
        verified_at=contact.email_verified_at,
        reason=verdict["reason"],
        auto_enroll_eligible=eligible,
    )


@router.post("/contacts/{contact_id}/verify", response_model=VerificationResult)
@limiter.limit(VERIFY_RATE_LIMIT)
async def verify_contact_now(
    request: Request,
    contact_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Verify a single contact's email address on demand.

    Returns 200 even when Hunter is unreachable — the response carries
    ``status='error'`` with a ``reason`` and the contact is left untouched
    (fail-open). A 5xx here would imply the *contact* is bad rather than the
    upstream, which is exactly the confusion this capability exists to avoid.
    """
    result = await db.execute(select(Contact).where(Contact.id == contact_id))
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email address")

    service = get_hunter_service()
    verdict = await service.verify_contact(db, contact)
    await db.commit()

    return _to_result(
        contact, verdict, eligible=service.is_auto_enroll_eligible(contact)
    )


@router.post("/contacts/bulk-verify", response_model=BulkVerifyResponse)
@limiter.limit(BULK_VERIFY_RATE_LIMIT)
async def bulk_verify_contacts(
    request: Request,
    payload: BulkVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Verify a set of contacts under one shared credit budget.

    Unknown ids are ignored rather than fatal — a partial list shouldn't cost the
    caller the whole batch. Contacts the budget can't pay for come back
    ``skipped`` / ``budget_exhausted``, never silently dropped.
    """
    settings = get_settings()
    ids = list(dict.fromkeys(payload.contact_ids))  # de-dupe, preserve order
    if len(ids) > settings.hunter_max_batch:
        raise HTTPException(
            status_code=400,
            detail=f"Too many contacts: {len(ids)} (max {settings.hunter_max_batch})",
        )

    result = await db.execute(select(Contact).where(Contact.id.in_(ids)))
    found = list(result.scalars().all())
    contacts = [c for c in found if c.email]

    service = get_hunter_service()
    budget = await service.make_budget(db)
    verdicts = await service.verify_batch(db, contacts, budget=budget)
    await db.commit()

    results = [
        _to_result(contact, v, eligible=service.is_auto_enroll_eligible(contact))
        for contact, v in zip(contacts, verdicts)
    ]

    breakdown: dict[str, int] = {}
    for v in verdicts:
        key = v["result"] or v["status"]
        breakdown[key] = breakdown.get(key, 0) + 1

    verified = sum(1 for v in verdicts if v["status"] == "ok")
    return BulkVerifyResponse(
        requested=len(ids),
        found=len(found),
        verified=verified,
        skipped=len(verdicts) - verified,
        breakdown=breakdown,
        budget_remaining=(
            None if budget.remaining == float("inf") else int(budget.remaining)
        ),
        results=results,
    )


@router.get("/contacts/verification-stats", response_model=VerificationStats)
async def get_verification_stats(db: AsyncSession = Depends(get_db)):
    """Verification coverage and outcome breakdown across the contact list."""
    settings = get_settings()
    service = get_hunter_service()

    emailable_filters = (
        Contact.email.isnot(None),
        Contact.email != "",
        Contact.suppressed.is_(False),
        Contact.unsubscribed.is_(False),
    )

    async def _count(*where) -> int:
        result = await db.execute(
            select(sqla_func.count()).select_from(Contact).where(*where)
        )
        return int(result.scalar() or 0)

    async def _group(column) -> dict[str, int]:
        result = await db.execute(
            select(column, sqla_func.count())
            .select_from(Contact)
            .where(*emailable_filters)
            .group_by(column)
        )
        return {(key or "unchecked"): int(count) for key, count in result.all()}

    total = await _count()
    emailable = await _count(*emailable_filters)
    verified = await _count(*emailable_filters, Contact.email_verified_at.isnot(None))

    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.hunter_reverify_days)
    stale = await _count(
        *emailable_filters,
        Contact.email_verified_at.isnot(None),
        Contact.email_verified_at < cutoff,
        or_(Contact.email_status.is_(None), Contact.email_status != "invalid"),
    )

    eligible = await _count(
        *emailable_filters,
        Contact.email_verification_result == DELIVERABLE,
        Contact.email_verification_score.isnot(None),
        Contact.email_verification_score >= settings.hunter_min_score,
        or_(Contact.email_status.is_(None), Contact.email_status != "invalid"),
    )
    bounces_prevented = await _count(
        *emailable_filters, Contact.email_verification_result == UNDELIVERABLE
    )

    return VerificationStats(
        total_contacts=total,
        emailable=emailable,
        verified=verified,
        unverified=emailable - verified,
        stale=stale,
        by_email_status=await _group(Contact.email_status),
        by_result=await _group(Contact.email_verification_result),
        by_source=await _group(Contact.email_verification_source),
        auto_enroll_eligible=eligible,
        hunter_enabled=service.enabled,
        hunter_calls_today=await service.hunter_calls_today(db),
        daily_credit_budget=settings.hunter_daily_credit_budget,
        bounces_prevented=bounces_prevented,
        verified_undeliverable_rate=(
            round(bounces_prevented / verified, 4) if verified else 0.0
        ),
    )
