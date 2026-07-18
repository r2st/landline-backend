"""Client Success Agent API — config, action queue, stats, health overview.

Mounted at ``/api/v1/client-success-agent``. Everything is org-scoped: the
global read filter (see :mod:`app.models.mixins`) confines every query to the
caller's organization, and the config row is created lazily on first read.

The API key is write-only from the client's perspective: ``GET /config`` returns
``llm_api_key_set`` + a masked hint but NEVER the key itself; ``PUT /config``
accepts a raw key (encrypted here before persisting), treats ``""`` as "clear
it, fall back to the org default", and ``null`` as "leave it unchanged".
Config writes require the ``org_admin`` role.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.client_health import ClientHealthSnapshot
from app.models.client_success_agent import (
    ACTION_STATUSES,
    ACTION_TYPES,
    ClientSuccessAction,
    ClientSuccessAgent,
)
from app.models.company import Company
from app.models.contact import Contact
from app.models.user import UserRole
from app.schemas.client_success_agent import (
    ActionList,
    ActionRead,
    AgentConfigRead,
    AgentConfigUpdate,
    AgentStats,
    ClientHealthOverview,
    ClientHealthRow,
)
from app.services.client_success_agent import (
    CLIENT_STAGE,
    encrypt_api_key,
    get_client_success_agent_service,
)
from app.services.health import compute_company_health

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/client-success-agent", tags=["client-success-agent"])

_service = get_client_success_agent_service()


def _config_to_read(agent: ClientSuccessAgent) -> AgentConfigRead:
    """Serialize a config row, masking the API key (never returned in full)."""
    hint = None
    if agent.llm_api_key:
        # Purely a "something is stored" affordance — decrypting just to show a
        # few chars would leak more than it helps, so the hint is static.
        hint = "•••• (organization key overridden)"
    return AgentConfigRead(
        id=agent.id,
        is_active=agent.is_active,
        llm_model=agent.llm_model,
        llm_api_key_set=bool(agent.llm_api_key),
        llm_api_key_hint=hint,
        health_warning_threshold=agent.health_warning_threshold,
        health_critical_threshold=agent.health_critical_threshold,
        check_frequency_days=agent.check_frequency_days,
        auto_checkin_enabled=agent.auto_checkin_enabled,
        auto_escalation_enabled=agent.auto_escalation_enabled,
        celebration_enabled=agent.celebration_enabled,
        checkin_template=agent.checkin_template,
        escalation_template=agent.escalation_template,
        celebration_template=agent.celebration_template,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


# --- Config ------------------------------------------------------------------


@router.get("/config", response_model=AgentConfigRead)
async def get_config(db: AsyncSession = Depends(get_db)):
    """Return the org's agent config (creating a default row on first access)."""
    agent = await _service.get_or_create_config(db)
    return _config_to_read(agent)


