"""Client-management overview rollup — GET /clients/overview."""

import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.deal import Deal
from app.models.task import Task
from app.schemas.clients import AtRiskClient, ClientOverview
from app.services.churn_predictor import get_churn_predictor
from app.services.expansion_detector import get_expansion_detector
from app.services.health import compute_company_health

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/clients", tags=["clients"])

# A "client" is a company that has converted (kanban pipeline_stage = client).
_CLIENT_STAGE = "client"
_OPEN_TASK_STATUSES = ("pending", "in_progress")


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


@router.get("")
async def list_clients(
    max_clients: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """List converted clients (companies in the ``client`` pipeline stage),
    enriched with health score, account value, last-contact, open-task count
    and industry — the columns the Clients table renders.

    ``health_status`` is intentionally omitted; the frontend derives its RAG
    badge from ``health_score`` so the two stay in sync.
    """
    clients = list(
        (
            await db.execute(
                select(Company)
                .where(Company.pipeline_stage == _CLIENT_STAGE)
                .order_by(Company.name.asc())
                .limit(max_clients)
            )
        ).scalars().all()
    )
    if not clients:
        return []

    client_ids = [c.id for c in clients]

    # Won-deal value per client (account value).
    deal_rows = (
        await db.execute(
            select(Deal.company_id, func.coalesce(func.sum(Deal.value), 0))
            .where(Deal.status == "won", Deal.company_id.in_(client_ids))
            .group_by(Deal.company_id)
        )
    ).all()
    deal_value = {cid: float(total or 0) for cid, total in deal_rows}

    # Open-task count per client.
    task_rows = (
        await db.execute(
            select(Task.company_id, func.count(Task.id))
            .where(Task.status.in_(_OPEN_TASK_STATUSES), Task.company_id.in_(client_ids))
            .group_by(Task.company_id)
        )
    ).all()
    open_tasks = {cid: int(count) for cid, count in task_rows}

    out = []
    for company in clients:
        health = await compute_company_health(db, company)
        out.append({
            "id": str(company.id),
            "company_id": str(company.id),
            "name": company.name,
            "company_name": company.name,
            "health_score": health.score,
            "deal_value": deal_value.get(company.id, 0.0),
            "last_contact_at": health.signals.last_contact_at,
            "upcoming_tasks": open_tasks.get(company.id, 0),
            "industry": company.industry_segment,
        })
    return out


@router.get("/churn-risk")
async def churn_risk(
    min_probability: float = Query(0.0, ge=0.0, le=1.0),
    max_clients: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Predictive churn risk across clients, highest risk first (feature 3.2).

    Computed live from health trajectory + engagement signals. Filter with
    ``min_probability`` to see only clients above a risk floor.
    """
    clients = list(
        (
            await db.execute(
                select(Company)
                .where(Company.pipeline_stage == _CLIENT_STAGE)
                .order_by(Company.name.asc())
                .limit(max_clients)
            )
        ).scalars().all()
    )
    predictor = get_churn_predictor()
    out = []
    for company in clients:
        churn = await predictor.predict(db, company, use_ai=False)
        if churn["probability"] < min_probability:
            continue
        out.append({
            "company_id": str(company.id),
            "company_name": company.name,
            "predicted_churn_probability": churn["probability"],
            "risk_level": churn["risk_level"],
            "health_score": churn["health_score"],
            "health_status": churn["health_status"],
            "risk_factors": churn["risk_factors"],
            "summary": churn["summary"],
        })
    out.sort(key=lambda c: c["predicted_churn_probability"], reverse=True)
    return out


@router.get("/expansion-opportunities")
async def expansion_opportunities(
    min_score: int = Query(50, ge=0, le=100),
    max_clients: int = Query(500, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
):
    """Clients showing upsell / cross-sell / referral potential (feature 3.2),
    highest expansion score first."""
    clients = list(
        (
            await db.execute(
                select(Company)
                .where(Company.pipeline_stage == _CLIENT_STAGE)
                .order_by(Company.name.asc())
                .limit(max_clients)
            )
        ).scalars().all()
    )
    detector = get_expansion_detector()
    out = []
    for company in clients:
        exp = await detector.detect(db, company, use_ai=False)
        if exp["expansion_score"] < min_score:
            continue
        out.append({
            "company_id": str(company.id),
            "company_name": company.name,
            "expansion_score": exp["expansion_score"],
            "ready": exp["ready"],
            "signals": exp["signals"],
            "recommended_play": exp["recommended_play"],
            "health_status": exp["health_status"],
        })
    out.sort(key=lambda c: c["expansion_score"], reverse=True)
    return out


@router.get("/overview", response_model=ClientOverview)
async def clients_overview(
    upcoming_days: int = Query(7, ge=1, le=365),
    max_clients: int = Query(500, ge=1, le=2000, description="Cap on clients health-scored"),
    db: AsyncSession = Depends(get_db),
):
    """Client-management dashboard rollup: client count, won revenue, health
    distribution, the at-risk list, and upcoming tasks."""
    now = datetime.now(timezone.utc)

    # --- Clients (converted companies) ---
    clients = list(
        (
            await db.execute(
                select(Company)
                .where(Company.pipeline_stage == _CLIENT_STAGE)
                .order_by(Company.name.asc())
                .limit(max_clients)
            )
        ).scalars().all()
    )

    # --- Won-deal revenue (all deals) ---
    won_row = (
        await db.execute(
            select(func.count(Deal.id), func.coalesce(func.sum(Deal.value), 0)).where(
                Deal.status == "won"
            )
        )
    ).one()
    won_deal_count = won_row[0] or 0
    total_won_revenue = float(won_row[1] or 0)

    # --- Health per client ---
    distribution = {"healthy": 0, "at_risk": 0, "churned": 0}
    at_risk: list[AtRiskClient] = []
    score_sum = 0
    for company in clients:
        health = await compute_company_health(db, company)
        distribution[health.health_status] = distribution.get(health.health_status, 0) + 1
        score_sum += health.score
        if health.health_status == "at_risk":
            at_risk.append(AtRiskClient(
                company_id=company.id,
                company_name=company.name,
                score=health.score,
                days_since_last_contact=health.signals.days_since_last_contact,
            ))

    # Most-stale at-risk clients first (None = never contacted -> most urgent).
    at_risk.sort(
        key=lambda c: c.days_since_last_contact if c.days_since_last_contact is not None else 10**9,
        reverse=True,
    )

    # --- Upcoming tasks (open, due within window incl. overdue) ---
    open_tasks = list(
        (
            await db.execute(
                select(Task).where(
                    Task.status.in_(_OPEN_TASK_STATUSES), Task.due_date.isnot(None)
                )
            )
        ).scalars().all()
    )
    horizon = now + timedelta(days=upcoming_days)
    upcoming = [t for t in open_tasks if _aware(t.due_date) is not None and _aware(t.due_date) <= horizon]
    upcoming.sort(key=lambda t: _aware(t.due_date))

    return ClientOverview(
        client_count=len(clients),
        total_won_revenue=total_won_revenue,
        won_deal_count=won_deal_count,
        health_distribution=distribution,
        avg_health_score=round(score_sum / len(clients), 1) if clients else None,
        at_risk_clients=at_risk,
        upcoming_tasks=upcoming[:25],
        upcoming_task_count=len(upcoming),
    )
