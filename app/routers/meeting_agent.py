"""Meeting Booking Agent — Console API (``/meeting-agent``).

The operator surface for the autonomous scheduling agent:

* ``GET  /meeting-agent/config``            — the org's agent config (lazily created)
* ``PUT  /meeting-agent/config``            — update config (org_admin)
* ``GET  /meeting-agent/meetings``          — booked meetings (filter: upcoming/past/status)
* ``GET  /meeting-agent/meetings/{id}``     — one meeting incl. the scheduling thread
* ``POST /meeting-agent/meetings/{id}/cancel`` — cancel a meeting
* ``GET  /meeting-agent/stats``             — booking-rate / show-rate / funnel stats

Everything is org-scoped: the config is the caller's own org's row, and every
meeting query is filtered to the current org by the global read filter. Config
writes are gated on ``org_admin`` via ``OrgScope.check_permission``.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.contact import Contact
from app.models.meeting_booking import BOOKED_MEETING_STATUSES, BookedMeeting, MeetingBookingAgent
from app.models.user import UserRole
from app.schemas.meeting_agent import (
    BookedMeetingDetail,
    BookedMeetingSummary,
    MeetingAgentConfig,
    MeetingAgentConfigUpdate,
    MeetingAgentStats,
    MeetingListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/meeting-agent", tags=["meeting-agent"])


async def _get_or_create_agent(db: AsyncSession) -> MeetingBookingAgent:
    """The caller-org's agent config, created with defaults on first access.

    Org-scoped: the SELECT only sees the current org's row, and a fresh insert is
    stamped with the current org by the before_flush hook.
    """
    agent = (await db.execute(select(MeetingBookingAgent).limit(1))).scalar_one_or_none()
    if agent is None:
        agent = MeetingBookingAgent()
        db.add(agent)
        await db.flush()
        await db.refresh(agent)
    return agent


async def _contact_map(db: AsyncSession, meetings: list[BookedMeeting]) -> dict:
    """Resolve {contact_id: (name, email)} for a page of meetings in one query."""
    ids = {m.contact_id for m in meetings if m.contact_id is not None}
    if not ids:
        return {}
    rows = (await db.execute(select(Contact).where(Contact.id.in_(ids)))).scalars().all()
    return {c.id: c for c in rows}


def _summary(meeting: BookedMeeting, contact: Optional[Contact]) -> BookedMeetingSummary:
    s = BookedMeetingSummary.model_validate(meeting)
    if contact is not None:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        s.contact_name = name or None
        s.contact_email = contact.email
    return s


# --- Config ------------------------------------------------------------------


@router.get("/config", response_model=MeetingAgentConfig)
async def get_config(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Return the org's booking-agent config (created with defaults if absent)."""
    return await _get_or_create_agent(db)


@router.put("/config", response_model=MeetingAgentConfig)
async def update_config(
    body: MeetingAgentConfigUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update the agent config (org_admin only)."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    agent = await _get_or_create_agent(db)

    data = body.model_dump(exclude_unset=True)
    # Guard: enabling the agent without a calendar to book on is a no-op that
    # reads as "on but doing nothing" — reject it with an actionable message.
    resulting_calendar = data.get("calendar_account_id", agent.calendar_account_id)
    if data.get("is_active") is True and resulting_calendar is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select a calendar account before activating the meeting booking agent.",
        )

    for field, value in data.items():
        setattr(agent, field, value)
    await db.flush()
    await db.refresh(agent)
    logger.info("Meeting agent config updated for org %s by %s", scope.org_id, scope.user.email)
    return agent


# --- Meetings ----------------------------------------------------------------


