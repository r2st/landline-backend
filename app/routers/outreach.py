"""Outreach sequence management, enrollment, and event tracking endpoints."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user
from app.org_context import org_scope
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.email_template import (
    DELIVERED_STATUSES,
    EmailSend,
    WIRE_STATUSES,
)
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
from app.services.webhook_dispatch import SIGNATURE_HEADER, verify_payload
from app.tasks.outreach import send_sequence_step

logger = logging.getLogger(__name__)
# Authenticated router — every route requires a valid, active user.
router = APIRouter(
    prefix="/outreach",
    tags=["outreach"],
    dependencies=[Depends(get_current_user)],
)
# Router for the event endpoint, which is reachable *either* with a JWT (an
# operator recording a touch by hand) or with an HMAC signature (an ESP posting
# delivery events). It is mounted without the router-level auth dependency so
# the second caller can exist at all; the per-route dependency below is what
# actually admits a request. See ``require_signed_webhook_or_user``.
public_router = APIRouter(prefix="/outreach", tags=["outreach"])


_webhook_bearer = HTTPBearer(auto_error=False)


async def require_signed_webhook_or_user(
    request: Request,
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_webhook_bearer),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Admit an ESP webhook (HMAC-signed body) or a logged-in user. Nothing else.

    ``POST /outreach/events`` was fully anonymous, on the stated grounds that an
    SES webhook must reach it without a JWT and that the ``enrollment_id``
    foreign key was guard enough. Both halves were wrong.

    **The FK is not a guard, because of what the rows it gates are used for.**
    An ``OutreachEvent`` is not an annotation on a campaign; it is the send
    ledger. ``SendLimiter._db_sent_today`` counts ``event_type='sent'`` rows to
    decide how much of the daily cap is spent — and the Redis reservation
    counter *seeds itself from that count* — so posting ``sent`` events spends
    the day's outreach without sending anything. ``DangerMonitor`` reads
    ``bounced`` rows for the bounce rate that trips the system-wide kill switch.
    And ``replied``/``bounced``/``unsubscribed`` are written straight onto
    ``enrollment.status`` a few lines below, ending the cadence. An anonymous
    caller holding one enrollment id could stop one campaign, and — because the
    cap and the kill switch are global — stop all of them.

    **And no SES webhook was ever posting here.** SES notifies over SNS, whose
    envelope is ``{"Type": "Notification", "Message": "<json string>", ...}``
    with its own certificate-based signature. Nothing in this codebase parses
    that shape; this endpoint takes ``{enrollment_id, step_number, event_type}``
    and would reject a genuine SNS delivery as a 422. The exemption was held
    open for a caller that could not have used it.

    So: an ESP proves itself with an HMAC over the raw body, using the same
    scheme and header we ask *our* webhook subscribers to verify
    (:func:`app.services.webhook_dispatch.sign_payload`); an operator proves
    itself with a JWT, which is how the manual-tracking half of this endpoint's
    docstring was always supposed to work. Returns the actor kind for logging.

    A present-but-wrong signature is rejected outright rather than falling
    through to the JWT check — a caller that signed and got it wrong is not an
    anonymous caller, and letting it retry unsigned would make the header
    optional in practice.

    The JWT is resolved through :func:`get_current_user` directly rather than
    ``get_current_user_optional``: "optional" carries a second meaning here (a
    legacy unscoped API-key caller resolves to ``None``), and the question this
    dependency asks is only whether the bearer token is a valid one.
    """
    signature = request.headers.get(SIGNATURE_HEADER)
    if signature:
        secret = get_settings().outreach_webhook_secret
        if verify_payload(secret, await request.body(), signature):
            return "webhook"
        # Covers both a bad digest and a signature offered when no secret is
        # configured; the caller learns which only from its own records.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )
    if creds is not None:
        await get_current_user(creds, db)  # raises 401 if invalid / deactivated
        return "user"
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


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
        tone=payload.tone,
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

    # The same do-not-contact test the send gate makes, asked before the row
    # exists rather than after the task has been dispatched. This endpoint
    # checked only for an address — not the two opt-out flags the bulk endpoint
    # next door checks, and not the org list that neither of them did — so the
    # narrowest way to enrol a contact was also the least guarded. It ends by
    # dispatching ``send_sequence_step`` immediately, which is what turns the
    # omission into a task per opted-out contact: refused at the gate, then
    # closed, having reported a live enrollment in between.
    from app.services import suppression as _suppression

    blocked = await _suppression.blocking_reason(db, contact)
    if blocked is not None:
        raise HTTPException(status_code=400, detail=blocked)

    # Verify sequence
    result = await db.execute(
        select(OutreachSequence).where(OutreachSequence.id == payload.sequence_id)
    )
    sequence = result.scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    if not sequence.is_active:
        raise HTTPException(status_code=400, detail="Sequence is not active")

    # Check for existing active enrollment. An existence check needs one row,
    # not the assertion that only one exists: nothing in the schema stops a
    # second active enrollment for the same pair (no unique constraint), and
    # two concurrent calls to this very endpoint are enough to create one. A
    # ``scalar_one_or_none`` would then answer "already enrolled?" with a 500.
    result = await db.execute(
        select(OutreachEnrollment.id)
        .where(
            OutreachEnrollment.contact_id == payload.contact_id,
            OutreachEnrollment.sequence_id == payload.sequence_id,
            OutreachEnrollment.status == "active",
        )
        .limit(1)
    )
    if result.scalars().first():
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

