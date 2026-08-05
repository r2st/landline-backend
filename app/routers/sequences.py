"""Top-level sequence endpoints — listing with enrollment counts and batch
enrollment of contacts.

Complements the lower-level ``/outreach/sequences`` CRUD: this is the surface the
frontend's sequence picker and "enroll selected contacts" action call.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.schemas.outreach import (
    EnrollBatchRequest,
    EnrollBatchResult,
    EnrollByIndustryRequest,
    EnrollSkip,
    IndustryVerticalRead,
    SequenceStatusUpdate,
    SequenceWithCounts,
    SuggestIndustryResult,
)
from app.services.industry_verticals import (
    all_verticals,
    get_vertical,
    resolve_vertical,
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
            industry=s.industry,
            is_active=s.is_active,
            status=s.status,
            tone=s.tone,
            created_at=s.created_at,
            step_count=step_counts.get(s.id, 0),
            enrollment_count=enroll_counts.get(s.id, 0),
            active_enrollment_count=active_counts.get(s.id, 0),
        )
        for s in sequences
    ]


async def _enroll_into_sequence(
    db: AsyncSession,
    sequence: OutreachSequence,
    contact_ids: list[uuid.UUID],
) -> EnrollBatchResult:
    """Enroll a batch of contacts into an already-resolved, active sequence.

    Each contact is validated individually; contacts that can't be enrolled
    (missing, no email, unsubscribed, already actively enrolled) are reported in
    ``skipped`` rather than failing the whole batch. Shared by the enroll and
    enroll-by-industry endpoints.
    """
    sequence_id = sequence.id

    # De-duplicate requested ids while preserving order.
    requested: list[uuid.UUID] = list(dict.fromkeys(contact_ids))

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

    # Addresses on the org do-not-contact list, in one query for the batch.
    #
    # The third clause of the send gate's test, and the one this handler was
    # missing: it read ``unsubscribed`` and ``suppressed`` off the contact row
    # and stopped. ``suppression.add_entry`` does not touch those flags — an
    # address outlives every contact row that carries it — so a compliance CSV
    # upload, a bounce against a re-scraped duplicate, and an erasure whose
    # subject was later rediscovered all present as a contact with clean flags
    # that must never be mailed. Enrolling them cost a dispatch and a refusal
    # each, and reported a campaign of live outreach that could never send.
    #
    # Batched deliberately: the flag checks below run first and this only has to
    # ask about the addresses that survive them, which keeps a full page of
    # contacts at one query rather than the per-row existence check the rest of
    # this handler goes out of its way to avoid.
    from app.services import suppression as _suppression

    listed = await _suppression.suppressed_addresses(
        db,
        [
            c.email for c in contacts.values()
            if _suppression.blocking_flag(c) is None
        ],
    )

    enrolled: list[uuid.UUID] = []
    skipped: list[EnrollSkip] = []
    new_enrollment_ids: list[str] = []

    for cid in requested:
        contact = contacts.get(cid)
        if contact is None:
            skipped.append(EnrollSkip(contact_id=cid, reason="Contact not found"))
            continue
        flag_reason = _suppression.blocking_flag(contact)
        if flag_reason is not None:
            skipped.append(EnrollSkip(contact_id=cid, reason=flag_reason))
            continue
        if _suppression.normalize_email(contact.email) in listed:
            skipped.append(EnrollSkip(contact_id=cid, reason=_suppression.ON_LIST))
            continue
        if cid in already:
            skipped.append(EnrollSkip(contact_id=cid, reason="Already enrolled in this sequence"))
            continue

        # Assign the id here rather than letting the column default supply it at
        # INSERT time. The default is Python-side, so it only lands on flush —
        # which is why this loop used to flush every iteration just to read
        # ``enrollment.id`` back. That made a batch of N contacts N round-trips
        # to the database instead of one. Generating the uuid up front is the
        # same value from the same function, minus the round-trips.
        enrollment = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=cid, sequence_id=sequence_id
        )
        db.add(enrollment)
        enrolled.append(cid)
        new_enrollment_ids.append(str(enrollment.id))

    # One flush for the whole batch. This must happen before the dispatches
    # below: the workers look the enrollment up by id, so the rows have to exist
    # in this transaction first.
    if enrolled:
        await db.flush()

    # Best-effort: kick off the first step immediately. If the broker is
    # unreachable the periodic process_outreach_queue task will pick these up.
    if new_enrollment_ids:
        try:
            from app.tasks.outreach import send_sequence_step
            for eid in new_enrollment_ids:
                send_sequence_step.delay(eid)
        except Exception as exc:  # pragma: no cover - broker-dependent
            logger.warning("Could not dispatch first sequence step(s): %s", exc)

        # Warm up a research brief for each enrolled prospect's company so the
        # first AI-composed touch is grounded in real signals (see ai_compose /
        # research_agent). research_company is idempotent — force=False returns
        # an existing non-stale brief untouched — so dispatching per distinct
        # company is cheap and safe. Best-effort: a missing broker or an already
        # -briefed company just means the static/optimizer path runs as before.
        company_ids = {
            contacts[cid].company_id
            for cid in enrolled
            if contacts.get(cid) is not None and contacts[cid].company_id is not None
        }
        if company_ids:
            try:
                from app.tasks.research import research_company
                for company_id in company_ids:
                    research_company.delay(str(company_id))
            except Exception as exc:  # pragma: no cover - broker-dependent
                logger.warning("Could not dispatch research brief(s) on enroll: %s", exc)

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


@router.post("/{sequence_id}/enroll", response_model=EnrollBatchResult)
async def enroll_contacts(
    sequence_id: uuid.UUID,
    payload: EnrollBatchRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enroll a batch of contacts into a sequence by id."""
    sequence = (
        await db.execute(
            select(OutreachSequence).where(OutreachSequence.id == sequence_id)
        )
    ).scalar_one_or_none()
    if not sequence:
        raise HTTPException(status_code=404, detail="Sequence not found")
    if not sequence.is_active:
        raise HTTPException(status_code=400, detail="Sequence is not active")
    return await _enroll_into_sequence(db, sequence, payload.contact_ids)


