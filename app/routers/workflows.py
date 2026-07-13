"""AI Workflow Builder API (feature 4.3).

CRUD for user-defined event-triggered workflows, plus run history, a manual
``run`` (test against a payload without waiting for a real event), and an
``emit`` endpoint that publishes an internal event through the event bus so any
matching workflows across the tenant execute.

Workflows and runs are scoped to the caller's tenant (``org_id``). Step and
trigger validation keeps a malformed workflow from being saved: every step must
name a known action from the engine's registry.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.workflow import Workflow, WorkflowRun
from app.services.event_bus import get_event_bus
from app.services.workflow_engine import ACTIONS, get_workflow_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workflows"])


# --- Schemas ---------------------------------------------------------------


class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    trigger_event: str = Field(..., min_length=1, max_length=80)
    trigger_conditions: dict = Field(default_factory=dict)
    steps: list[dict] = Field(default_factory=list)
    is_active: bool = True
    max_runs_per_day: Optional[int] = None


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    trigger_event: Optional[str] = None
    trigger_conditions: Optional[dict] = None
    steps: Optional[list[dict]] = None
    is_active: Optional[bool] = None
    max_runs_per_day: Optional[int] = None


class WorkflowRead(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str]
    trigger_event: str
    trigger_conditions: dict
    steps: list[dict]
    is_active: bool
    max_runs_per_day: Optional[int]

    class Config:
        from_attributes = True


class RunRequest(BaseModel):
    payload: dict = Field(default_factory=dict)


class EmitRequest(BaseModel):
    event_type: str
    payload: dict = Field(default_factory=dict)


def _to_read(w: Workflow) -> WorkflowRead:
    return WorkflowRead(
        id=w.id,
        name=w.name,
        description=w.description,
        trigger_event=w.trigger_event,
        trigger_conditions=w.trigger_conditions or {},
        steps=w.steps or [],
        is_active=w.is_active,
        max_runs_per_day=w.max_runs_per_day,
    )


def _validate_steps(steps: list[dict]) -> None:
    """Every step must name a known action from the engine registry."""
    for idx, step in enumerate(steps or []):
        if not isinstance(step, dict):
            raise HTTPException(400, f"step {idx} must be an object")
        action = step.get("action")
        if action not in ACTIONS:
            raise HTTPException(
                400,
                f"step {idx}: unknown action {action!r}. Available: {sorted(ACTIONS)}",
            )


# --- Metadata --------------------------------------------------------------


@router.get("/workflows/actions")
async def list_actions(_: User = Depends(get_current_user)):
    """List the action keys workflows can use as steps."""
    return {"actions": sorted(ACTIONS.keys())}


# --- CRUD ------------------------------------------------------------------


@router.post("/workflows", response_model=WorkflowRead, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a workflow for the caller's tenant."""
    _validate_steps(body.steps)
    wf = Workflow(
        org_id=current_user.org_id,
        name=body.name,
        description=body.description,
        trigger_event=body.trigger_event,
        trigger_conditions=body.trigger_conditions or {},
        steps=body.steps or [],
        is_active=body.is_active,
        max_runs_per_day=body.max_runs_per_day,
        created_by=current_user.id,
    )
    db.add(wf)
    await db.flush()
    return _to_read(wf)


@router.get("/workflows", response_model=list[WorkflowRead])
async def list_workflows(
    active_only: bool = Query(False),
    trigger_event: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the tenant's workflows."""
    query = select(Workflow).where(Workflow.org_id == current_user.org_id)
    if active_only:
        query = query.where(Workflow.is_active.is_(True))
    if trigger_event:
        query = query.where(Workflow.trigger_event == trigger_event)
    query = query.order_by(Workflow.created_at.desc())
    rows = (await db.execute(query)).scalars().all()
    return [_to_read(w) for w in rows]


async def _get_workflow_or_404(
    db: AsyncSession, workflow_id: uuid.UUID, org_id: Optional[uuid.UUID]
) -> Workflow:
    wf = (
        await db.execute(select(Workflow).where(Workflow.id == workflow_id))
    ).scalar_one_or_none()
    if wf is None or wf.org_id != org_id:
        raise HTTPException(404, "Workflow not found")
    return wf


@router.get("/workflows/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(
    workflow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _to_read(await _get_workflow_or_404(db, workflow_id, current_user.org_id))


@router.patch("/workflows/{workflow_id}", response_model=WorkflowRead)
async def update_workflow(
    workflow_id: uuid.UUID,
    body: WorkflowUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a workflow definition."""
    wf = await _get_workflow_or_404(db, workflow_id, current_user.org_id)
    if body.steps is not None:
        _validate_steps(body.steps)
        wf.steps = body.steps
    if body.name is not None:
        wf.name = body.name
    if body.description is not None:
        wf.description = body.description
    if body.trigger_event is not None:
        wf.trigger_event = body.trigger_event
    if body.trigger_conditions is not None:
        wf.trigger_conditions = body.trigger_conditions
    if body.is_active is not None:
        wf.is_active = body.is_active
    if body.max_runs_per_day is not None:
        wf.max_runs_per_day = body.max_runs_per_day
    await db.flush()
    return _to_read(wf)


@router.delete("/workflows/{workflow_id}", status_code=200)
async def delete_workflow(
    workflow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    wf = await _get_workflow_or_404(db, workflow_id, current_user.org_id)
    await db.delete(wf)
    await db.flush()
    return {"status": "deleted", "workflow_id": str(workflow_id)}


# --- Execution -------------------------------------------------------------


@router.post("/workflows/{workflow_id}/run")
async def run_workflow(
    workflow_id: uuid.UUID,
    body: RunRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a workflow immediately against a supplied payload (test/manual).

    Bypasses trigger-event matching but still evaluates step conditions.
    """
    wf = await _get_workflow_or_404(db, workflow_id, current_user.org_id)
    run = await get_workflow_engine().execute_workflow(db, wf, body.payload, current_user.org_id)
    return _run_read(run)


@router.post("/workflows/emit")
async def emit_event(
    body: EmitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Publish an internal event; runs every matching workflow for the tenant."""
    result = await get_event_bus().publish(
        db, body.event_type, body.payload, current_user.org_id
    )
    return result


@router.get("/workflows/{workflow_id}/runs")
async def list_runs(
    workflow_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List execution history for a workflow, most recent first."""
    await _get_workflow_or_404(db, workflow_id, current_user.org_id)
    rows = (
        await db.execute(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(desc(WorkflowRun.started_at))
            .limit(limit)
        )
    ).scalars().all()
    return [_run_read(r) for r in rows]


def _run_read(run: WorkflowRun) -> dict:
    return {
        "id": str(run.id),
        "workflow_id": str(run.workflow_id),
        "status": run.status,
        "trigger_payload": run.trigger_payload,
        "step_results": run.step_results,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }
