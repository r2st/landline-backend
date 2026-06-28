"""Cold-call workflow API — call tasks, scripts, notifications, queue, briefing.

Endpoints:
* ``GET    /calls/tasks``                 — list call tasks (filterable)
* ``GET    /calls/tasks/today``           — today's call queue for a rep
* ``POST   /calls/tasks``                 — create a call task
* ``PUT    /calls/tasks/{id}``            — update (complete, log outcome, notes)
* ``GET    /calls/tasks/{id}/briefing``   — pre-call background brief
* ``GET    /calls/scripts``               — list scripts (DB + in-code defaults)
* ``GET    /calls/scripts/{industry}``    — script for one industry
* ``POST   /calls/scripts``               — create/update a script
* ``GET    /calls/notifications``         — unread notifications
* ``PUT    /calls/notifications/{id}/read`` — mark a notification read
* ``POST   /calls/queue/generate``        — AI-prioritised call queue for the day
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.call import CallNotification, CallScript, CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.schemas.call import (
    CallBriefing,
    CallNotificationRead,
    CallScriptRead,
    CallScriptUpsert,
    CallTaskCreate,
    CallTaskRead,
    CallTaskUpdate,
    QueueGenerateRequest,
    QueueGenerateResponse,
)
from app.services import call_scripts_data
from app.services.call_briefing import build_briefing
from app.services.call_queue import generate_call_queue_for_session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/calls", tags=["calls"])

_OPEN_CALL_STATUSES = ("pending", "rescheduled")


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _validate_refs(
    db: AsyncSession, company_id: uuid.UUID, contact_id: Optional[uuid.UUID]
):
    company = (
        await db.execute(select(Company.id).where(Company.id == company_id))
    ).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    if contact_id is not None:
        contact = (
            await db.execute(select(Contact.id).where(Contact.id == contact_id))
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")


# --------------------------------------------------------------------------- #
# Call tasks
# --------------------------------------------------------------------------- #


@router.post("/tasks", response_model=CallTaskRead, status_code=201)
async def create_call_task(payload: CallTaskCreate, db: AsyncSession = Depends(get_db)):
    """Create a cold-call task for a company (optionally tied to a contact)."""
    await _validate_refs(db, payload.company_id, payload.contact_id)
    task = CallTask(**payload.model_dump())
    db.add(task)
    await db.flush()
    await db.refresh(task)
    logger.info("Created call task %s for company %s", task.id, payload.company_id)
    return task


@router.get("/tasks", response_model=list[CallTaskRead])
async def list_call_tasks(
    status: Optional[str] = Query(None, pattern="^(pending|completed|skipped|rescheduled)$"),
    priority: Optional[str] = Query(None, pattern="^(hot|warm|cold)$"),
    assigned_to: Optional[str] = Query(None),
    company_id: Optional[uuid.UUID] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    outcome: Optional[str] = Query(None),
    scheduled_from: Optional[datetime] = Query(None),
    scheduled_to: Optional[datetime] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List call tasks with optional filters; hottest then soonest-scheduled."""
    conditions = []
    if status:
        conditions.append(CallTask.status == status)
    if priority:
        conditions.append(CallTask.priority == priority)
    if assigned_to:
        conditions.append(CallTask.assigned_to == assigned_to)
    if company_id:
        conditions.append(CallTask.company_id == company_id)
    if contact_id:
        conditions.append(CallTask.contact_id == contact_id)
    if outcome:
        conditions.append(CallTask.outcome == outcome)
    if scheduled_from:
        conditions.append(CallTask.scheduled_at >= scheduled_from)
    if scheduled_to:
        conditions.append(CallTask.scheduled_at <= scheduled_to)

    query = select(CallTask)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(
        CallTask.scheduled_at.is_(None), CallTask.scheduled_at.asc(), CallTask.created_at.desc()
    )
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/tasks/today", response_model=list[CallTaskRead])
async def todays_call_queue(
    assigned_to: Optional[str] = Query(None),
    include_overdue: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Open call tasks for today (plus overdue), hottest first.

    "Today" means scheduled today, or unscheduled-but-created-today. With
    ``include_overdue`` (default), still-open tasks scheduled before today are
    included so nothing slips through the cracks.
    """
    query = select(CallTask).where(CallTask.status.in_(_OPEN_CALL_STATUSES))
    if assigned_to:
        query = query.where(CallTask.assigned_to == assigned_to)
    tasks = list((await db.execute(query)).scalars().all())

    today = datetime.now(timezone.utc).date()
    priority_rank = {"hot": 0, "warm": 1, "cold": 2}
    selected = []
    for t in tasks:
        sched = _aware(t.scheduled_at)
        created = _aware(t.created_at)
        sched_day = sched.date() if sched else None
        created_day = created.date() if created else None
        if sched_day == today:
            selected.append(t)
        elif sched_day is not None and sched_day < today and include_overdue:
            selected.append(t)
        elif sched_day is None and created_day == today:
            selected.append(t)

    selected.sort(
        key=lambda t: (
            priority_rank.get(t.priority, 3),
            _aware(t.scheduled_at) or _aware(t.created_at) or datetime.max.replace(tzinfo=timezone.utc),
        )
    )
    return selected


@router.get("/tasks/{task_id}", response_model=CallTaskRead)
async def get_call_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single call task."""
    task = (
        await db.execute(select(CallTask).where(CallTask.id == task_id))
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Call task not found")
    return task


@router.put("/tasks/{task_id}", response_model=CallTaskRead)
async def update_call_task(
    task_id: uuid.UUID, payload: CallTaskUpdate, db: AsyncSession = Depends(get_db)
):
    """Update a call task — mark complete, log an outcome, add notes, reschedule.

    Marking ``status=completed`` auto-stamps ``completed_at`` if not supplied.
    """
    task = (
        await db.execute(select(CallTask).where(CallTask.id == task_id))
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Call task not found")

    update_data = payload.model_dump(exclude_unset=True)
    if "contact_id" in update_data and update_data["contact_id"] is not None:
        contact = (
            await db.execute(
                select(Contact.id).where(Contact.id == update_data["contact_id"])
            )
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")

    for field, value in update_data.items():
        setattr(task, field, value)

    if (
        update_data.get("status") == "completed"
        and "completed_at" not in update_data
        and task.completed_at is None
    ):
        task.completed_at = datetime.now(timezone.utc)

    await db.flush()
    await db.refresh(task)
    logger.info("Updated call task %s", task_id)
    return task


@router.get("/tasks/{task_id}/briefing", response_model=CallBriefing)
async def call_task_briefing(
    task_id: uuid.UUID,
    script_type: str = Query("initial_cold_call", pattern="^(initial_cold_call|follow_up|voicemail)$"),
    db: AsyncSession = Depends(get_db),
):
    """Pre-call background brief: company, engagement, pain points, script."""
    task = (
        await db.execute(select(CallTask).where(CallTask.id == task_id))
    ).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Call task not found")
    return await build_briefing(db, call_task=task, script_type=script_type)


# --------------------------------------------------------------------------- #
# Call scripts
# --------------------------------------------------------------------------- #


def _default_scripts_as_read() -> list[CallScriptRead]:
    return [CallScriptRead(**t.as_row()) for t in call_scripts_data.all_scripts()]


@router.get("/scripts", response_model=list[CallScriptRead])
async def list_scripts(
    industry: Optional[str] = Query(None),
    script_type: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """List call scripts. Falls back to the canonical in-code scripts when the
    DB has none seeded yet (e.g. a fresh database)."""
    query = select(CallScript)
    if industry:
        query = query.where(CallScript.industry == industry)
    if script_type:
        query = query.where(CallScript.script_type == script_type)
    rows = list((await db.execute(query)).scalars().all())
    if rows:
        return [CallScriptRead.model_validate(r) for r in rows]

    # Fallback to in-code defaults, applying the same filters.
    defaults = _default_scripts_as_read()
    if industry:
        defaults = [d for d in defaults if d.industry == industry]
    if script_type:
        defaults = [d for d in defaults if d.script_type == script_type]
    return defaults


@router.get("/scripts/{industry}", response_model=CallScriptRead)
async def get_script_for_industry(
    industry: str,
    script_type: str = Query("initial_cold_call"),
    db: AsyncSession = Depends(get_db),
):
    """Get the script for a specific industry (DB first, then in-code default)."""
    row = (
        await db.execute(
            select(CallScript).where(
                CallScript.industry == industry,
                CallScript.script_type == script_type,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return CallScriptRead.model_validate(row)

    tpl = call_scripts_data.get_script(industry, script_type)
    if tpl is None:
        raise HTTPException(status_code=404, detail="No script for that industry")
    return CallScriptRead(**tpl.as_row())


@router.post("/scripts", response_model=CallScriptRead, status_code=201)
async def upsert_script(payload: CallScriptUpsert, db: AsyncSession = Depends(get_db)):
    """Create or update a script for an (industry, script_type) pair."""
    existing = (
        await db.execute(
            select(CallScript).where(
                CallScript.industry == payload.industry,
                CallScript.script_type == payload.script_type,
            )
        )
    ).scalar_one_or_none()

    data = payload.model_dump()
    data["objection_handling"] = [o for o in data.get("objection_handling", [])]

    if existing is not None:
        for field, value in data.items():
            setattr(existing, field, value)
        await db.flush()
        await db.refresh(existing)
        logger.info("Updated call script %s/%s", payload.industry, payload.script_type)
        return CallScriptRead.model_validate(existing)

    script = CallScript(**data)
    db.add(script)
    await db.flush()
    await db.refresh(script)
    logger.info("Created call script %s/%s", payload.industry, payload.script_type)
    return CallScriptRead.model_validate(script)


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #


@router.get("/notifications", response_model=list[CallNotificationRead])
async def list_notifications(
    unread_only: bool = Query(True),
    notification_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List call notifications, newest first (unread only by default)."""
    query = select(CallNotification)
    if unread_only:
        query = query.where(CallNotification.read_at.is_(None))
    if notification_type:
        query = query.where(CallNotification.notification_type == notification_type)
    query = query.order_by(CallNotification.created_at.desc()).limit(limit)
    return list((await db.execute(query)).scalars().all())


@router.put("/notifications/{notification_id}/read", response_model=CallNotificationRead)
async def mark_notification_read(
    notification_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    """Mark a notification as read."""
    notif = (
        await db.execute(
            select(CallNotification).where(CallNotification.id == notification_id)
        )
    ).scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notif.read_at is None:
        notif.read_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(notif)
    return notif


# --------------------------------------------------------------------------- #
# Queue generation
# --------------------------------------------------------------------------- #


@router.post("/queue/generate", response_model=QueueGenerateResponse)
async def generate_queue(
    payload: QueueGenerateRequest | None = None, db: AsyncSession = Depends(get_db)
):
    """Analyse active leads and generate a prioritised call queue for the day.

    Ranks by email engagement (opens/clicks/replies), recency since last
    contact, lead score, and industry priority. Idempotent for the day —
    contacts with an open call task are skipped.
    """
    payload = payload or QueueGenerateRequest()
    result = await generate_call_queue_for_session(
        db,
        assigned_to=payload.assigned_to,
        limit=payload.limit,
        stages=payload.stages,
    )
    return QueueGenerateResponse(
        generated=result["generated"],
        hot=result["hot"],
        warm=result["warm"],
        cold=result["cold"],
        skipped_existing=result["skipped_existing"],
        tasks=[CallTaskRead.model_validate(t) for t in result["tasks"]],
    )
