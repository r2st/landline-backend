"""Follow-up reminder endpoints — schedule, list, and complete follow-ups,
plus a query for contacts that have gone stale (not emailed in X days)."""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.schemas.follow_up import (
    FollowUpCreate,
    FollowUpRead,
    FollowUpUpdate,
    StaleContact,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/follow-ups", tags=["follow-ups"])


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a (possibly naive) datetime to a timezone-aware UTC datetime.

    SQLite (used in tests) returns naive datetimes; Postgres returns aware ones.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _serialize(db: AsyncSession, fu: FollowUp, now: datetime) -> FollowUpRead:
    """Build a FollowUpRead with contact/company names and overdue flag."""
    contact_name = None
    company_name = None
    if fu.contact_id is not None:
        result = await db.execute(select(Contact).where(Contact.id == fu.contact_id))
        contact = result.scalar_one_or_none()
        if contact:
            contact_name = contact.full_name
            if company_name is None and contact.company_id:
                cresult = await db.execute(
                    select(Company.name).where(Company.id == contact.company_id)
                )
                company_name = cresult.scalar_one_or_none()
    if company_name is None and fu.company_id is not None:
        cresult = await db.execute(select(Company.name).where(Company.id == fu.company_id))
        company_name = cresult.scalar_one_or_none()

    due = _aware(fu.due_date)
    is_overdue = fu.status == "pending" and due is not None and due < now

    return FollowUpRead(
        id=fu.id,
        contact_id=fu.contact_id,
        company_id=fu.company_id,
        due_date=fu.due_date,
        reason=fu.reason,
        status=fu.status,
        notes=fu.notes,
        created_at=fu.created_at,
        completed_at=fu.completed_at,
        contact_name=contact_name,
        company_name=company_name,
        is_overdue=is_overdue,
        enrollment_id=fu.enrollment_id,
        step_number=fu.step_number,
        channel=fu.channel,
    )


async def _get_follow_up_in_scope(
    db: AsyncSession, follow_up_id: uuid.UUID, scope: UserScope
) -> FollowUp:
    """Load a follow-up, 404-ing if it's outside the caller's scope."""
    follow_up = (
        await db.execute(select(FollowUp).where(FollowUp.id == follow_up_id))
    ).scalar_one_or_none()
    if not follow_up or not scope.can_access(follow_up.user_id):
        raise HTTPException(status_code=404, detail="Follow-up not found")
    return follow_up


@router.post("", response_model=FollowUpRead, status_code=201)
async def create_follow_up(
    payload: FollowUpCreate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Create a follow-up reminder for a contact and/or company."""
    if payload.contact_id is None and payload.company_id is None:
        raise HTTPException(
            status_code=400, detail="A follow-up must reference a contact or a company"
        )

    if payload.contact_id is not None:
        result = await db.execute(select(Contact).where(Contact.id == payload.contact_id))
        contact = result.scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
        # Default the company from the contact if not given.
        if payload.company_id is None:
            payload.company_id = contact.company_id

    if payload.company_id is not None:
        result = await db.execute(select(Company).where(Company.id == payload.company_id))
        if not result.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Company not found")

    follow_up = FollowUp(
        contact_id=payload.contact_id,
        company_id=payload.company_id,
        due_date=payload.due_date,
        reason=payload.reason,
        notes=payload.notes,
        user_id=scope.owner_id,
    )
    db.add(follow_up)
    await db.flush()
    await db.refresh(follow_up)
    logger.info("Created follow-up %s due %s", follow_up.id, follow_up.due_date)
    return await _serialize(db, follow_up, datetime.now(timezone.utc))


@router.get("", response_model=list[FollowUpRead])
async def list_follow_ups(
    status: Optional[str] = Query(None, pattern="^(pending|completed|dismissed)$"),
    contact_id: Optional[uuid.UUID] = Query(None),
    company_id: Optional[uuid.UUID] = Query(None),
    overdue: Optional[bool] = Query(None, description="Only overdue pending follow-ups"),
    within_days: Optional[int] = Query(
        None, ge=0, le=365, description="Only pending follow-ups due within N days"
    ),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """List follow-ups, newest due first, with optional filters.

    Scoped to the caller; admins/managers may pass ``view_as_user_id``.
    """
    query = scope.apply(select(FollowUp), FollowUp.user_id)
    conditions = []
    if status:
        conditions.append(FollowUp.status == status)
    if contact_id:
        conditions.append(FollowUp.contact_id == contact_id)
    if company_id:
        conditions.append(FollowUp.company_id == company_id)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(FollowUp.due_date.asc())

    result = await db.execute(query)
    follow_ups = list(result.scalars().all())

    now = datetime.now(timezone.utc)
    items = [await _serialize(db, fu, now) for fu in follow_ups]

    # Python-side filters that depend on tz-normalized comparison.
    if overdue:
        items = [i for i in items if i.is_overdue]
    if within_days is not None:
        horizon = now + timedelta(days=within_days)
        items = [
            i for i in items
            if i.status == "pending" and now <= _aware(i.due_date) <= horizon
        ]
    return items


@router.get("/stale-contacts", response_model=list[StaleContact])
async def get_stale_contacts(
    days: int = Query(30, ge=1, le=365, description="Days since last email to flag as stale"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List contacts (with an email) that haven't been emailed in `days` days.

    Contacts that have never been emailed are included. Each row notes whether
    an open (pending) follow-up already exists so the UI can avoid nagging.
    """
    # Latest send time per contact (only successful/recorded sends count).
    last_send = (
        select(
            EmailSend.contact_id.label("contact_id"),
            func.max(EmailSend.sent_at).label("last_sent_at"),
        )
        .where(EmailSend.contact_id.isnot(None))
        .group_by(EmailSend.contact_id)
        .subquery()
    )

    query = (
        select(
            Contact.id,
            Contact.company_id,
            Contact.first_name,
            Contact.last_name,
            Contact.email,
            Contact.title,
            Company.name.label("company_name"),
            last_send.c.last_sent_at,
        )
        .outerjoin(last_send, last_send.c.contact_id == Contact.id)
        .outerjoin(Company, Company.id == Contact.company_id)
        .where(Contact.email.isnot(None))
    )

    result = await db.execute(query)
    rows = result.all()

    # Contacts with an open follow-up already.
    fu_result = await db.execute(
        select(FollowUp.contact_id).where(
            FollowUp.status == "pending", FollowUp.contact_id.isnot(None)
        )
    )
    open_follow_up_ids = {r[0] for r in fu_result.all()}

    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=days)

    stale: list[StaleContact] = []
    for row in rows:
        last_sent = _aware(row.last_sent_at)
        if last_sent is not None and last_sent > threshold:
            continue  # contacted recently — not stale
        days_since = (
            (now - last_sent).days if last_sent is not None else None
        )
        stale.append(
            StaleContact(
                contact_id=row.id,
                company_id=row.company_id,
                first_name=row.first_name,
                last_name=row.last_name,
                email=row.email,
                title=row.title,
                company_name=row.company_name,
                last_emailed_at=row.last_sent_at,
                days_since_contact=days_since,
                has_open_follow_up=row.id in open_follow_up_ids,
            )
        )

    # Never-contacted first, then longest-since-contact first.
    stale.sort(
        key=lambda s: (s.days_since_contact is not None, -(s.days_since_contact or 0))
    )
    return stale[:limit]


@router.patch("/{follow_up_id}", response_model=FollowUpRead)
async def update_follow_up(
    follow_up_id: uuid.UUID,
    payload: FollowUpUpdate,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Update a follow-up — reschedule, edit notes, or change status."""
    follow_up = await _get_follow_up_in_scope(db, follow_up_id, scope)

    update_data = payload.model_dump(exclude_unset=True)
    new_status = update_data.get("status")
    for field, value in update_data.items():
        setattr(follow_up, field, value)

    # Stamp completion time when moving into a terminal state.
    if new_status in ("completed", "dismissed") and follow_up.completed_at is None:
        follow_up.completed_at = datetime.now(timezone.utc)
    elif new_status == "pending":
        follow_up.completed_at = None

    await db.flush()

    # A task filed by a non-email sequence step IS the cadence — the enrollment
    # is holding on it. Advancing here rather than waiting for the hold to expire
    # is what makes the next email follow the call by the interval the sequence
    # asked for, instead of by however much of the deadline was left.
    #
    # Only on a fresh terminal transition: a PATCH that edits the notes of an
    # already-completed task must not advance the cadence a second time.
    if new_status in ("completed", "dismissed"):
        from app.services import manual_touch

        advanced = await manual_touch.complete_task(db, follow_up)
        if advanced is not None:
            logger.info(
                "Follow-up %s advanced enrollment %s to step %s",
                follow_up_id, advanced["enrollment_id"], advanced["next_step"],
            )

    await db.refresh(follow_up)
    logger.info("Updated follow-up %s (status=%s)", follow_up_id, follow_up.status)
    return await _serialize(db, follow_up, datetime.now(timezone.utc))


@router.delete("/{follow_up_id}", status_code=204)
async def delete_follow_up(
    follow_up_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Delete a follow-up reminder (must be in the caller's scope)."""
    follow_up = await _get_follow_up_in_scope(db, follow_up_id, scope)
    await db.delete(follow_up)
    await db.flush()
    logger.info("Deleted follow-up %s", follow_up_id)
