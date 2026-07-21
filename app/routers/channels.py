"""Multi-channel step execution API (P2 audit — multichannel execution).

Exposes the channel dispatcher so a cadence step (or a manual touch) can be
executed on any channel — email for real, LinkedIn / phone / SMS through
provider stubs — and its per-touch status/reply tracked afterwards.

    POST   /channels/execute                  -> run one channel touch
    GET    /channels/executions               -> list (filter by enrollment/channel/status)
    GET    /channels/executions/{id}          -> one execution
    POST   /channels/executions/{id}/status   -> refresh provider status
    POST   /channels/executions/{id}/reply    -> attach an inbound reply
    GET    /channels/handlers                  -> registered channels + configured flag
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
from app.models.channel_execution import (
    CHANNEL_EXECUTION_CHANNELS,
    ChannelStepExecution,
)
from app.services.channels import dispatcher

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])


# --- Schemas ---

class ExecuteRequest(BaseModel):
    channel: str = Field(..., description="email | linkedin | phone | sms")
    action: str = Field("message", max_length=40)
    payload: dict = Field(default_factory=dict)
    enrollment_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    step_number: Optional[int] = None


class ReplyRequest(BaseModel):
    reply: dict = Field(default_factory=dict)


class ExecutionRead(BaseModel):
    id: uuid.UUID
    enrollment_id: Optional[uuid.UUID] = None
    contact_id: Optional[uuid.UUID] = None
    step_number: Optional[int] = None
    channel: str
    action: str
    provider: Optional[str] = None
    status: str
    external_id: Optional[str] = None
    payload: dict = {}
    result: dict = {}
    reply: Optional[dict] = None
    error: Optional[str] = None
    created_at: datetime
    sent_at: Optional[datetime] = None
    status_checked_at: Optional[datetime] = None

    class Config:
        from_attributes = True


async def _get_execution(db: AsyncSession, execution_id: uuid.UUID) -> ChannelStepExecution:
    row = (
        await db.execute(
            select(ChannelStepExecution).where(ChannelStepExecution.id == execution_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Channel execution not found")
    return row


@router.get("/handlers", response_model=dict)
async def list_handlers():
    """Registered channels and whether each has a real integration configured."""
    out = {}
    for channel in CHANNEL_EXECUTION_CHANNELS:
        handler = dispatcher.handler_for_channel(channel)
        out[channel] = {
            "provider": handler.provider if handler else None,
            "configured": bool(handler.configured) if handler else False,
        }
    return {"channels": out}


@router.post("/execute", response_model=ExecutionRead, status_code=201)
async def execute_step(payload: ExecuteRequest, db: AsyncSession = Depends(get_db)):
    """Execute one channel touch and return the recorded execution."""
    if payload.channel not in CHANNEL_EXECUTION_CHANNELS:
        raise HTTPException(
            status_code=422,
            detail=f"channel must be one of {list(CHANNEL_EXECUTION_CHANNELS)}",
        )
    try:
        execution = await dispatcher.dispatch_channel_step(
            db,
            channel=payload.channel,
            action=payload.action,
            payload=payload.payload,
            enrollment_id=payload.enrollment_id,
            contact_id=payload.contact_id,
            step_number=payload.step_number,
        )
    except dispatcher.UnknownChannelError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return execution


@router.get("/executions", response_model=list[ExecutionRead])
async def list_executions(
    enrollment_id: Optional[uuid.UUID] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    channel: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List channel executions, newest first, with optional filters."""
    query = select(ChannelStepExecution).order_by(ChannelStepExecution.created_at.desc())
    if enrollment_id is not None:
        query = query.where(ChannelStepExecution.enrollment_id == enrollment_id)
    if contact_id is not None:
        query = query.where(ChannelStepExecution.contact_id == contact_id)
    if channel is not None:
        query = query.where(ChannelStepExecution.channel == channel)
    if status is not None:
        query = query.where(ChannelStepExecution.status == status)
    rows = (await db.execute(query.limit(limit))).scalars().all()
    return list(rows)


@router.get("/executions/{execution_id}", response_model=ExecutionRead)
async def get_execution(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _get_execution(db, execution_id)


@router.post("/executions/{execution_id}/status", response_model=ExecutionRead)
async def refresh_status(execution_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Poll the channel handler for the latest delivery/engagement status."""
    execution = await _get_execution(db, execution_id)
    handler = dispatcher.get_handler(execution.channel)
    await handler.check_status(db, execution)
    await db.flush()
    return execution


@router.post("/executions/{execution_id}/reply", response_model=ExecutionRead)
async def record_reply(
    execution_id: uuid.UUID, body: ReplyRequest, db: AsyncSession = Depends(get_db)
):
    """Attach an inbound reply to a touch and mark it replied."""
    execution = await _get_execution(db, execution_id)
    handler = dispatcher.get_handler(execution.channel)
    await handler.handle_reply(db, execution, body.reply)
    await db.flush()
    return execution