@public_router.post("/events", response_model=OutreachEventRead, status_code=201)
async def record_event(
    payload: OutreachEventCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(require_signed_webhook_or_user),
):
    """Record an outreach event (signed ESP webhook, or manual tracking).

    Reachable without a JWT only by an ESP that signs the body with
    ``OUTREACH_WEBHOOK_SECRET``; a logged-in operator may post here directly.
    The ``enrollment_id`` foreign key still scopes the write to an enrollment
    that exists — it is just not, on its own, an authorization check. See
    :func:`require_signed_webhook_or_user`.

    **The webhook half binds no org, so the event must name one.** A JWT caller
    arrives with ``org_id`` bound by ``get_current_user``, which both scopes the
    lookup below and stamps the insert. A signed webhook arrives with nothing
    bound — the route is in ``PUBLIC_ROUTES`` precisely so an ESP can reach it —
    and ``org_id_for_insert()`` then falls back to the first-party org. Every
    delivery event for every tenant was therefore filed under Apprend: the
    tenant that actually sent the mail never saw its own bounces (the read
    filter scopes them away), while ``SendLimiter._db_sent_today`` and
    ``DangerMonitor`` counted a stranger's ``sent`` and ``bounced`` rows against
    *our* daily cap and *our* bounce rate. Same failure the tracking pixel had
    and ``opt_out.record`` documents: an unauthenticated writer has to carry the
    org on the row, because there is no context to infer it from.

    **An ESP redelivers, so a repeat is not an error.** Webhook delivery is
    at-least-once by design, and migration 117 made ``opened``/``clicked`` unique
    per ``(enrollment, step)``. A second delivery of the same open therefore hit
    the index and came back a 500 — which every ESP reads as "not accepted" and
    retries, indefinitely, on a body that can never be accepted. The duplicate is
    answered with the row that already exists: idempotent is what a webhook
    receiver is supposed to be, and it is the same "first occurrence wins"
    ``_record_outreach_engagement`` applies to the pixel.
    """
    result = await db.execute(
        select(OutreachEnrollment).where(OutreachEnrollment.id == payload.enrollment_id)
    )
    enrollment = result.scalar_one_or_none()
    if not enrollment:
        raise HTTPException(status_code=404, detail="Enrollment not found")

    # Bind the enrollment's own org for the write. Correct on both paths: the
    # JWT caller's org already equals this one (the scoped read above is what
    # proved it), and the webhook caller had none.
    async with org_scope(enrollment.org_id):
        event = OutreachEvent(org_id=enrollment.org_id, **payload.model_dump())
        try:
            # Inside the savepoint, so losing the race takes the row back and
            # leaves the session usable — the same shape ``_record_outreach_
            # engagement`` uses for the pixel. Rolling the savepoint back
            # expunges the pending instance for us.
            async with db.begin_nested():
                db.add(event)
                await db.flush()
        except IntegrityError:
            existing = (
                await db.execute(
                    select(OutreachEvent).where(
                        OutreachEvent.enrollment_id == enrollment.id,
                        OutreachEvent.step_number == payload.step_number,
                        OutreachEvent.event_type == payload.event_type,
                    ).limit(1)
                )
            ).scalars().first()
            if existing is None:  # pragma: no cover - a constraint we don't model
                raise
            logger.info(
                "Duplicate %s for enrollment %s step %s (actor=%s) — returning the "
                "recorded event", payload.event_type, enrollment.id,
                payload.step_number, actor,
            )
            return existing

        # Auto-update enrollment status based on event type
        if payload.event_type in ("replied", "bounced", "unsubscribed"):
            # Worth a line: this is the one path where an *external* caller ends
            # a cadence, so "who stopped this enrollment" should be answerable
            # later.
            logger.info(
                "Enrollment %s → %s (actor=%s)", enrollment.id, payload.event_type, actor,
            )
            enrollment.status = payload.event_type

        await db.flush()
        await db.refresh(event)

        # An ESP-reported unsubscribe is an opt-out, and this was the seventh
        # do-not-contact writer that only wrote *one* enrollment.
        #
        # Per "Every do-not-contact writer writes the list": the six known
        # writers (footer link, one-click POST, a reply saying unsubscribe, a
        # complaint, a hard bounce, an erasure) all set the contact flags, write
        # the revoked consent record, stop **every** actionable enrollment and
        # add the address to the org suppression list. This one set
        # ``enrollment.status`` and stopped. Three things follow, and none of
        # them raise:
        #
        # * the contact's *other* live sequences kept their schedule, so the
        #   recipient who told the ESP to stop went on receiving our mail from a
        #   different campaign;
        # * ``contact.suppressed`` stayed False, so every gate that reads the row
        #   — the enrollment gates, ``check_send_allowed`` — waved the address
        #   through;
        # * nothing address-keyed was recorded at all, so a re-scrape next month
        #   creates a clean row and the cadence starts over.
        #
        # Deliberately only ``unsubscribed``. ``bounced`` is ambiguous here (an
        # ESP reports soft and hard alike with one event type) and belongs to
        # ``bounce_handler``, which is what decides the kind; ``replied`` is not
        # a do-not-contact decision. ``opt_out.record`` is idempotent, and its
        # campaign attribution asks whether *this contact* already carries an
        # ``unsubscribed`` event — the one written a few lines up — rather than
        # whether one particular enrollment does, so neither a redelivery nor a
        # contact enrolled in two campaigns books the opt-out twice.
        if payload.event_type == "unsubscribed":
            await _record_external_opt_out(db, enrollment)

        return event


