"""Outreach sequence management, enrollment, and event tracking endpoints."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.email_template import EmailSend
from app.models.sequence import OutreachSequence, SequenceStep
from app.schemas.outreach import (
    EnrollmentCreate,
    EnrollmentRead,
    EnrollmentUpdate,
    OutreachEventCreate,
    OutreachEventRead,
    SequenceCreate,
    SequenceRead,
    SequenceStepCreate,
    SequenceUpdate,
)
from app.tasks.outreach import send_sequence_step

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/outreach", tags=["outreach"])


# --- Sequences ---

@router.post("/sequences", response_model=SequenceRead, status_code=201)
async def create_sequence(
    payload: SequenceCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new outreach sequence with steps."""
    sequence = OutreachSequence(
        name=payload.name,
        description=payload.description,
    )
    db.add(sequence)
    await db.flush()

    for step_data in payload.steps:
        step = SequenceStep(
            sequence_id=sequence.id,
            **step_data.model_dump(),
        )
        db.add(step)

    await db.flush()
    await db.refresh(sequence)
    logger.info("Created sequence '%s' with %d steps", sequence.name, len(payload.steps))
    return sequence


@router.get("/sequences", response_model=list[SequenceRead])
async def list_sequences(
    active_only: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """List all outreach sequences."""
    query = select(OutreachSequence)
    if active_only:
        query = query.where(OutreachSequence.is_active == True)
    query = query.order_by(OutreachSequence.created_at.desc())

    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/sequences/{sequence_id}", response_model=SequenceRead)
async def get_sequence(
    sequence_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get a sequence with its steps."""
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == sequence_id)
    )
    sequence = result.scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    return sequence


@router.patch("/sequences/{sequence_id}", response_model=SequenceRead)
async def update_sequence(
    sequence_id: uuid.UUID,
    payload: SequenceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a sequence's name, description, or active status."""
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == sequence_id)
    )
    sequence = result.scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(sequence, field, value)

    await db.flush()
    await db.refresh(sequence)
    return sequence


@router.delete("/sequences/{sequence_id}", status_code=204)
async def delete_sequence(
    sequence_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete a sequence and all its steps and enrollments."""
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == sequence_id)
    )
    sequence = result.scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    await db.delete(sequence)
    await db.flush()


@router.post("/sequences/{sequence_id}/steps", status_code=201)
async def add_step(
    sequence_id: uuid.UUID,
    payload: SequenceStepCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a step to an existing sequence."""
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == sequence_id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Sequence not found")

    step = SequenceStep(sequence_id=sequence_id, **payload.model_dump())
    db.add(step)
    await db.flush()
    await db.refresh(step)
    return step


# --- Enrollments ---

@router.post("/enrollments", response_model=EnrollmentRead, status_code=201)
async def enroll_contact(
    payload: EnrollmentCreate,
    db: AsyncSession = Depends(get_db),
):
    """Enroll a contact in an outreach sequence."""
    # Verify contact
    result = await db.execute(
        select(Contact).where(Contact.id == payload.contact_id)
    )
    contact = result.scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if not contact.email:
        raise HTTPException(status_code=400, detail="Contact has no email address")

    # Verify sequence
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == payload.sequence_id)
    )
    sequence = result.scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    if not sequence.is_active:
        raise HTTPException(status_code=400, detail="Sequence is not active")

    # Check for existing active enrollment
    result = await db.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.contact_id == payload.contact_id,
            OutreachEnrollment.sequence_id == payload.sequence_id,
            OutreachEnrollment.status == "active",
        )
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail="Contact already enrolled in this sequence"
        )

    enrollment = OutreachEnrollment(
        contact_id=payload.contact_id,
        sequence_id=payload.sequence_id,
    )
    db.add(enrollment)
    await db.flush()
    await db.refresh(enrollment)

    # Dispatch first step
    send_sequence_step.delay(str(enrollment.id))

    logger.info(
        "Enrolled contact %s in sequence %s",
        payload.contact_id, payload.sequence_id,
    )
    return enrollment


@router.get("/enrollments", response_model=list[EnrollmentRead])
async def list_enrollments(
    contact_id: Optional[uuid.UUID] = Query(None),
    sequence_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List enrollments with optional filters."""
    query = select(OutreachEnrollment)

    if contact_id:
        query = query.where(OutreachEnrollment.contact_id == contact_id)
    if sequence_id:
        query = query.where(OutreachEnrollment.sequence_id == sequence_id)
    if status:
        query = query.where(OutreachEnrollment.status == status)

    query = query.order_by(OutreachEnrollment.enrolled_at.desc())
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    return list(result.scalars().all())


@router.patch("/enrollments/{enrollment_id}", response_model=EnrollmentRead)
async def update_enrollment(
    enrollment_id: uuid.UUID,
    payload: EnrollmentUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update enrollment status (pause, resume, etc.)."""
    result = await db.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id)
    )
    enrollment = result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    enrollment.status = payload.status
    await db.flush()
    await db.refresh(enrollment)
    return enrollment


# --- Events ---

@router.post("/events", response_model=OutreachEventRead, status_code=201)
async def record_event(
    payload: OutreachEventCreate,
    db: AsyncSession = Depends(get_db),
):
    """Record an outreach event (webhook from SES, manual tracking)."""
    result = await db.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == payload.enrollment_id)
    )
    enrollment = result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    event = OutreachEvent(**payload.model_dump())
    db.add(event)

    # Auto-update enrollment status based on event type
    if payload.event_type in ("replied", "bounced", "unsubscribed"):
        enrollment.status = payload.event_type

    await db.flush()
    await db.refresh(event)
    return event


@router.get("/events/{enrollment_id}", response_model=list[OutreachEventRead])
async def get_enrollment_events(
    enrollment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Get all events for an enrollment."""
    result = await db.execute(
        select(OutreachEvent)
        .where(OutreachEvent.enrollment_id == enrollment_id)
        .order_by(OutreachEvent.occurred_at.asc())
    )
    return list(result.scalars().all())


# --- Reply tracking stats ---

@router.get("/reply-stats")
async def get_reply_stats(db: AsyncSession = Depends(get_db)):
    """Get reply tracking statistics across all outreach emails."""

    # Count by status
    result = await db.execute(
        select(EmailSend.status, func.count(EmailSend.id)).group_by(EmailSend.status)
    )
    status_counts = dict(result.all())

    # Recent replies (last 10)
    result = await db.execute(
        select(EmailSend)
        .where(EmailSend.status == "replied")
        .order_by(EmailSend.replied_at.desc())
        .limit(10)
    )
    recent_replies = result.scalars().all()

    total_sent = sum(status_counts.values())
    replied = status_counts.get("replied", 0)

    return {
        "total_sent": total_sent,
        "replied": replied,
        "opened": status_counts.get("opened", 0),
        "clicked": status_counts.get("clicked", 0),
        "bounced": status_counts.get("bounced", 0),
        "reply_rate": round(replied / max(total_sent, 1) * 100, 1),
        "recent_replies": [
            {
                "id": str(r.id),
                "to_email": r.to_email,
                "subject": r.subject,
                "replied_at": r.replied_at.isoformat() if r.replied_at else None,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
            }
            for r in recent_replies
        ],
    }
