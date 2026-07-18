"""Social Listening Agent API (`/api/v1/social-listening-agent`).

Manages the per-org buying-intent listening agent: its config + LLM credentials,
the feed of discovered signals, the list of explicitly monitored companies, and
summary stats. The org is always the caller's own (resolved from the JWT); no
route takes an ``org_id``. Config *writes* are gated to ``org_admin``; reads and
signal triage (action/dismiss) are open to any member of the org.
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.social_listening_agent import (
    ALLOWED_LLM_MODELS,
    SIGNAL_STATUSES,
    SocialListeningAgent,
    SignalEvent,
    MonitoredCompany,
)
from app.schemas.social_listening_agent import (
    AgentConfigRead,
    AgentConfigUpdate,
    MonitoredCompanyCreate,
    MonitoredCompanyRead,
    SignalActionResponse,
    SignalRead,
    StatsRead,
)
from app.services.social_listening_agent import get_social_listening_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/social-listening-agent", tags=["social-listening-agent"])


def _config_read(agent: SocialListeningAgent) -> AgentConfigRead:
    """Serialize the agent config with the API key masked to a boolean."""
    return AgentConfigRead(
        id=agent.id,
        is_active=agent.is_active,
        llm_model=agent.llm_model,
        has_api_key=agent.has_own_key,
        monitored_keywords=agent.monitored_keywords or [],
        monitored_industries=agent.monitored_industries or [],
        news_sources=agent.news_sources or [],
        min_relevance_score=agent.min_relevance_score,
        auto_create_leads=agent.auto_create_leads,
        auto_enroll_dialtone=agent.auto_enroll_dialtone,
        scan_frequency_hours=agent.scan_frequency_hours,
        last_scan_at=agent.last_scan_at,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        available_models=list(ALLOWED_LLM_MODELS),
    )


# --- Config ------------------------------------------------------------------


@router.get("/config", response_model=AgentConfigRead)
async def get_config(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Return the caller's org agent config (creating a default row if absent)."""
    agent = await get_social_listening_service().get_or_create_agent(db)
    return _config_read(agent)


@router.put("/config", response_model=AgentConfigRead)
async def update_config(
    payload: AgentConfigUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update agent config (org_admin only)."""
    scope.check_permission("org_admin")
    service = get_social_listening_service()
    agent = await service.get_or_create_agent(db)

    data = payload.model_dump(exclude_unset=True)

    if "llm_model" in data:
        model = data["llm_model"]
        if model not in ALLOWED_LLM_MODELS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unsupported llm_model. Choose one of: {', '.join(ALLOWED_LLM_MODELS)}",
            )
        agent.llm_model = model

    # Write-only key: present-and-non-None means "set or clear"; absent means
    # "leave the stored key alone".
    if "llm_api_key" in data:
        service.set_api_key(agent, data["llm_api_key"])

    for field in (
        "is_active",
        "monitored_keywords",
        "monitored_industries",
        "news_sources",
        "min_relevance_score",
        "auto_create_leads",
        "auto_enroll_dialtone",
        "scan_frequency_hours",
    ):
        if field in data:
            setattr(agent, field, data[field])

    await db.flush()
    # The UPDATE fires ``updated_at``'s server-side ``onupdate``, expiring the
    # attribute; refresh in-context so serializing it doesn't trigger a lazy
    # (sync) IO load outside the async greenlet.
    await db.refresh(agent)
    return _config_read(agent)


# --- Signals -----------------------------------------------------------------


@router.get("/signals", response_model=list[SignalRead])
async def list_signals(
    signal_type: str | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    min_relevance: int = Query(0, ge=0, le=100),
    source_type: str | None = Query(None),
    since: datetime | None = Query(None, description="Only signals created on/after this time"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """List discovered signals (org-scoped), newest first, with filters."""
    query = select(SignalEvent).where(SignalEvent.relevance_score >= min_relevance)
    if signal_type:
        query = query.where(SignalEvent.signal_type == signal_type)
    if status_filter:
        if status_filter not in SIGNAL_STATUSES:
            raise HTTPException(status_code=422, detail=f"Invalid status: {status_filter}")
        query = query.where(SignalEvent.status == status_filter)
    if source_type:
        query = query.where(SignalEvent.source_type == source_type)
    if since:
        query = query.where(SignalEvent.created_at >= since)
    query = query.order_by(SignalEvent.created_at.desc()).offset(offset).limit(limit)
    rows = (await db.execute(query)).scalars().all()
    return [SignalRead.model_validate(r) for r in rows]


async def _signal_or_404(db: AsyncSession, signal_id: UUID) -> SignalEvent:
    signal = (
        await db.execute(select(SignalEvent).where(SignalEvent.id == signal_id))
    ).scalars().first()
    if signal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Signal not found")
    return signal


@router.post("/signals/{signal_id}/action", response_model=SignalActionResponse)
async def action_signal(
    signal_id: UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Convert a signal to a warm lead (and auto-enroll if the agent is set to)."""
    service = get_social_listening_service()
    agent = await service.get_or_create_agent(db)
    signal = await _signal_or_404(db, signal_id)

    contact = await service.create_warm_lead(db, signal)
    if contact is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not resolve a company for this signal — no lead created.",
        )
    enrolled = False
    if agent.auto_enroll_dialtone:
        enrolled = await service.auto_enroll_lead(db, contact, signal)
    await db.flush()
    await db.refresh(signal)
    return SignalActionResponse(
        signal=SignalRead.model_validate(signal),
        lead_created=True,
        enrolled=enrolled,
        contact_id=contact.id,
        detail="Warm lead created" + (" and enrolled in a sequence" if enrolled else ""),
    )


