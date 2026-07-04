"""Task CRUD and an upcoming-tasks feed for the dashboard."""

import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models.company import Company
from app.models.contact import Contact
from app.models.task import Task
from app.models.user import User
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"])

_OPEN_STATUSES = ("pending", "in_progress")


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _validate_refs(db: AsyncSession, company_id: uuid.UUID, contact_id: Optional[uuid.UUID]):
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


@router.post("", response_model=TaskRead, status_code=201)
async def create_task(
    payload: TaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Create a task for a company (optionally tied to a contact)."""
    await _validate_refs(db, payload.company_id, payload.contact_id)
    task = Task(**payload.model_dump())
    if current_user is not None:
        task.user_id = current_user.id
    db.add(task)
    await db.flush()
    await db.refresh(task)
    logger.info("Created task %s for company %s", task.id, payload.company_id)
    return task


@router.get("", response_model=list[TaskRead])
async def list_tasks(
    company_id: Optional[uuid.UUID] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    status: Optional[str] = Query(None, pattern="^(pending|in_progress|completed|cancelled)$"),
    priority: Optional[str] = Query(None, pattern="^(low|medium|high|urgent)$"),
    task_type: Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """List tasks with optional filters, soonest due first (nulls last).

    When called with a JWT Bearer token, scopes to the current user's tasks.
    """
    query = select(Task)
    conditions = []
    if current_user is not None:
        conditions.append(Task.user_id == current_user.id)
    if company_id:
        conditions.append(Task.company_id == company_id)
    if contact_id:
        conditions.append(Task.contact_id == contact_id)
    if status:
        conditions.append(Task.status == status)
    if priority:
        conditions.append(Task.priority == priority)
    if task_type:
        conditions.append(Task.task_type == task_type)
    if assigned_to:
        conditions.append(Task.assigned_to == assigned_to)
    if conditions:
        query = query.where(and_(*conditions))
    query = query.order_by(Task.due_date.is_(None), Task.due_date.asc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.get("/upcoming", response_model=list[TaskRead])
async def upcoming_tasks(
    days: int = Query(7, ge=1, le=365, description="Horizon window in days"),
    include_overdue: bool = Query(True),
    assigned_to: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Open tasks due within ``days`` (optionally including overdue ones).

    Powers the dashboard's "what's next" panel; sorted soonest-due first.
    When called with a JWT Bearer token, scopes to the current user's tasks.
    """
    query = select(Task).where(Task.status.in_(_OPEN_STATUSES), Task.due_date.isnot(None))
    if current_user is not None:
        query = query.where(Task.user_id == current_user.id)
    if assigned_to:
        query = query.where(Task.assigned_to == assigned_to)
    tasks = list((await db.execute(query)).scalars().all())

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    selected = []
    for t in tasks:
        due = _aware(t.due_date)
        if due is None:
            continue
        if due < now:
            if include_overdue:
                selected.append(t)
        elif due <= horizon:
            selected.append(t)

    selected.sort(key=lambda t: _aware(t.due_date))
    return selected[:limit]


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Get a single task by ID."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.patch("/{task_id}", response_model=TaskRead)
async def update_task(
    task_id: uuid.UUID, payload: TaskUpdate, db: AsyncSession = Depends(get_db)
):
    """Update task fields."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    update_data = payload.model_dump(exclude_unset=True)
    if "contact_id" in update_data and update_data["contact_id"] is not None:
        contact = (
            await db.execute(select(Contact.id).where(Contact.id == update_data["contact_id"]))
        ).scalar_one_or_none()
        if not contact:
            raise HTTPException(status_code=404, detail="Contact not found")
    for field, value in update_data.items():
        setattr(task, field, value)
    await db.flush()
    await db.refresh(task)
    logger.info("Updated task %s", task_id)
    return task


@router.delete("/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Delete a task."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    await db.delete(task)
    await db.flush()
    logger.info("Deleted task %s", task_id)