# ── Industry verticals ──────────────────────────────────────────────────────


async def _vertical_sequence_ids(db: AsyncSession) -> dict[str, OutreachSequence]:
    """Map each seeded sequence by its industry key (most-recently-created wins)."""
    rows = (
        await db.execute(
            select(OutreachSequence)
            .where(OutreachSequence.industry.isnot(None))
            .order_by(OutreachSequence.created_at.asc())
        )
    ).scalars().all()
    return {s.industry: s for s in rows}


@router.get("/industries", response_model=list[IndustryVerticalRead])
async def list_industries(db: AsyncSession = Depends(get_db)):
    """List supported industry verticals and their seeded sequences.

    Powers the industry picker: each entry carries the vertical's pain-point
    language, demo link, and the id of the sequence to enroll contacts into.
    """
    seq_by_industry = await _vertical_sequence_ids(db)
    result = []
    for v in all_verticals():
        seq = seq_by_industry.get(v.key)
        result.append(
            IndustryVerticalRead(
                key=v.key,
                name=v.name,
                description=v.description,
                demo_link=v.demo_link,
                pain_points=list(v.pain_points),
                keywords=list(v.keywords),
                step_count=len(v.steps),
                sequence_id=seq.id if seq else None,
                sequence_name=v.sequence_name,
            )
        )
    return result


@router.post("/enroll-by-industry", response_model=EnrollBatchResult)
async def enroll_by_industry(
    payload: EnrollByIndustryRequest,
    db: AsyncSession = Depends(get_db),
):
    """Enroll contacts into the sequence for a given industry vertical.

    Resolves the industry key to its seeded sequence, then enrolls — so callers
    pick an industry instead of needing to know sequence ids.
    """
    vertical = get_vertical(payload.industry)
    if vertical is None:
        known = ", ".join(v.key for v in all_verticals())
        raise HTTPException(
            status_code=404,
            detail=f"Unknown industry '{payload.industry}'. Known: {known}",
        )
    seq_by_industry = await _vertical_sequence_ids(db)
    sequence = seq_by_industry.get(vertical.key)
    if sequence is None:
        raise HTTPException(
            status_code=404,
            detail=f"No seeded sequence for industry '{vertical.key}' yet — run migrations",
        )
    if not sequence.is_active:
        raise HTTPException(status_code=400, detail="Sequence is not active")
    return await _enroll_into_sequence(db, sequence, payload.contact_ids)


