"""System control API — global sending kill switch + warm-up reset.

The kill switch is the launch-blocker safety valve: one toggle that immediately
halts ALL autonomous outbound email and pauses active enrollments, plus its
inverse to resume. Reads are public-to-authenticated; writes capture who flipped
it and why for the audit trail.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models.user import User
from app.services import system_settings
from app.services.send_limiter import get_send_limiter

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/system", tags=["system"])


class SendingToggleRequest(BaseModel):
    enabled: bool = Field(..., description="True to allow sending, False to halt all sending")
    reason: Optional[str] = Field(None, max_length=500, description="Why the switch was flipped")


class SendingStatusResponse(BaseModel):
    enabled: bool
    reason: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None
    # Rolling send stats for context on the dashboard.
    today_sent: Optional[int] = None
    daily_limit: Optional[int] = None
    daily_remaining: Optional[int] = None
    warmup_active: Optional[bool] = None
    warmup_week: Optional[int] = None
    enrollments_affected: Optional[int] = None


def _actor(user: Optional[User]) -> Optional[str]:
    return user.email if user is not None else None


@router.get("/sending-status", response_model=SendingStatusResponse)
async def sending_status(db: AsyncSession = Depends(get_db)):
    """Current kill-switch state plus rolling send stats."""
    status = await system_settings.get_sending_status(db)
    try:
        stats = await get_send_limiter().get_send_stats(db)
    except Exception as exc:  # pragma: no cover - stats are best-effort context
        logger.info("send stats unavailable: %s", exc)
        stats = {}
    status.update(
        {
            "today_sent": stats.get("today_sent"),
            "daily_limit": stats.get("daily_limit"),
            "daily_remaining": stats.get("daily_remaining"),
            "warmup_active": stats.get("warmup_active"),
            "warmup_week": stats.get("warmup_week"),
        }
    )
    return status


@router.post("/sending-toggle", response_model=SendingStatusResponse)
async def sending_toggle(
    payload: SendingToggleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Engage or release the global sending kill switch.

    Disabling immediately pauses every active enrollment; enabling resumes only
    those the kill switch paused.
    """
    status = await system_settings.set_sending_enabled(
        db, payload.enabled, updated_by=_actor(current_user), reason=payload.reason
    )
    await db.commit()
    return status


class WarmupResetResponse(BaseModel):
    warmup_start_date: str


@router.post("/reset-warmup", response_model=WarmupResetResponse)
async def reset_warmup(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Reset the sender warm-up ramp to start today."""
    date = await system_settings.reset_warmup_start_date(db, updated_by=_actor(current_user))
    await db.commit()
    return {"warmup_start_date": date}
