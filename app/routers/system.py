"""System control API — global sending kill switch, warm-up reset, automation state.

The kill switch is the launch-blocker safety valve: one toggle that immediately
halts ALL autonomous outbound email and pauses active enrollments, plus its
inverse to resume. Reads are public-to-authenticated; writes capture who flipped
it and why for the audit trail.

``/system/automation`` reports which autonomous behaviours are actually on. It
exists because the Settings page used to hardcode those badges, and two of the
three were wrong — follow-up generation and lead re-scoring both run on the beat
schedule while the UI displayed them as OFF.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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


class AutomationFlag(BaseModel):
    """One autonomous behaviour, and whether it is actually running."""

    key: str
    label: str
    enabled: bool
    # How it's controlled, so the UI can say whether an operator can change it:
    # "env" = a config/environment flag, "schedule" = a Celery beat task that
    # runs unconditionally and is tuned by cadence rather than switched off.
    source: str
    # The cadence or setting behind `enabled` — a bare ON tells an operator
    # nothing about how often, or what would change it.
    detail: Optional[str] = None


class AutomationResponse(BaseModel):
    """What the agent does on its own, without anyone asking.

    A list rather than fixed fields: the vocabulary belongs to the backend, so
    adding a behaviour here surfaces it in the UI without a frontend change —
    and, more importantly, nothing renders unless the server actually reported
    it. The previous UI invented these values client-side.
    """

    flags: list[AutomationFlag]
    scheduling_link: str = ""


@router.get("/automation", response_model=AutomationResponse)
async def automation_settings():
    """Report the real state of each autonomous behaviour.

    Every value here is read from config or from the beat schedule. Nothing is
    defaulted for display: if a flag can't be determined it does not appear.
    """
    settings = get_settings()
    flags = [
        AutomationFlag(
            key="auto_reply",
            label="Auto-reply to interested replies",
            enabled=settings.auto_reply_enabled,
            source="env",
            detail=(
                "AI drafts a reply and sends it for clear positive intent."
                if settings.auto_reply_enabled
                else "Replies are classified but never answered automatically."
            ),
        ),
        AutomationFlag(
            key="ai_compose",
            label="AI-composed outreach",
            enabled=settings.ai_compose_enabled,
            source="env",
            detail=(
                (
                    f"First {settings.ai_compose_review_batch_size} per sequence step go to "
                    "human review; "
                    + (
                        "every step is composed in the sequence's tone."
                        if settings.ai_compose_all_steps
                        else "step 1 only — steps 2+ send their static templates."
                    )
                )
                if settings.ai_compose_enabled
                else "Sequences send their static templates."
            ),
        ),
        AutomationFlag(
            key="auto_research",
            label="Auto-research new companies",
            enabled=settings.auto_research_enabled,
            source="env",
            detail=(
                "A research brief is generated before first outreach."
                if settings.auto_research_enabled
                else "Briefs are only generated on request."
            ),
        ),
        # These two are beat tasks with no on/off switch — they run. The old UI
        # showed both as OFF, which was simply false.
        AutomationFlag(
            key="auto_followups",
            label="Stale-lead follow-ups",
            enabled=True,
            source="schedule",
            detail=(
                f"Every 4h, up to {settings.auto_followup_batch_limit} reminders for leads untouched "
                f"for {settings.stale_lead_followup_days}+ days."
            ),
        ),
        AutomationFlag(
            key="auto_scoring",
            label="Lead scoring & re-scoring",
            enabled=True,
            source="schedule",
            detail=(
                f"New leads scored every 30m; active leads re-scored twice daily, up to "
                f"{settings.rescore_batch_limit} per run."
            ),
        ),
    ]
    return AutomationResponse(flags=flags, scheduling_link=settings.scheduling_link or "")


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