async def _record_external_opt_out(db: AsyncSession, enrollment) -> None:
    """Run the full opt-out for the contact behind an ESP unsubscribe event.

    Best-effort and non-fatal: the event row is the receipt the ESP is waiting
    for, and answering a delivered webhook with a 500 makes it redeliver forever.
    A failure here is loud in the log and leaves the enrollment stopped, which is
    what the handler already did on its own.
    """
    contact_id = getattr(enrollment, "contact_id", None)
    if contact_id is None:
        return
    email = (
        await db.execute(select(Contact.email).where(Contact.id == contact_id))
    ).scalars().first()
    if not email:
        return
    try:
        from app.services import opt_out

        await opt_out.record(
            db, email, org_id=enrollment.org_id, reason="unsubscribe",
            source="auto", basis="ESP unsubscribe event",
        )
    except Exception as exc:  # noqa: BLE001 — never fail the webhook receipt
        logger.error(
            "Could not record the opt-out for enrollment %s (%s): %s",
            enrollment.id, email, exc,
        )


@router.get("/events")
async def list_events(
    company_id: Optional[uuid.UUID] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List outreach events, optionally scoped to a company or contact.

    Returns events newest-first. Each row carries both the canonical
    (``event_type``/``occurred_at``) and the UI-friendly (``type``/``created_at``)
    field names so the company-timeline and activity-feed views render without
    a field-name mismatch.
    """
    query = (
        select(OutreachEvent)
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .join(Contact, OutreachEnrollment.contact_id == Contact.id)
    )
    if company_id is not None:
        query = query.where(Contact.company_id == company_id)
    if contact_id is not None:
        query = query.where(OutreachEnrollment.contact_id == contact_id)
    query = query.order_by(OutreachEvent.occurred_at.desc()).limit(limit)

    events = list((await db.execute(query)).scalars().all())
    return [
        {
            "id": str(e.id),
            "enrollment_id": str(e.enrollment_id),
            "step_number": e.step_number,
            "type": e.event_type,
            "event_type": e.event_type,
            "subject": (e.event_metadata or {}).get("subject"),
            "occurred_at": e.occurred_at,
            "created_at": e.occurred_at,
            "metadata": e.event_metadata or {},
        }
        for e in events
    ]


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
    """Reply-tracking statistics across outreach sends recorded in ``email_sends``.

    Every number here used to come out of one ``GROUP BY status``, which is the
    wrong source twice over.

    **``status`` is a lifecycle column, not a set of tags.** It carries the
    furthest state a send reached — the tracking endpoints upgrade it and never
    downgrade it — so a message that was opened and then replied to is
    ``replied`` and appears under no other key. Counting ``status == 'opened'``
    therefore counted opens that *stopped* at an open. It was a measure of
    disengagement flying the name of a measure of engagement, and it fell as a
    campaign got better. The timestamp columns are the tags: ``opened_at`` is
    set on the first open and stays set through every later state.

    **``sum(status_counts.values())`` counted every row.** Nothing filtered the
    denominator, so `pending`, `skipped`, `error`, `failed` and `scheduled` were
    all "sent" — a suppressed recipient, a spam-gate block, a spent daily cap
    and a batch queued for next Thursday each made the reply rate worse. That is
    the direction it must not be wrong in: the denominator grows fastest exactly
    when sending is being *prevented*, so the operator reads a collapsing reply
    rate and blames the copy.

    Denominators follow the same split as the campaign summary in
    ``email_templates``: volume is ``WIRE_STATUSES`` (a bounce was mail that
    went out), engagement rates divide by ``DELIVERED_STATUSES`` (a bounce was
    never delivered, so it is a list problem, not a copy problem).
    """

    async def _count(*criteria) -> int:
        return (
            await db.execute(select(func.count(EmailSend.id)).where(*criteria))
        ).scalar() or 0

    total_sent = await _count(EmailSend.status.in_(WIRE_STATUSES))
    delivered = await _count(EmailSend.status.in_(DELIVERED_STATUSES))
    opened = await _count(EmailSend.opened_at.isnot(None))
    clicked = await _count(EmailSend.clicked_at.isnot(None))
    replied = await _count(EmailSend.replied_at.isnot(None))
    bounced = await _count(EmailSend.status == "bounced")

    # Recent replies (last 10). Keyed on ``replied_at``, not on the status: a
    # send that replied and then bounced a later message in the same thread
    # would drop off this list while its reply timestamp stands.
    result = await db.execute(
        select(EmailSend)
        .where(EmailSend.replied_at.isnot(None))
        .order_by(EmailSend.replied_at.desc())
        .limit(10)
    )
    recent_replies = result.scalars().all()

    return {
        "total_sent": total_sent,
        "replied": replied,
        "opened": opened,
        "clicked": clicked,
        "bounced": bounced,
        "reply_rate": round(replied / delivered * 100, 1) if delivered else 0.0,
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
