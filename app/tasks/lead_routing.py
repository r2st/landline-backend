"""Celery tasks for real-time lead prioritization & routing (Feature 1.3).

* ``update_lead_score_on_signal`` — event-driven single-company rescore, fired
  from the tracking endpoints the instant a contact opens/clicks an email so a
  hot lead surfaces immediately instead of waiting for the batch cycle.
* ``refresh_propensity_scores`` — beat task that re-routes active-pipeline
  companies so propensity/velocity stay current.
* ``recompute_rep_profiles`` — beat task refreshing rep win-rates & capacity.

``enqueue_signal_rescore`` is the best-effort producer used from the (sync)
request path; it never raises into the caller, so a missing broker in tests or
local dev simply skips the async rescore.
"""

import asyncio
import logging
import uuid
from typing import Optional

from sqlalchemy import select

from app.database_celery import celery_session
from app.models.company import Company
from app.models.contact import Contact
from app.models.rep_profile import RepProfile
from app.services import lead_router
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def enqueue_signal_rescore(*, contact_id: Optional[uuid.UUID] = None, company_id: Optional[uuid.UUID] = None) -> None:
    """Best-effort: enqueue a propensity rescore for a contact/company's lead.

    Swallows all errors (e.g. no broker configured) so callers on the web
    request path are never blocked or broken by the async hook.
    """
    try:
        update_lead_score_on_signal.delay(
            contact_id=str(contact_id) if contact_id else None,
            company_id=str(company_id) if company_id else None,
        )
    except Exception as exc:  # pragma: no cover - broker-less envs
        logger.debug("Skipped signal rescore enqueue: %s", exc)


async def _rescore_company(db, company_id: uuid.UUID) -> dict:
    company = (await db.execute(select(Company).where(Company.id == company_id))).scalar_one_or_none()
    if company is None:
        return {"status": "not_found", "company_id": str(company_id)}
    ls = await lead_router.score_and_route(db, company)
    return {
        "status": "ok",
        "company_id": str(company_id),
        "fit_score": ls.fit_score,
        "propensity_score": ls.propensity_score,
    }


@celery_app.task(name="app.tasks.lead_routing.update_lead_score_on_signal")
def update_lead_score_on_signal(contact_id: Optional[str] = None, company_id: Optional[str] = None):
    """Event-driven rescore for a single lead triggered by an engagement signal."""
    async def _run():
        async with celery_session() as db:
            try:
                cid: Optional[uuid.UUID] = uuid.UUID(company_id) if company_id else None
                if cid is None and contact_id:
                    contact = (
                        await db.execute(select(Contact).where(Contact.id == uuid.UUID(contact_id)))
                    ).scalar_one_or_none()
                    cid = contact.company_id if contact else None
                if cid is None:
                    return {"status": "no_company"}
                result = await _rescore_company(db, cid)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("update_lead_score_on_signal failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.lead_routing.refresh_propensity_scores")
def refresh_propensity_scores(limit: int = 200):
    """Beat task: re-route active-pipeline companies to refresh propensity/velocity."""
    async def _run():
        async with celery_session() as db:
            try:
                rows = await db.execute(
                    select(Company)
                    .where(Company.pipeline_stage.in_(("researching", "outreach", "engaged", "negotiating")))
                    .order_by(Company.updated_at.asc())
                    .limit(limit)
                )
                companies = list(rows.scalars().all())
                scored = 0
                for company in companies:
                    try:
                        await lead_router.score_and_route(db, company)
                        scored += 1
                    except Exception as e:
                        logger.warning("Re-route failed for %s: %s", company.name, e)
                await db.commit()
                return {"scored": scored, "candidates": len(companies)}
            except Exception:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("refresh_propensity_scores failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.lead_routing.recompute_rep_profiles")
def recompute_rep_profiles():
    """Beat task: refresh every rep profile's win rates, velocity, and load."""
    async def _run():
        async with celery_session() as db:
            try:
                profiles = list((await db.execute(select(RepProfile))).scalars().all())
                for p in profiles:
                    await lead_router.recompute_rep_profile(db, p)
                await db.commit()
                return {"recomputed": len(profiles)}
            except Exception:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("recompute_rep_profiles failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
