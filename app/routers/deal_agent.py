"""Deal Progression Agent API — config, action queue, approvals, stats.

Mounted at ``/api/v1/deal-agent``. Reads are auto-scoped to the caller's org by
the global filter; config writes are gated to ``org_admin`` and up.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.company import Company
from app.models.deal import Deal
from app.models.deal_progression_agent import (
    ACTION_STATUSES,
    ACTION_TYPES,
    DealAction,
)
from app.models.user import UserRole
from app.schemas.deal_agent import (
    AgentConfigRead,
    AgentConfigUpdate,
    AgentStats,
    DealActionRead,
)
from app.services import deal_progression_agent as agent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deal-agent", tags=["deal-agent"])


@router.get("/config", response_model=AgentConfigRead)
async def get_config(db: AsyncSession = Depends(get_db)):
    """Return the org's agent config (created with defaults on first read)."""
    config = await agent.get_or_create_config(db)
    await db.commit()
    return config


@router.put("/config", response_model=AgentConfigRead)
async def update_config(
    payload: AgentConfigUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update the org's agent config. Requires ``org_admin`` or higher."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    config = await agent.get_or_create_config(db)

    updates = payload.model_dump(exclude_unset=True)
    if "stale_thresholds" in updates and updates["stale_thresholds"] is not None:
        # Merge onto existing so a partial threshold map doesn't drop stages.
        merged = dict(config.stale_thresholds or {})
        merged.update(updates.pop("stale_thresholds"))
        config.stale_thresholds = merged
    for field, value in updates.items():
        setattr(config, field, value)

    await db.commit()
    await db.refresh(config)
    return config


async def _serialize_actions(
    db: AsyncSession, rows: list[DealAction]
) -> list[DealActionRead]:
    """Attach deal title + company name to each action for the queue view."""
    if not rows:
        return []
    deal_ids = {a.deal_id for a in rows}
    deal_rows = (
        await db.execute(
            select(Deal.id, Deal.title, Company.name)
            .join(Company, Deal.company_id == Company.id, isouter=True)
            .where(Deal.id.in_(deal_ids))
        )
    ).all()
    meta = {did: (title, cname) for did, title, cname in deal_rows}

    out = []
    for a in rows:
        item = DealActionRead.model_validate(a)
        title, cname = meta.get(a.deal_id, (None, None))
        item.deal_title = title
        item.company_name = cname
        out.append(item)
    return out


@router.get("/actions", response_model=list[DealActionRead])
async def list_actions(
    action_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    deal_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List agent actions, newest first, filterable by type / status / deal."""
    if action_type is not None and action_type not in ACTION_TYPES:
        raise HTTPException(status_code=422, detail=f"invalid action_type '{action_type}'")
    if status is not None and status not in ACTION_STATUSES:
        raise HTTPException(status_code=422, detail=f"invalid status '{status}'")

    query = select(DealAction).order_by(DealAction.created_at.desc())
    if action_type:
        query = query.where(DealAction.action_type == action_type)
    if status:
        query = query.where(DealAction.status == status)
    if deal_id:
        query = query.where(DealAction.deal_id == deal_id)
    query = query.limit(limit)

    rows = (await db.execute(query)).scalars().all()
    return await _serialize_actions(db, list(rows))


async def _get_action(db: AsyncSession, action_id: uuid.UUID) -> DealAction:
    action = (
        await db.execute(select(DealAction).where(DealAction.id == action_id))
    ).scalars().first()
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@router.post("/actions/{action_id}/approve", response_model=DealActionRead)
async def approve_action(action_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Approve an action (auto-follow-ups are sent immediately)."""
    action = await _get_action(db, action_id)
    if action.status in ("dismissed", "expired"):
        raise HTTPException(
            status_code=409, detail=f"Cannot approve a {action.status} action"
        )
    await agent.approve_action(db, action)
    await db.commit()
    await db.refresh(action)
    return (await _serialize_actions(db, [action]))[0]


@router.post("/actions/{action_id}/dismiss", response_model=DealActionRead)
async def dismiss_action(action_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Dismiss a pending action — reject the agent's suggestion."""
    action = await _get_action(db, action_id)
    if action.status == "executed":
        raise HTTPException(status_code=409, detail="Cannot dismiss an executed action")
    await agent.dismiss_action(db, action)
    await db.commit()
    await db.refresh(action)
    return (await _serialize_actions(db, [action]))[0]


@router.get("/stats", response_model=AgentStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Aggregate counts: deals nudged, follow-ups sent, advances, at-risk."""
    return await agent.compute_stats(db)
