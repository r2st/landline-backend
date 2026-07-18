"""Referral & Warm-Intro agent API (``/api/v1/referral-agent``).

Config, the referral pipeline, and the action queue for the autonomous referral
agent. Reads are org-filtered globally (see :mod:`app.models.mixins`); config
writes and action execution require ``org_admin``.

Routes:
    GET  /config                 — agent config (llm_api_key masked)
    PUT  /config                 — update config (org_admin)
    GET  /referrals              — list referrals (filter: status, referrer)
    POST /referrals              — manually add a referral
    PUT  /referrals/{id}         — update a referral (status/fields)
    GET  /actions                — pending actions (filter: status, type)
    POST /actions/{id}/approve   — approve + send an action (org_admin)
    POST /actions/{id}/dismiss   — dismiss an action (org_admin)
    GET  /stats                  — pipeline stats + totals
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.company import Company
from app.models.contact import Contact
from app.models.referral import (
    REFERRAL_LLM_MODELS,
    REFERRAL_STATUSES,
    Referral,
    ReferralAction,
)
from app.schemas.referral import (
    ActionRead,
    ActionResult,
    AgentConfigRead,
    AgentConfigUpdate,
    ReferralCreate,
    ReferralRead,
    ReferralStats,
    ReferralUpdate,
)
from app.services import referral_agent as svc
from app.services.crypto import encrypt

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/referral-agent", tags=["referral-agent"])


# --- helpers --------------------------------------------------------------
def _config_read(agent) -> AgentConfigRead:
    data = AgentConfigRead.model_validate(agent)
    data.has_api_key = bool(agent.llm_api_key)
    return data


async def _contact_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict:
    """Map contact id → display name for a set of ids (one query)."""
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Contact.id, Contact.first_name, Contact.last_name).where(
                Contact.id.in_(ids)
            )
        )
    ).all()
    out: dict = {}
    for cid, first, last in rows:
        name = " ".join(p for p in (first or "", last or "") if p).strip()
        out[cid] = name or None
    return out


def _referral_read(referral: Referral, names: dict) -> ReferralRead:
    data = ReferralRead.model_validate(referral)
    data.referrer_name = names.get(referral.referrer_contact_id)
    return data


def _action_read(action: ReferralAction, names: dict) -> ActionRead:
    data = ActionRead.model_validate(action)
    data.referrer_name = names.get(action.referrer_contact_id)
    return data


# --- config ---------------------------------------------------------------
@router.get("/config", response_model=AgentConfigRead)
async def get_config(db: AsyncSession = Depends(get_db)):
    """The caller-org's agent config (creating a default row on first access)."""
    agent = await svc.get_or_create_agent(db)
    return _config_read(agent)