@router.post("/signals/{signal_id}/dismiss", response_model=SignalRead)
async def dismiss_signal(
    signal_id: UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a signal (mark it as not worth actioning)."""
    signal = await _signal_or_404(db, signal_id)
    signal.status = "dismissed"
    await db.flush()
    await db.refresh(signal)
    return SignalRead.model_validate(signal)


# --- Monitored companies -----------------------------------------------------


@router.get("/monitored-companies", response_model=list[MonitoredCompanyRead])
async def list_monitored_companies(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """List the companies this org is actively monitoring."""
    rows = (
        await db.execute(
            select(MonitoredCompany).order_by(MonitoredCompany.created_at.desc())
        )
    ).scalars().all()
    return [MonitoredCompanyRead.model_validate(r) for r in rows]


@router.post(
    "/monitored-companies",
    response_model=MonitoredCompanyRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_monitored_company(
    payload: MonitoredCompanyCreate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Start monitoring a company (org_admin only)."""
    scope.check_permission("org_admin")
    mc = MonitoredCompany(
        company_name=payload.company_name.strip(),
        domain=(payload.domain or None),
        company_id=payload.company_id,
        monitor_news=payload.monitor_news,
        monitor_jobs=payload.monitor_jobs,
    )
    db.add(mc)
    await db.flush()
    await db.refresh(mc)
    return MonitoredCompanyRead.model_validate(mc)


@router.delete("/monitored-companies/{monitored_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_monitored_company(
    monitored_id: UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Stop monitoring a company (org_admin only)."""
    scope.check_permission("org_admin")
    mc = (
        await db.execute(select(MonitoredCompany).where(MonitoredCompany.id == monitored_id))
    ).scalars().first()
    if mc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not monitored")
    await db.delete(mc)
    await db.flush()
    return None


# --- Stats + manual scan -----------------------------------------------------


@router.get("/stats", response_model=StatsRead)
async def get_stats(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate stats: signals found, leads created, conversion, top types."""
    return StatsRead(**await get_social_listening_service().stats(db))


@router.post("/scan")
async def trigger_scan(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Run a scan now for the caller's org (org_admin only).

    Synchronous, so the UI gets an immediate result. The agent must be active.
    """
    scope.check_permission("org_admin")
    result = await get_social_listening_service().run_scan(db)
    return result