@router.get("/meetings", response_model=MeetingListResponse)
async def list_meetings(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
    filter: str = Query("all", pattern="^(all|upcoming|past)$"),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """List booked meetings, newest activity first.

    ``filter=upcoming`` returns confirmed meetings starting in the future;
    ``filter=past`` returns meetings whose start time has passed. ``status``
    narrows to one lifecycle state (proposed/confirmed/…).
    """
    now = datetime.now(timezone.utc)
    stmt = select(BookedMeeting)
    count_stmt = select(func.count()).select_from(BookedMeeting)

    if status_filter:
        if status_filter not in BOOKED_MEETING_STATUSES:
            raise HTTPException(status_code=400, detail=f"Unknown status '{status_filter}'")
        stmt = stmt.where(BookedMeeting.status == status_filter)
        count_stmt = count_stmt.where(BookedMeeting.status == status_filter)

    if filter == "upcoming":
        cond = (BookedMeeting.start_time.isnot(None)) & (BookedMeeting.start_time >= now) & (
            BookedMeeting.status == "confirmed"
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    elif filter == "past":
        cond = (BookedMeeting.start_time.isnot(None)) & (BookedMeeting.start_time < now)
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    # Upcoming reads best soonest-first; everything else newest-activity-first.
    if filter == "upcoming":
        stmt = stmt.order_by(BookedMeeting.start_time.asc())
    else:
        stmt = stmt.order_by(BookedMeeting.created_at.desc())

    total = (await db.execute(count_stmt)).scalar_one()
    meetings = list((await db.execute(stmt.limit(limit).offset(offset))).scalars().all())
    contacts = await _contact_map(db, meetings)
    items = [_summary(m, contacts.get(m.contact_id)) for m in meetings]
    return MeetingListResponse(items=items, total=total)


@router.get("/meetings/{meeting_id}", response_model=BookedMeetingDetail)
async def get_meeting(
    meeting_id: UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """One meeting with its proposed slots and full scheduling conversation."""
    meeting = (
        await db.execute(select(BookedMeeting).where(BookedMeeting.id == meeting_id))
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    contacts = await _contact_map(db, [meeting])
    detail = BookedMeetingDetail.model_validate(meeting)
    contact = contacts.get(meeting.contact_id)
    if contact is not None:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        detail.contact_name = name or None
        detail.contact_email = contact.email
    return detail


@router.post("/meetings/{meeting_id}/cancel", response_model=BookedMeetingDetail)
async def cancel_meeting(
    meeting_id: UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Cancel a meeting. Idempotent — cancelling a cancelled meeting is a no-op.

    A completed meeting cannot be cancelled after the fact (409); the outcome
    already happened.
    """
    meeting = (
        await db.execute(select(BookedMeeting).where(BookedMeeting.id == meeting_id))
    ).scalar_one_or_none()
    if meeting is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if meeting.status == "completed":
        raise HTTPException(status_code=409, detail="A completed meeting cannot be cancelled")

    if meeting.status != "cancelled":
        meeting.status = "cancelled"
        meeting.conversation_thread = [
            *(meeting.conversation_thread or []),
            {
                "role": "agent",
                "text": f"Meeting cancelled by {scope.user.email}.",
                "at": datetime.now(timezone.utc).isoformat(),
                "kind": "cancel",
            },
        ]
        await db.flush()
        logger.info("Meeting %s cancelled by %s", meeting_id, scope.user.email)

    await db.refresh(meeting)
    contacts = await _contact_map(db, [meeting])
    detail = BookedMeetingDetail.model_validate(meeting)
    contact = contacts.get(meeting.contact_id)
    if contact is not None:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip()
        detail.contact_name = name or None
        detail.contact_email = contact.email
    return detail


# --- Stats -------------------------------------------------------------------


@router.get("/stats", response_model=MeetingAgentStats)
async def get_stats(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Booking funnel + outcome stats for the dashboard cards."""
    now = datetime.now(timezone.utc)
    rows = (
        await db.execute(
            select(BookedMeeting.status, func.count()).group_by(BookedMeeting.status)
        )
    ).all()
    by_status = {s: c for s, c in rows}
    proposed = by_status.get("proposed", 0)
    confirmed = by_status.get("confirmed", 0)
    completed = by_status.get("completed", 0)
    cancelled = by_status.get("cancelled", 0)
    no_show = by_status.get("no_show", 0)
    total = proposed + confirmed + completed + cancelled + no_show

    booked = confirmed + completed
    booking_rate = round(booked / total, 4) if total else 0.0
    show_denom = completed + no_show
    show_rate = round(completed / show_denom, 4) if show_denom else 0.0

    # Average time-to-book: hours from the proposal (created_at) to confirmation.
    # We don't keep a dedicated confirmed_at column, so for a currently-confirmed
    # meeting updated_at ≈ the confirmation time (the confirm sweep was its last
    # write). Restricted to status='confirmed' to avoid later writes (reminders,
    # completion sweeps) skewing the delta.
    avg_hours = None
    timed = (
        await db.execute(
            select(BookedMeeting.created_at, BookedMeeting.updated_at).where(
                BookedMeeting.status == "confirmed",
            )
        )
    ).all()
    if timed:
        deltas = []
        for created, updated in timed:
            if created is None or updated is None:
                continue
            hrs = (updated - created).total_seconds() / 3600.0
            if hrs >= 0:
                deltas.append(hrs)
        if deltas:
            avg_hours = round(sum(deltas) / len(deltas), 1)

    upcoming = (
        await db.execute(
            select(func.count()).select_from(BookedMeeting).where(
                BookedMeeting.status == "confirmed",
                BookedMeeting.start_time.isnot(None),
                BookedMeeting.start_time >= now,
            )
        )
    ).scalar_one()

    return MeetingAgentStats(
        total_meetings=total,
        proposed=proposed,
        confirmed=confirmed,
        completed=completed,
        cancelled=cancelled,
        no_show=no_show,
        booking_rate=booking_rate,
        show_rate=show_rate,
        avg_hours_to_book=avg_hours,
        upcoming=upcoming,
    )