@router.put("/config", response_model=AgentConfigRead)
async def update_config(
    payload: AgentConfigUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update agent config. Requires ``org_admin``. The ``llm_api_key`` is stored
    encrypted; passing an empty string clears it, omitting it leaves it as-is."""
    scope.check_permission("org_admin")
    agent = await svc.get_or_create_agent(db)

    fields = payload.model_dump(exclude_unset=True)

    if "llm_model" in fields and fields["llm_model"] not in REFERRAL_LLM_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"llm_model must be one of {list(REFERRAL_LLM_MODELS)}",
        )

    # API key handling: "" clears, non-empty encrypts + stores, absent = no change.
    if "llm_api_key" in fields:
        raw = (fields.pop("llm_api_key") or "").strip()
        agent.llm_api_key = encrypt(raw) if raw else None

    for key, value in fields.items():
        setattr(agent, key, value)

    await db.flush()
    # Reload server-side onupdate columns (updated_at) in async context so the
    # sync pydantic serialization below doesn't trigger a lazy IO load.
    await db.refresh(agent)
    return _config_read(agent)


# --- referrals ------------------------------------------------------------
@router.get("/referrals", response_model=list[ReferralRead])
async def list_referrals(
    db: AsyncSession = Depends(get_db),
    status_filter: Optional[str] = Query(None, alias="status"),
    referrer: Optional[uuid.UUID] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """List referrals, newest first, optionally filtered by status / referrer."""
    stmt = select(Referral).order_by(Referral.created_at.desc())
    if status_filter:
        if status_filter not in REFERRAL_STATUSES:
            raise HTTPException(status_code=422, detail="Unknown status filter")
        stmt = stmt.where(Referral.status == status_filter)
    if referrer:
        stmt = stmt.where(Referral.referrer_contact_id == referrer)
    stmt = stmt.limit(limit)

    referrals = (await db.execute(stmt)).scalars().all()
    names = await _contact_names(db, {r.referrer_contact_id for r in referrals})
    return [_referral_read(r, names) for r in referrals]


@router.post("/referrals", response_model=ReferralRead, status_code=status.HTTP_201_CREATED)
async def create_referral(
    payload: ReferralCreate,
    db: AsyncSession = Depends(get_db),
):
    """Manually record a referral (e.g. one that came in over the phone)."""
    if payload.status not in REFERRAL_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown status")

    # Validate referrer belongs to the caller's org (global filter → own org only).
    if payload.referrer_contact_id is not None:
        exists = (
            await db.execute(
                select(Contact.id).where(Contact.id == payload.referrer_contact_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="Referrer contact not found")

    now = datetime.now(timezone.utc)
    referral = Referral(
        referrer_contact_id=payload.referrer_contact_id,
        referred_name=payload.referred_name,
        referred_email=payload.referred_email,
        referred_company=payload.referred_company,
        status=payload.status,
        referral_value=payload.referral_value,
        notes=payload.notes,
        received_at=now if payload.status in ("received", "converted") else None,
        converted_at=now if payload.status == "converted" else None,
    )
    db.add(referral)
    await db.flush()
    names = await _contact_names(db, {referral.referrer_contact_id})
    return _referral_read(referral, names)


@router.put("/referrals/{referral_id}", response_model=ReferralRead)
async def update_referral(
    referral_id: uuid.UUID,
    payload: ReferralUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a referral's status or fields. Stamps lifecycle timestamps."""
    referral = (
        await db.execute(select(Referral).where(Referral.id == referral_id))
    ).scalar_one_or_none()
    if referral is None:
        raise HTTPException(status_code=404, detail="Referral not found")

    fields = payload.model_dump(exclude_unset=True)
    new_status = fields.get("status")
    if new_status is not None and new_status not in REFERRAL_STATUSES:
        raise HTTPException(status_code=422, detail="Unknown status")

    now = datetime.now(timezone.utc)
    for key, value in fields.items():
        setattr(referral, key, value)

    # Stamp lifecycle timestamps when the status advances.
    if new_status == "asked" and referral.ask_sent_at is None:
        referral.ask_sent_at = now
    elif new_status == "received" and referral.received_at is None:
        referral.received_at = now
    elif new_status == "converted" and referral.converted_at is None:
        referral.converted_at = now
        referral.received_at = referral.received_at or now

    await db.flush()
    await db.refresh(referral)  # reload updated_at (onupdate) in async context
    names = await _contact_names(db, {referral.referrer_contact_id})
    return _referral_read(referral, names)


# --- actions --------------------------------------------------------------
@router.get("/actions", response_model=list[ActionRead])
async def list_actions(
    db: AsyncSession = Depends(get_db),
    status_filter: str = Query("pending", alias="status"),
    action_type: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    """List agent actions (defaults to pending), newest first."""
    stmt = select(ReferralAction).order_by(ReferralAction.created_at.desc())
    if status_filter and status_filter != "all":
        stmt = stmt.where(ReferralAction.status == status_filter)
    if action_type:
        stmt = stmt.where(ReferralAction.action_type == action_type)
    stmt = stmt.limit(limit)

    actions = (await db.execute(stmt)).scalars().all()
    names = await _contact_names(db, {a.referrer_contact_id for a in actions})
    return [_action_read(a, names) for a in actions]


@router.post("/actions/{action_id}/approve", response_model=ActionResult)
async def approve_action(
    action_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Approve a pending action, send its email, and advance the referral.

    Requires ``org_admin``. Sends via the shared outreach dispatcher (which
    honours the global kill switch); on a non-``sent`` transport result the
    action stays ``pending`` so it can be retried."""
    scope.check_permission("org_admin")

    action = (
        await db.execute(select(ReferralAction).where(ReferralAction.id == action_id))
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action already {action.status}")

    result = await svc_execute_action(db, action)
    names = await _contact_names(db, {action.referrer_contact_id})
    return ActionResult(
        action=_action_read(action, names),
        send_status=result.get("status"),
        detail=result.get("reason") or result.get("detail"),
    )


@router.post("/actions/{action_id}/dismiss", response_model=ActionRead)
async def dismiss_action(
    action_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a pending action without sending. Requires ``org_admin``."""
    scope.check_permission("org_admin")
    action = (
        await db.execute(select(ReferralAction).where(ReferralAction.id == action_id))
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if action.status != "pending":
        raise HTTPException(status_code=409, detail=f"Action already {action.status}")

    action.status = "dismissed"
    await db.flush()
    names = await _contact_names(db, {action.referrer_contact_id})
    return _action_read(action, names)


async def svc_execute_action(db: AsyncSession, action: ReferralAction) -> dict:
    """Send an action's email and advance its + the referral's state.

    Shared by the approve endpoint and the auto-ask task. Returns the dispatcher
    result dict. On success marks the action ``sent`` and moves the referral to
    ``asked`` (initial_ask) — follow-ups/thank-yous leave the referral status be.
    """
    contact = None
    if action.referrer_contact_id is not None:
        contact = (
            await db.execute(
                select(Contact).where(Contact.id == action.referrer_contact_id)
            )
        ).scalar_one_or_none()
    if contact is None or not contact.email:
        return {"status": "error", "reason": "Referrer contact has no email on file."}

    from app.services.email_dispatch import send_outreach_email

    subject = action.ai_subject or "A quick favor"
    body = action.ai_message or ""
    result = await send_outreach_email(
        db,
        to_email=contact.email,
        subject=subject,
        body_html=body.replace("\n", "<br>"),
        body_text=body,
        include_unsubscribe=True,
    )

    if result.get("status") == "sent":
        now = datetime.now(timezone.utc)
        action.status = "sent"
        action.executed_at = now
        if action.referral_id is not None:
            referral = (
                await db.execute(
                    select(Referral).where(Referral.id == action.referral_id)
                )
            ).scalar_one_or_none()
            if referral is not None and action.action_type == "initial_ask":
                referral.status = "asked"
                referral.ask_sent_at = referral.ask_sent_at or now
        await db.flush()
    return result


# --- stats ----------------------------------------------------------------
@router.get("/stats", response_model=ReferralStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Pipeline counts, response/conversion rates, and total referral value."""
    rows = (
        await db.execute(
            select(Referral.status, func.count(Referral.id)).group_by(Referral.status)
        )
    ).all()
    counts = {s: 0 for s in REFERRAL_STATUSES}
    for st, n in rows:
        counts[st] = n or 0

    total = sum(counts.values())
    asked = counts["asked"] + counts["received"] + counts["converted"] + counts["declined"]
    received_or_better = counts["received"] + counts["converted"]
    response_rate = (received_or_better / asked) if asked else 0.0
    conversion_rate = (counts["converted"] / received_or_better) if received_or_better else 0.0

    total_value = (
        await db.execute(
            select(func.coalesce(func.sum(Referral.referral_value), 0)).where(
                Referral.referral_value.isnot(None)
            )
        )
    ).scalar_one() or 0

    pending_actions = (
        await db.execute(
            select(func.count(ReferralAction.id)).where(
                ReferralAction.status == "pending"
            )
        )
    ).scalar_one() or 0

    return ReferralStats(
        identified=counts["identified"],
        asked=counts["asked"],
        pending=counts["pending"],
        received=counts["received"],
        converted=counts["converted"],
        declined=counts["declined"],
        total=total,
        response_rate=round(response_rate, 4),
        conversion_rate=round(conversion_rate, 4),
        total_referral_value=int(total_value),
        pending_actions=int(pending_actions),
    )
