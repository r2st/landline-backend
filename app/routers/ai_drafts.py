"""Human review queue for AI-composed first touches.

The first ``ai_compose_review_batch_size`` AI emails per sequence are parked
here rather than sent (see :mod:`app.services.ai_compose`). This router is where
a human clears them.

Approving is what actually sends: the draft's copy is dispatched through the
same path a normal step uses, so tracking, threading and event recording behave
identically. Rejecting sends the static template the AI would have replaced,
because the prospect is mid-sequence and dropping a touch entirely is worse than
sending the copy a human already wrote.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.ai_email_draft import AiEmailDraft
from app.models.outreach import OutreachEnrollment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-drafts", tags=["ai-drafts"])


class AiDraftRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    enrollment_id: uuid.UUID
    sequence_id: Optional[uuid.UUID]
    contact_id: Optional[uuid.UUID]
    company_id: Optional[uuid.UUID]
    step_number: int
    subject: str
    body: str
    static_subject: Optional[str]
    static_body: Optional[str]
    ai_generated: bool
    research_used: bool
    status: str
    review_notes: Optional[str]
    created_at: datetime


class ReviewDecision(BaseModel):
    notes: Optional[str] = None


class AiDraftStats(BaseModel):
    """Review-queue health for the Dialtone dashboard.

    ``pending`` is the number of enrollments currently frozen on a human review
    (the metric the dashboard surfaces so a growing backlog is visible), plus a
    per-status breakdown and the age of the oldest pending draft — a large
    ``oldest_pending_age_hours`` is the signal that the auto-expiry sweep is
    about to release drafts to the static template.
    """

    pending: int
    approved: int
    rejected: int
    sent: int
    failed: int
    total: int
    oldest_pending_age_hours: Optional[float] = None


@router.get("/stats", response_model=AiDraftStats)
async def draft_stats(db: AsyncSession = Depends(get_db)):
    """Counts of AI drafts by status, plus the pending-review backlog age."""
    rows = (
        await db.execute(
            select(AiEmailDraft.status, func.count()).group_by(AiEmailDraft.status)
        )
    ).all()
    counts = {status: count for status, count in rows}

    oldest_age_hours: Optional[float] = None
    oldest = (
        await db.execute(
            select(func.min(AiEmailDraft.created_at)).where(
                AiEmailDraft.status == "pending"
            )
        )
    ).scalar_one_or_none()
    if oldest is not None:
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        oldest_age_hours = round(
            (datetime.now(timezone.utc) - oldest).total_seconds() / 3600.0, 2
        )

    return AiDraftStats(
        pending=counts.get("pending", 0),
        approved=counts.get("approved", 0),
        rejected=counts.get("rejected", 0),
        sent=counts.get("sent", 0),
        failed=counts.get("failed", 0),
        total=sum(counts.values()),
        oldest_pending_age_hours=oldest_age_hours,
    )


@router.get("", response_model=list[AiDraftRead])
async def list_drafts(
    status: str = Query("pending"),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List AI drafts, pending by default."""
    rows = (
        await db.execute(
            select(AiEmailDraft)
            .where(AiEmailDraft.status == status)
            .order_by(AiEmailDraft.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def _load_pending(db: AsyncSession, draft_id: uuid.UUID) -> AiEmailDraft:
    draft = (
        await db.execute(select(AiEmailDraft).where(AiEmailDraft.id == draft_id))
    ).scalar_one_or_none()
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft not found")
    if draft.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"Draft already {draft.status}"
        )
    return draft


@router.post("/{draft_id}/approve", response_model=AiDraftRead)
async def approve_draft(
    draft_id: uuid.UUID,
    decision: ReviewDecision | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Approve a draft and send it.

    Reactivates the enrollment and dispatches the step. The approved copy is
    handed to the send task, which applies tracking and threading as usual.
    """
    draft = await _load_pending(db, draft_id)
    enrollment = (
        await db.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == draft.enrollment_id)
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    draft.status = "approved"
    draft.reviewed_at = datetime.now(timezone.utc)
    if decision and decision.notes:
        draft.review_notes = decision.notes

    # Un-park the enrollment so the send task will process it, and send now.
    enrollment.status = "active"
    enrollment.next_send_at = datetime.now(timezone.utc)
    await db.commit()

    _dispatch(str(enrollment.id))
    logger.info("Draft %s approved — enrollment %s resumed", draft_id, enrollment.id)
    await db.refresh(draft)
    return draft


@router.post("/{draft_id}/reject", response_model=AiDraftRead)
async def reject_draft(
    draft_id: uuid.UUID,
    decision: ReviewDecision | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Reject a draft; the static template is sent in its place.

    The contact is mid-sequence, so dropping the touch outright is worse than
    sending the human-written copy the AI was trying to improve on. Marking the
    draft rejected also counts toward the sequence's review quota, so a sequence
    whose AI copy keeps getting rejected still stops queueing after N.
    """
    draft = await _load_pending(db, draft_id)
    enrollment = (
        await db.execute(
            select(OutreachEnrollment).where(OutreachEnrollment.id == draft.enrollment_id)
        )
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    draft.status = "rejected"
    draft.reviewed_at = datetime.now(timezone.utc)
    if decision and decision.notes:
        draft.review_notes = decision.notes

    enrollment.status = "active"
    enrollment.next_send_at = datetime.now(timezone.utc)
    await db.commit()

    _dispatch(str(enrollment.id))
    logger.info("Draft %s rejected — enrollment %s resumed on static copy", draft_id, enrollment.id)
    await db.refresh(draft)
    return draft


def _dispatch(enrollment_id: str) -> None:
    """Queue the send task, best-effort.

    Guarded because the API must not 500 when no Celery broker is reachable
    (tests, local runs). The periodic queue picks the enrollment up regardless
    once its status is active.
    """
    try:
        from app.tasks.outreach import send_sequence_step

        send_sequence_step.delay(enrollment_id)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Could not dispatch send for %s: %s", enrollment_id, exc)
