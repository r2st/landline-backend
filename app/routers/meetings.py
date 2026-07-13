"""Smart meeting scheduler & pre-meeting briefing endpoints (feature 2.5).

- POST /meetings/briefings                 — generate + store a pre-meeting brief.
- GET  /meetings/briefings                 — list briefs (optionally by company).
- GET  /meetings/briefings/{id}            — fetch one brief.
- POST /meetings/briefings/{id}/post-meeting — run post-meeting analysis on notes.
- POST /meetings/propose-times             — propose meeting slots + scheduling link.
- POST /meetings/schedule                  — create a Google Calendar event (best-effort).
"""

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.gmail_account import GmailAccount
from app.models.meeting import MeetingBrief
from app.services import google_calendar
from app.services.meeting_intelligence import get_meeting_intelligence

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/meetings", tags=["meetings"])


# --- Schemas ---

class BriefingCreate(BaseModel):
    company_id: uuid.UUID
    contact_ids: list[uuid.UUID] = Field(default_factory=list)
    meeting_at: Optional[datetime] = None
    title: Optional[str] = None
    use_ai: bool = True


class BriefingRead(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    contact_ids: list = []
    title: str
    meeting_at: Optional[datetime] = None
    briefing_content: dict = {}
    status: str
    calendar_event_id: Optional[str] = None
    post_meeting_analysis: dict = {}
    model_used: Optional[str] = None
    generated_at: datetime
    created_at: datetime

    class Config:
        from_attributes = True


class PostMeetingRequest(BaseModel):
    notes: str = Field(..., min_length=1)
    use_ai: bool = True


class ProposeTimesRequest(BaseModel):
    company_id: Optional[uuid.UUID] = None
    count: int = Field(3, ge=1, le=8)


class ScheduleRequest(BaseModel):
    company_id: uuid.UUID
    start: datetime
    summary: Optional[str] = None
    description: str = ""
    attendee_emails: list[str] = Field(default_factory=list)
    contact_ids: list[uuid.UUID] = Field(default_factory=list)
    duration_minutes: int = Field(30, ge=15, le=240)
    brief_id: Optional[uuid.UUID] = None


async def _require_company(db: AsyncSession, company_id: uuid.UUID) -> Company:
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


# --- Briefings ---

@router.post("/briefings", response_model=BriefingRead, status_code=201)
async def create_briefing(payload: BriefingCreate, db: AsyncSession = Depends(get_db)):
    await _require_company(db, payload.company_id)
    mi = get_meeting_intelligence()
    try:
        brief = await mi.build_and_store_brief(
            db,
            payload.company_id,
            contact_ids=payload.contact_ids or None,
            meeting_at=payload.meeting_at,
            title=payload.title,
            use_ai=payload.use_ai,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    await db.refresh(brief)
    return brief


@router.get("/briefings", response_model=list[BriefingRead])
async def list_briefings(
    company_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(MeetingBrief).order_by(desc(MeetingBrief.generated_at))
    if company_id:
        query = query.where(MeetingBrief.company_id == company_id)
    return list((await db.execute(query.limit(limit))).scalars().all())


@router.get("/briefings/{brief_id}", response_model=BriefingRead)
async def get_briefing(brief_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    brief = (
        await db.execute(select(MeetingBrief).where(MeetingBrief.id == brief_id))
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="Briefing not found")
    return brief


@router.post("/briefings/{brief_id}/post-meeting", response_model=BriefingRead)
async def run_post_meeting(
    brief_id: uuid.UUID, payload: PostMeetingRequest, db: AsyncSession = Depends(get_db)
):
    """Run post-meeting analysis on notes and attach it to the brief."""
    brief = (
        await db.execute(select(MeetingBrief).where(MeetingBrief.id == brief_id))
    ).scalar_one_or_none()
    if brief is None:
        raise HTTPException(status_code=404, detail="Briefing not found")
    mi = get_meeting_intelligence()
    await mi.analyze_post_meeting(
        db,
        company_id=brief.company_id,
        notes=payload.notes,
        brief=brief,
        use_ai=payload.use_ai,
    )
    await db.refresh(brief)
    return brief


# --- Scheduling ---

@router.post("/propose-times", response_model=dict)
async def propose_times(payload: ProposeTimesRequest, db: AsyncSession = Depends(get_db)):
    """Propose meeting slots (within the send window) plus the scheduling link."""
    if payload.company_id is not None:
        await _require_company(db, payload.company_id)
    mi = get_meeting_intelligence()
    return mi.propose_meeting_times(count=payload.count)


@router.post("/schedule", response_model=dict)
async def schedule_meeting(payload: ScheduleRequest, db: AsyncSession = Depends(get_db)):
    """Create a Google Calendar event on a connected account (best-effort).

    Falls back to returning the configured scheduling link when no calendar-
    capable account is connected or the API call fails.
    """
    company = await _require_company(db, payload.company_id)

    # Resolve attendee emails: explicit list + any named contacts.
    attendees = list(payload.attendee_emails)
    if payload.contact_ids:
        rows = (
            await db.execute(select(Contact).where(Contact.id.in_(payload.contact_ids)))
        ).scalars().all()
        attendees.extend(c.email for c in rows if c.email)

    account = (
        await db.execute(
            select(GmailAccount).where(GmailAccount.status == "connected").limit(1)
        )
    ).scalar_one_or_none()

    summary = payload.summary or f"Apprend × {company.name}"
    if account is None:
        mi = get_meeting_intelligence()
        fallback = mi.propose_meeting_times(count=3)
        return {
            "scheduled": False,
            "reason": "No connected calendar account — offer the scheduling link.",
            "scheduling_link": fallback["scheduling_link"],
        }

    # google_calendar.create_event uses the blocking Google client; run it off the loop.
    result = await asyncio.to_thread(
        google_calendar.create_event,
        account,
        summary=summary,
        start=payload.start,
        attendee_emails=list(dict.fromkeys(attendees)),
        description=payload.description,
        duration_minutes=payload.duration_minutes,
    )
    if result.get("error"):
        return {"scheduled": False, "reason": result["error"]}

    # Link the event back to a brief when provided.
    if payload.brief_id is not None:
        brief = (
            await db.execute(select(MeetingBrief).where(MeetingBrief.id == payload.brief_id))
        ).scalar_one_or_none()
        if brief is not None:
            brief.calendar_event_id = result.get("event_id")
            brief.meeting_at = payload.start
            await db.flush()

    return {"scheduled": True, **result}
