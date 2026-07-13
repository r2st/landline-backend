"""Autonomous orchestration API endpoints (feature 2.2).

- OrchestrationPolicy CRUD — the guardrails the autonomous engine runs within.
- Set an enrollment's orchestration mode (manual/assisted/autonomous).
- Dry-run the next TouchDecision for an enrollment (no side effects).
- Read an enrollment's decision log.
- Trigger a bounded orchestration run over autonomous enrollments.
"""

import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.orchestration import ORCHESTRATION_CHANNELS, OrchestrationPolicy
from app.models.outreach import OutreachEnrollment
from app.services.orchestrator import get_orchestrator

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/orchestration", tags=["orchestration"])

_MODES = ("manual", "assisted", "autonomous")


# --- Schemas ---

class PolicyCreate(BaseModel):
    name: str = Field("Default policy", max_length=120)
    user_id: Optional[uuid.UUID] = None
    max_daily_touches: int = Field(2, ge=1, le=20)
    max_total_touches: int = Field(12, ge=1, le=100)
    min_hours_between_touches: int = Field(24, ge=1, le=720)
    allowed_channels: list[str] = Field(default_factory=lambda: list(ORCHESTRATION_CHANNELS))
    required_channels: list[str] = Field(default_factory=list)
    human_review_triggers: list[str] = Field(default_factory=list)
    is_active: bool = True


class PolicyUpdate(BaseModel):
    name: Optional[str] = None
    max_daily_touches: Optional[int] = Field(None, ge=1, le=20)
    max_total_touches: Optional[int] = Field(None, ge=1, le=100)
    min_hours_between_touches: Optional[int] = Field(None, ge=1, le=720)
    allowed_channels: Optional[list[str]] = None
    required_channels: Optional[list[str]] = None
    human_review_triggers: Optional[list[str]] = None
    is_active: Optional[bool] = None


class PolicyRead(BaseModel):
    id: uuid.UUID
    user_id: Optional[uuid.UUID] = None
    name: str
    is_active: bool
    max_daily_touches: int
    max_total_touches: int
    min_hours_between_touches: int
    allowed_channels: list = []
    required_channels: list = []
    human_review_triggers: list = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ModeUpdate(BaseModel):
    mode: str

    def validated(self) -> str:
        if self.mode not in _MODES:
            raise HTTPException(status_code=422, detail=f"mode must be one of {_MODES}")
        return self.mode


def _validate_channels(channels: Optional[list[str]]) -> None:
    if channels is None:
        return
    bad = [c for c in channels if c not in ORCHESTRATION_CHANNELS]
    if bad:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown channel(s) {bad}; allowed: {list(ORCHESTRATION_CHANNELS)}",
        )


# --- Policy CRUD ---

@router.get("/policies", response_model=list[PolicyRead])
async def list_policies(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    query = select(OrchestrationPolicy).order_by(OrchestrationPolicy.created_at.desc())
    if active_only:
        query = query.where(OrchestrationPolicy.is_active.is_(True))
    return list((await db.execute(query)).scalars().all())


@router.post("/policies", response_model=PolicyRead, status_code=201)
async def create_policy(payload: PolicyCreate, db: AsyncSession = Depends(get_db)):
    _validate_channels(payload.allowed_channels)
    _validate_channels(payload.required_channels)
    policy = OrchestrationPolicy(**payload.model_dump())
    db.add(policy)
    await db.flush()
    await db.refresh(policy)
    return policy


@router.get("/policies/{policy_id}", response_model=PolicyRead)
async def get_policy(policy_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    policy = (
        await db.execute(select(OrchestrationPolicy).where(OrchestrationPolicy.id == policy_id))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    return policy


@router.patch("/policies/{policy_id}", response_model=PolicyRead)
async def update_policy(
    policy_id: uuid.UUID, payload: PolicyUpdate, db: AsyncSession = Depends(get_db)
):
    policy = (
        await db.execute(select(OrchestrationPolicy).where(OrchestrationPolicy.id == policy_id))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    _validate_channels(payload.allowed_channels)
    _validate_channels(payload.required_channels)
    for field_name, value in payload.model_dump(exclude_unset=True).items():
        setattr(policy, field_name, value)
    await db.flush()
    await db.refresh(policy)
    return policy


@router.delete("/policies/{policy_id}", status_code=204)
async def delete_policy(policy_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    policy = (
        await db.execute(select(OrchestrationPolicy).where(OrchestrationPolicy.id == policy_id))
    ).scalar_one_or_none()
    if policy is None:
        raise HTTPException(status_code=404, detail="Policy not found")
    await db.delete(policy)
    await db.flush()


# --- Enrollment orchestration ---

async def _get_enrollment(db: AsyncSession, enrollment_id: uuid.UUID) -> OutreachEnrollment:
    enrollment = (
        await db.execute(select(OutreachEnrollment).where(OutreachEnrollment.id == enrollment_id))
    ).scalar_one_or_none()
    if enrollment is None:
        raise HTTPException(status_code=404, detail="Enrollment not found")
    return enrollment


@router.post("/enrollments/{enrollment_id}/mode", response_model=dict)
async def set_mode(
    enrollment_id: uuid.UUID, payload: ModeUpdate, db: AsyncSession = Depends(get_db)
):
    """Set an enrollment's orchestration mode (manual/assisted/autonomous)."""
    mode = payload.validated()
    enrollment = await _get_enrollment(db, enrollment_id)
    enrollment.orchestration_mode = mode
    await db.flush()
    return {"enrollment_id": str(enrollment.id), "orchestration_mode": mode}


@router.post("/enrollments/{enrollment_id}/decide", response_model=dict)
async def decide_next_touch(
    enrollment_id: uuid.UUID,
    use_ai: bool = Query(True),
    db: AsyncSession = Depends(get_db),
):
    """Dry-run: compute the next TouchDecision for an enrollment (no side effects)."""
    enrollment = await _get_enrollment(db, enrollment_id)
    orch = get_orchestrator()
    decision = await orch.decide(db, enrollment, use_ai=use_ai)
    return {"enrollment_id": str(enrollment.id), "decision": decision.as_log_entry()}


@router.get("/enrollments/{enrollment_id}/log", response_model=dict)
async def get_log(enrollment_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Return the enrollment's append-only orchestration decision log."""
    enrollment = await _get_enrollment(db, enrollment_id)
    return {
        "enrollment_id": str(enrollment.id),
        "orchestration_mode": enrollment.orchestration_mode,
        "log": list(enrollment.orchestration_log or []),
    }


@router.post("/run", response_model=dict)
async def run_orchestration(
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Trigger a bounded orchestration run over autonomous-mode enrollments."""
    orch = get_orchestrator()
    results = await orch.run_all_autonomous(db, limit=limit)
    return {"processed": len(results), "results": results}
