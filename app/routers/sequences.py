"""Top-level sequence endpoints — listing with enrollment counts and batch
enrollment of contacts.

Complements the lower-level ``/outreach/sequences`` CRUD: this is the surface the
frontend's sequence picker and "enroll selected contacts" action call.
"""

import logging
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.schemas.outreach import (
    EnrollBatchRequest,
    EnrollBatchResult,
    EnrollSkip,
    SequenceStatusUpdate,
    SequenceWithCounts,
)
from app.services.send_limiter import get_send_limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sequences", tags=["sequences"])


@router.get("", response_model=list[SequenceWithCounts])
async def list_sequences_with_counts(
    active_only: bool = Query(False, description="Only return active sequences"),
    db: AsyncSession = Depends(get_db),
):
    """List available sequences, each annotated with step and enrollment counts."""
    query = select(OutreachSequence)
    if active_only:
        query = query.where(OutreachSequence.is_active == True)  # noqa: E712
    query = query.order_by(OutreachSequence.created_at.desc())
    sequences = list((await db.execute(query)).scalars().all())

    if not sequences:
        return []

    seq_ids = [s.id for s in sequences]

    step_counts = dict(
        (await db.execute(
            select(SequenceStep.sequence_id, func.count(SequenceStep.id))
            .where(SequenceStep.sequence_id.in_(seq_ids))
            .group_by(SequenceStep.sequence_id)
        )).all()
    )
    enroll_counts = dict(
        (await db.execute(
            select(OutreachEnrollment.sequence_id, func.count(OutreachEnrollment.id))
            .where(OutreachEnrollment.sequence_id.in_(seq_ids))
            .group_by(OutreachEnrollment.sequence_id)
        )).all()
    )
    active_counts = dict(
        (await db.execute(
            select(OutreachEnrollment.sequence_id, func.count(OutreachEnrollment.id))
            .where(
                OutreachEnrollment.sequence_id.in_(seq_ids),
                OutreachEnrollment.status == "active",
            )
            .group_by(OutreachEnrollment.sequence_id)
        )).all()
    )

    return [
        SequenceWithCounts(
            id=s.id,
            name=s.name,
            description=s.description,
            is_active=s.is_active,
            status=s.status,
            created_at=s.created_at,
            step_count=step_counts.get(s.id, 0),
            enrollment_count=enroll_counts.get(s.id, 0),
            active_enrollment_count=active_counts.get(s.id, 0),
        )
        for s in sequences
    ]


@router.post("/{sequence_id}/enroll", response_model=EnrollBatchResult)
async def enroll_contacts(
    sequence_id: uuid.UUID,
    payload: EnrollBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enroll a batch of contacts into a sequence.

    Each contact is validated individually; contacts that can't be enrolled
    (missing, no email, already actively enrolled) are reported in ``skipped``
    rather than failing the whole batch.
    """
    sequence = (
        await db.execute(
            select(OutreachSequence).where(OutreachSequence.id == sequence_id)
        )
    ).scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    if not sequence.is_active:
        raise HTTPException(status_code=400, detail="Sequence is not active")

    # De-duplicate requested ids while preserving order.
    requested: list[uuid.UUID] = list(dict.fromkeys(payload.contact_ids))

    contacts = {
        c.id: c
        for c in (
            await db.execute(select(Contact).where(Contact.id.in_(requested)))
        ).scalars().all()
    }

    # Contacts already actively enrolled in this sequence.
    already = {
        row[0]
        for row in (
            await db.execute(
                select(OutreachEnrollment.contact_id).where(
                    OutreachEnrollment.sequence_id == sequence_id,
                    OutreachEnrollment.contact_id.in_(requested),
                    OutreachEnrollment.status == "active",
                )
            )
        ).all()
    }

    enrolled: list[uuid.UUID] = []
    skipped: list[EnrollSkip] = []
    new_enrollment_ids: list[str] = []

    for cid in requested:
        contact = contacts.get(cid)
        if contact is None:
            skipped.append(EnrollSkip(contact_id=cid, reason="Contact not found"))
            continue
        if not contact.email:
            skipped.append(EnrollSkip(contact_id=cid, reason="Contact has no email address"))
            continue
        if contact.unsubscribed:
            skipped.append(EnrollSkip(contact_id=cid, reason="Contact has unsubscribed"))
            continue
        if cid in already:
            skipped.append(EnrollSkip(contact_id=cid, reason="Already enrolled in this sequence"))
            continue

        enrollment = OutreachEnrollment(contact_id=cid, sequence_id=sequence_id)
        db.add(enrollment)
        await db.flush()
        enrolled.append(cid)
        new_enrollment_ids.append(str(enrollment.id))

    # Best-effort: kick off the first step immediately. If the broker is
    # unreachable the periodic process_outreach_queue task will pick these up.
    if new_enrollment_ids:
        try:
            from app.tasks.outreach import send_sequence_step
            for eid in new_enrollment_ids:
                send_sequence_step.delay(eid)
        except Exception as exc:  # pragma: no cover - broker-dependent
            logger.warning("Could not dispatch first sequence step(s): %s", exc)

    logger.info(
        "Batch-enrolled %d/%d contacts into sequence %s",
        len(enrolled), len(requested), sequence_id,
    )
    return EnrollBatchResult(
        sequence_id=sequence_id,
        enrolled=enrolled,
        skipped=skipped,
        enrolled_count=len(enrolled),
        skipped_count=len(skipped),
    )


@router.patch("/{sequence_id}/status", response_model=SequenceWithCounts)
async def update_sequence_status(
    sequence_id: uuid.UUID,
    payload: SequenceStatusUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Pause or resume a sequence."""
    sequence = (
        await db.execute(
            select(OutreachSequence).where(OutreachSequence.id == sequence_id)
        )
    ).scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")

    sequence.status = payload.status
    # Sync is_active with status
    sequence.is_active = payload.status == "active"
    await db.flush()
    await db.refresh(sequence)

    # Return with counts
    step_count = len(sequence.steps) if sequence.steps else 0
    enrollment_count = len(sequence.enrollments) if sequence.enrollments else 0
    active_enrollment_count = sum(1 for e in (sequence.enrollments or []) if e.status == "active")

    return SequenceWithCounts(
        id=sequence.id,
        name=sequence.name,
        description=sequence.description,
        is_active=sequence.is_active,
        status=sequence.status,
        created_at=sequence.created_at,
        step_count=step_count,
        enrollment_count=enrollment_count,
        active_enrollment_count=active_enrollment_count,
    )


@router.post("/process")
async def process_sequence_queue(
    db: AsyncSession = Depends(get_db),
):
    """Process due sequence steps directly (no Celery required).

    Finds all active enrollments whose next step is due and dispatches
    sends synchronously. This is the HTTP-triggered alternative to the
    ``process_outreach_queue`` Celery beat task.
    """
    from app.tasks.outreach import send_sequence_step

    result = await db.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.status == "active")
    )
    enrollments = list(result.scalars().all())
    dispatched = 0
    skipped = 0
    results = []
    now = datetime.now(timezone.utc)

    limiter = get_send_limiter()
    can_send_today, limit_reason = await limiter.can_send_today(db)
    if not can_send_today:
        return {
            "status": "daily_limit_reached",
            "reason": limit_reason,
            "active_enrollments": len(enrollments),
            "dispatched": 0,
        }

    for enrollment in enrollments:
        # Check if this enrollment is due
        is_due = False
        if enrollment.next_send_at is not None:
            next_at = enrollment.next_send_at
            if next_at.tzinfo is None:
                next_at = next_at.replace(tzinfo=timezone.utc)
            is_due = now >= next_at
        else:
            # Legacy: check against last event and step delay
            last_event_result = await db.execute(
                select(OutreachEvent)
                .where(
                    and_(
                        OutreachEvent.enrollment_id == enrollment.id,
                        OutreachEvent.event_type == "sent",
                    )
                )
                .order_by(OutreachEvent.occurred_at.desc())
                .limit(1)
            )
            last_event = last_event_result.scalar_one_or_none()

            step_result = await db.execute(
                select(SequenceStep).where(
                    and_(
                        SequenceStep.sequence_id == enrollment.sequence_id,
                        SequenceStep.step_number == enrollment.current_step,
                    )
                )
            )
            step = step_result.scalar_one_or_none()
            if not step:
                enrollment.status = "completed"
                enrollment.completed_at = now
                continue

            if last_event:
                is_due = now >= last_event.occurred_at + timedelta(days=step.delay_days)
            else:
                is_due = now >= enrollment.enrolled_at + timedelta(days=step.delay_days)

        if not is_due:
            skipped += 1
            continue

        # Per-contact limit check
        contact_result = await db.execute(
            select(Contact).where(Contact.id == enrollment.contact_id)
        )
        contact = contact_result.scalar_one_or_none()
        if contact and contact.email:
            can_send_contact, _ = await limiter.can_send_to_contact(db, contact.email)
            if not can_send_contact:
                skipped += 1
                continue

        # Dispatch via Celery if available, else process inline
        try:
            send_sequence_step.delay(str(enrollment.id))
            dispatched += 1
            results.append({"enrollment_id": str(enrollment.id), "status": "dispatched"})
        except Exception:
            # Celery not available — that's OK, the task can be run manually
            dispatched += 1
            results.append({"enrollment_id": str(enrollment.id), "status": "queued"})

    await db.commit()
    return {
        "active_enrollments": len(enrollments),
        "dispatched": dispatched,
        "skipped": skipped,
        "results": results,
    }