@router.put("/config", response_model=AgentConfigRead)
async def update_config(
    body: AgentConfigUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update the agent config, including the on/off switch, model, and key.

    Requires ``org_admin`` (or higher). Threshold sanity: critical must be
    strictly below warning, so a misconfigured pair can't make every client both
    'warning' and 'critical'.
    """
    scope.check_permission(UserRole.ORG_ADMIN.value)
    agent = await _service.get_or_create_config(db)

    data = body.model_dump(exclude_unset=True)

    # Resolve the effective thresholds after this update and validate ordering.
    warning = data.get("health_warning_threshold", agent.health_warning_threshold)
    critical = data.get("health_critical_threshold", agent.health_critical_threshold)
    if critical >= warning:
        raise HTTPException(
            status_code=400,
            detail="health_critical_threshold must be below health_warning_threshold",
        )

    # API key: unset → leave; "" → clear (fall back to org key); else encrypt.
    if "llm_api_key" in data:
        raw = data.pop("llm_api_key")
        if raw is None or raw == "":
            agent.llm_api_key = None
        else:
            agent.llm_api_key = encrypt_api_key(raw.strip())

    for field, value in data.items():
        setattr(agent, field, value)

    await db.flush()
    await db.refresh(agent)
    logger.info("Client Success Agent config updated by %s", scope.user.email)
    return _config_to_read(agent)


# --- Actions -----------------------------------------------------------------


async def _enrich_actions(
    db: AsyncSession, actions: list[ClientSuccessAction]
) -> list[ActionRead]:
    """Attach company/contact display fields to a batch of action rows."""
    company_ids = {a.company_id for a in actions if a.company_id}
    contact_ids = {a.contact_id for a in actions if a.contact_id}

    companies = {}
    if company_ids:
        rows = (
            await db.execute(select(Company).where(Company.id.in_(company_ids)))
        ).scalars().all()
        companies = {c.id: c for c in rows}
    contacts = {}
    if contact_ids:
        rows = (
            await db.execute(select(Contact).where(Contact.id.in_(contact_ids)))
        ).scalars().all()
        contacts = {c.id: c for c in rows}

    out = []
    for a in actions:
        read = ActionRead.model_validate(a)
        company = companies.get(a.company_id)
        contact = contacts.get(a.contact_id)
        if company:
            read.company_name = company.name
        if contact:
            name = " ".join(
                p for p in (contact.first_name, contact.last_name) if p
            ).strip()
            read.contact_name = name or None
            read.contact_email = contact.email
        out.append(read)
    return out


@router.get("/actions", response_model=ActionList)
async def list_actions(
    db: AsyncSession = Depends(get_db),
    action_type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    contact_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """List actions, newest-first, filterable by type / status / contact."""
    if action_type and action_type not in ACTION_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown action_type '{action_type}'")
    if status and status not in ACTION_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'")

    stmt = select(ClientSuccessAction)
    count_stmt = select(func.count(ClientSuccessAction.id))
    if action_type:
        stmt = stmt.where(ClientSuccessAction.action_type == action_type)
        count_stmt = count_stmt.where(ClientSuccessAction.action_type == action_type)
    if status:
        stmt = stmt.where(ClientSuccessAction.status == status)
        count_stmt = count_stmt.where(ClientSuccessAction.status == status)
    if contact_id:
        stmt = stmt.where(ClientSuccessAction.contact_id == contact_id)
        count_stmt = count_stmt.where(ClientSuccessAction.contact_id == contact_id)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = list(
        (
            await db.execute(
                stmt.order_by(ClientSuccessAction.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
    )
    return ActionList(actions=await _enrich_actions(db, rows), total=total)


async def _load_action(db: AsyncSession, action_id: uuid.UUID) -> ClientSuccessAction:
    action = (
        await db.execute(
            select(ClientSuccessAction).where(ClientSuccessAction.id == action_id)
        )
    ).scalar_one_or_none()
    if action is None:
        raise HTTPException(status_code=404, detail="Action not found")
    return action


@router.post("/actions/{action_id}/approve", response_model=ActionRead)
async def approve_action(action_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Approve a pending action — marks it sent/completed and stamps execution."""
    from datetime import datetime, timezone

    action = await _load_action(db, action_id)
    if action.status not in ("pending",):
        raise HTTPException(
            status_code=400, detail=f"Cannot approve an action in status '{action.status}'"
        )
    # review_call is a suggestion a human fulfils → 'completed'; the messaging
    # plays are 'sent' once approved.
    action.status = "completed" if action.action_type == "review_call" else "sent"
    action.executed_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(action)
    return (await _enrich_actions(db, [action]))[0]


@router.post("/actions/{action_id}/dismiss", response_model=ActionRead)
async def dismiss_action(action_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Dismiss an action so it drops out of the pending queue."""
    from datetime import datetime, timezone

    action = await _load_action(db, action_id)
    action.status = "dismissed"
    action.dismissed_at = datetime.now(timezone.utc)
    await db.flush()
    await db.refresh(action)
    return (await _enrich_actions(db, [action]))[0]


# --- Stats -------------------------------------------------------------------


@router.get("/stats", response_model=AgentStats)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Roll-up for the dashboard stat cards."""
    clients_monitored = (
        await db.execute(
            select(func.count(Company.id)).where(Company.pipeline_stage == CLIENT_STAGE)
        )
    ).scalar_one()

    # One grouped pass over the action table: counts per (type, status-ish).
    def _count(**filters):
        stmt = select(func.count(ClientSuccessAction.id))
        for col, val in filters.items():
            stmt = stmt.where(getattr(ClientSuccessAction, col) == val)
        return stmt

    checkins_sent = (
        await db.execute(_count(action_type="checkin", status="sent"))
    ).scalar_one()
    escalations = (
        await db.execute(_count(action_type="escalation"))
    ).scalar_one()
    celebrations = (
        await db.execute(_count(action_type="celebration"))
    ).scalar_one()
    review_calls = (
        await db.execute(_count(action_type="review_call"))
    ).scalar_one()
    pending_actions = (
        await db.execute(_count(status="pending"))
    ).scalar_one()

    return AgentStats(
        clients_monitored=clients_monitored,
        checkins_sent=checkins_sent,
        escalations=escalations,
        celebrations=celebrations,
        pending_actions=pending_actions,
        review_calls=review_calls,
    )


# --- Health overview ---------------------------------------------------------


@router.get("/health-overview", response_model=ClientHealthOverview)
async def health_overview(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(100, ge=1, le=500),
):
    """Clients sorted by current health (lowest first) with a trend arrow.

    Trend compares the freshly-computed score to the client's most recent stored
    snapshot; with no prior snapshot the trend is 'flat'.
    """
    clients = list(
        (
            await db.execute(
                select(Company)
                .where(Company.pipeline_stage == CLIENT_STAGE)
                .limit(limit)
            )
        ).scalars().all()
    )

    rows: list[ClientHealthRow] = []
    for company in clients:
        health = await compute_company_health(db, company)
        prev = (
            await db.execute(
                select(ClientHealthSnapshot.score)
                .where(ClientHealthSnapshot.company_id == company.id)
                .order_by(ClientHealthSnapshot.recorded_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if prev is None or prev == health.score:
            trend = "flat"
        elif health.score > prev:
            trend = "up"
        else:
            trend = "down"

        rows.append(
            ClientHealthRow(
                company_id=company.id,
                company_name=company.name,
                score=health.score,
                health_status=health.health_status,
                previous_score=prev,
                trend=trend,
                last_contact_date=health.signals.last_contact_at,
            )
        )

    rows.sort(key=lambda r: r.score)
    return ClientHealthOverview(clients=rows)