@router.get("/suggest-industry", response_model=SuggestIndustryResult)
async def suggest_industry(
    company_id: Optional[uuid.UUID] = Query(None, description="Resolve from this company"),
    industry: Optional[str] = Query(None, description="Or pass raw industry text"),
    company_name: Optional[str] = Query(None, description="Optional company name hint"),
    db: AsyncSession = Depends(get_db),
):
    """Suggest the best industry vertical (and its sequence) for a company.

    Pass a ``company_id`` to resolve from its stored industry/name, or pass raw
    ``industry``/``company_name`` text directly. Used to auto-select the right
    templates when enrolling a contact.
    """
    if company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == company_id))
        ).scalar_one_or_none()
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        industry = industry or company.industry_segment or ""
        company_name = company_name or company.name or ""

    vertical = resolve_vertical(industry or "", company_name or "")
    if vertical is None:
        return SuggestIndustryResult(matched=False)

    seq = (await _vertical_sequence_ids(db)).get(vertical.key)
    return SuggestIndustryResult(
        industry=vertical.key,
        name=vertical.name,
        matched=True,
        sequence_id=seq.id if seq else None,
        sequence_name=vertical.sequence_name,
        demo_link=vertical.demo_link,
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
        industry=sequence.industry,
        is_active=sequence.is_active,
        status=sequence.status,
        tone=sequence.tone,
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

    **A second dispatcher is a second copy of every rule the first one has.**
    This one was written as the manual twin of the beat task and then stopped
    tracking it. It kept the kill switch and drifted on the two interlocks the
    sweep added afterwards, both of which it can defeat precisely *because* an
    operator reaches for it when something looks stuck:

    * **Paused campaigns.** ``PATCH /sequences/{id}/status`` sets
      ``is_active=False`` and the console renders the sequence as paused. The
      sweep joins on it; this endpoint selected every ``active`` enrollment
      regardless, so one call re-dispatched the shelved campaign the operator
      had just stopped. ``send_sequence_step`` holds the actual send, so no mail
      goes out — but the whole point of the SQL filter in the sweep is that a
      paused campaign costs nothing, and here it cost a task per enrollment.
    * **The dispatch lease.** ``OutreachEnrollment.dispatched_at`` is what stops
      an enrollment whose task is still in the broker from being queued again.
      The sweep takes it; this endpoint neither read nor wrote it, so it handed
      the broker a fresh copy of every task already in it — and each copy pays
      for contact research and an LLM composition before ``send_claim`` refuses
      it. That is the exact cost the lease was added to stop, reachable by the
      button an operator presses when the queue looks behind.

    What it deliberately still does not do is rank (``queue_priority``), check
    the sending window, or run per-org: it is a manual trigger, not a scheduler,
    and every one of those gates runs again inside ``send_sequence_step``. What
    it must not do is *undo* an interlock, which is a different thing from not
    implementing a heuristic.
    """
    from app.tasks.outreach import send_sequence_step
    from app.services import system_settings

    # Global kill switch — dispatch nothing while sending is halted.
    if not await system_settings.is_sending_enabled(db):
        return {
            "status": "halted",
            "reason": "Global sending kill switch is engaged.",
            "active_enrollments": 0,
            "dispatched": 0,
            "skipped": 0,
        }

    result = await db.execute(
        select(OutreachEnrollment)
        .join(
            OutreachSequence,
            OutreachEnrollment.sequence_id == OutreachSequence.id,
        )
        .where(
            OutreachEnrollment.status == "active",
            OutreachSequence.is_active.is_(True),
        )
    )
    enrollments = list(result.scalars().all())
    dispatched = 0
    skipped = 0
    skipped_in_flight = 0
    results = []
    now = datetime.now(timezone.utc)

    # Same lease the sweep takes, read from the same setting and floored the same
    # way — a lease shorter than the sweep interval is inert.
    from app.config import get_settings as _get_settings

    dispatch_lease = timedelta(
        minutes=max(getattr(_get_settings(), "dispatch_lease_minutes", 15), 6)
    )

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
        # Already queued and not yet expired — the task is in the broker, and a
        # second copy of it does research and an LLM composition before the send
        # claim refuses it. Checked before the due test, since a leased
        # enrollment is by definition one that was due a moment ago.
        leased_at = enrollment.dispatched_at
        if leased_at is not None:
            if leased_at.tzinfo is None:
                leased_at = leased_at.replace(tzinfo=timezone.utc)
            if now - leased_at < dispatch_lease:
                skipped_in_flight += 1
                continue

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

            # Same resolver the executor uses (``services.step_delay``), with the
            # same adaptive factor. This preview computed a raw-delay due date
            # while the executor computed a factored one, so it could report a
            # step as ready that the send path would immediately defer.
            from app.services import step_delay

            anchor = last_event.occurred_at if last_event else enrollment.enrolled_at
            is_due, _due_at = step_delay.is_due(
                step,
                anchor,
                now=now,
                factor=enrollment.adaptive_delay_factor,
                enrollment_id=enrollment.id,
            )

        if not is_due:
            skipped += 1
            continue

        # Per-contact limit check
        contact_result = await db.execute(
            select(Contact).where(Contact.id == enrollment.contact_id)
        )
        contact = contact_result.scalar_one_or_none()
        # Skip enrollments whose recipient domain is known-undeliverable.
        if contact and contact.email_status == "invalid":
            skipped += 1
            continue
        if contact and contact.email:
            can_send_contact, _ = await limiter.can_send_to_contact(db, contact.email)
            if not can_send_contact:
                skipped += 1
                continue

        # Take the lease before queuing, in both branches. The broker rejecting
        # the task is not a reason to skip it: the fallback path counts the
        # enrollment as dispatched and reports it to the caller, so leaving it
        # unleased would let the next call queue it again on the strength of a
        # dispatch this one already claimed.
        enrollment.dispatched_at = now

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
        # Reported rather than folded into ``skipped``: a number that climbs
        # across calls is the readable form of "the worker is not keeping up",
        # which is the condition an operator is usually pressing this for.
        "skipped_in_flight": skipped_in_flight,
        "results": results,
    }
