"""Celery task for predictive client health (feature 3.2).

Weekly pass over all clients (companies in the ``client`` pipeline stage): for
each, compute current health plus a churn probability and expansion assessment,
and persist a ``ClientHealthSnapshot`` carrying all three so trends are tracked
over time. High churn risk raises a notification to the account owner.
"""

import asyncio
import logging

from sqlalchemy import select

from app.models.client_health import ClientHealthSnapshot
from app.models.company import Company
from app.services.churn_predictor import get_churn_predictor
from app.services.expansion_detector import get_expansion_detector
from app.services.health import compute_company_health
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

_CLIENT_STAGE = "client"
_HIGH_CHURN_THRESHOLD = 0.6


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def assess_and_record(db, company: Company, use_ai: bool = False) -> ClientHealthSnapshot:
    """Compute health + churn + expansion for one client and persist a snapshot."""
    health = await compute_company_health(db, company)
    churn = await get_churn_predictor().predict(db, company, use_ai=use_ai)
    expansion = await get_expansion_detector().detect(db, company, use_ai=use_ai)

    snapshot = ClientHealthSnapshot(
        company_id=company.id,
        score=health.score,
        health_status=health.health_status,
        last_contact_date=health.signals.last_contact_at,
        response_rate=health.signals.response_rate,
        meeting_frequency=health.signals.meetings_last_90d,
        predicted_churn_probability=churn["probability"],
        churn_risk_factors={
            "risk_level": churn["risk_level"],
            "factors": churn["risk_factors"],
            "summary": churn["summary"],
        },
        expansion_signals={
            "expansion_score": expansion["expansion_score"],
            "signals": expansion["signals"],
            "recommended_play": expansion["recommended_play"],
            "ready": expansion["ready"],
        },
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


@celery_app.task(name="app.tasks.client_health.predict_client_health")
def predict_client_health(limit: int = 500, use_ai: bool = True):
    """Weekly predictive-health pass across all clients."""
    logger.info("Starting predictive client-health pass")

    async def _run():
        # Per-org: clients (companies in the ``client`` stage) are org-scoped.
        async def _body(db):
            clients = list(
                (
                    await db.execute(
                        select(Company).where(Company.pipeline_stage == _CLIENT_STAGE).limit(limit)
                    )
                ).scalars().all()
            )
            high_risk = 0
            for company in clients:
                try:
                    snap = await assess_and_record(db, company, use_ai=use_ai)
                    if (snap.predicted_churn_probability or 0) >= _HIGH_CHURN_THRESHOLD:
                        high_risk += 1
                        _alert_high_churn(company, snap)
                except Exception:
                    logger.exception("Client-health assessment failed for %s", company.id)
            await db.commit()
            return {"clients_assessed": len(clients), "high_churn_risk": high_risk}

        return await for_each_org(_body, label="predict_client_health")

    return _run_async(_run())


def _alert_high_churn(company: Company, snapshot: ClientHealthSnapshot) -> None:
    """Best-effort high-churn alert. Logged as a warning the account owner /
    dashboard surfaces; the churn-risk endpoint is the primary read surface."""
    prob = round((snapshot.predicted_churn_probability or 0) * 100)
    summary = (snapshot.churn_risk_factors or {}).get("summary", "")
    logger.warning("HIGH CHURN RISK: %s (%s%%) — %s", company.name, prob, summary)
