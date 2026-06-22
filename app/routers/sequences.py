"""Top-level sequence endpoints — listing with enrollment counts and batch
enrollment of contacts.

Complements the lower-level ``/outreach/sequences`` CRUD: this is the surface the
frontend's sequence picker and "enroll selected contacts" action call.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.schemas.outreach import (
    EnrollBatchRequest,
    EnrollBatchResult,
    EnrollSkip,
    SequenceWithCounts,
)

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
