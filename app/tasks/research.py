"""Celery tasks for the AI Company Research Agent (feature 1.4)."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select

from app.database_celery import celery_session
from app.models.company import Company
from app.models.company_research import CompanyResearch
from app.services.research_agent import get_research_agent
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org, org_scope

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.research.research_company", bind=True, max_retries=2)
def research_company(self, company_id: str, force: bool = False):
    """Research a single company and store an AI research brief.

    Triggered on company creation and before first outreach enrollment. Idempotent
    unless ``force`` — a fresh non-stale brief short-circuits the expensive
    scrape + synthesis.
    """
    logger.info("Researching company %s (force=%s)", company_id, force)

    async def _run():
        agent = get_research_agent()
        async with celery_session() as db:
            try:
                # Per-record: bind the company's org (context is lost over the
                # Celery wire) so the research brief lands in the owning tenant.
                company = (
                    await db.execute(
                        select(Company)
                        .where(Company.id == UUID(company_id))
                        .execution_options(skip_org_filter=True)
                    )
                ).scalar_one_or_none()
                if not company:
                    return {"status": "not_found", "company_id": company_id}

                async with org_scope(company.org_id):
                    research = await agent.research_company(db, company, force=force)
                    if research is None:
                        return {"status": "failed", "company_id": company_id}
                    return {
                        "status": "researched",
                        "company_id": company_id,
                        "pain_points": len(research.pain_points or []),
                        "angles": len(research.outreach_angles or []),
                        "sources": research.sources_used,
                    }
            except Exception:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("research_company failed for %s: %s", company_id, str(exc))
        raise self.retry(exc=exc, countdown=120 * (self.request.retries + 1))


@celery_app.task(name="app.tasks.research.research_stale_companies")
def research_stale_companies(limit: int = 20):
    """Periodic backfill: research active-pipeline companies with no fresh brief.

    Finds companies in an outreach-relevant stage that have no non-stale
    ``CompanyResearch`` and dispatches ``research_company`` for each, so briefs
    exist before a rep starts outreach.
    """
    logger.info("Scanning for companies needing research (limit=%d)", limit)

    async def _run():
        # Per-org: companies and research briefs are org-scoped.
        async def _body(db):
            # Companies with a non-stale research brief already.
            researched = (
                await db.execute(
                    select(CompanyResearch.company_id).where(
                        CompanyResearch.is_stale.is_(False)
                    )
                )
            ).scalars().all()
            researched_ids = set(researched)

            active_stages = ["new_lead", "researching", "outreach", "engaged", "negotiating"]
            companies = (
                await db.execute(
                    select(Company)
                    .where(Company.pipeline_stage.in_(active_stages))
                    .order_by(Company.created_at.desc())
                    .limit(200)
                )
            ).scalars().all()

            dispatched = 0
            for company in companies:
                if company.id in researched_ids:
                    continue
                try:
                    research_company.delay(str(company.id))
                    dispatched += 1
                except Exception as exc:  # pragma: no cover - broker-dependent
                    logger.warning("Could not dispatch research for %s: %s", company.id, exc)
                if dispatched >= limit:
                    break
            return {"scanned": len(companies), "dispatched": dispatched}

        return await for_each_org(_body, label="research_stale_companies")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("research_stale_companies failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
