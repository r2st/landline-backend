"""Celery tasks for data enrichment via Apollo.io."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.company import Company
from app.services.enrichment import ApolloEnrichmentService
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.enrichment.enrich_company", bind=True, max_retries=3)
def enrich_company(self, company_id: str):
    """Enrich a single company via Apollo.io API.

    Args:
        company_id: UUID string of the company to enrich
    """
    logger.info("Enriching company %s", company_id)

    async def _run():
        service = ApolloEnrichmentService()
        async with async_session_factory() as db:
            try:
                result = await db.execute(
                    select(Company).where(Company.id == UUID(company_id))
                )
                company = result.scalar_one_or_none()
                if not company:
                    return {"status": "not_found", "company_id": company_id}

                enrich_result = await service.enrich_company(db, company)
                contacts = await service.find_contacts(db, company)
                await db.commit()

                return {
                    "company_id": company_id,
                    "enrichment": enrich_result,
                    "contacts_found": len(contacts),
                }
            except Exception as e:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("Enrichment failed for %s: %s", company_id, str(exc))
        raise self.retry(exc=exc, countdown=30 * (self.request.retries + 1))


@celery_app.task(name="app.tasks.enrichment.enrich_batch", bind=True)
def enrich_batch(self, company_ids: list[str]):
    """Enrich multiple companies. Dispatches individual tasks.

    Args:
        company_ids: List of UUID strings
    """
    logger.info("Dispatching enrichment for %d companies", len(company_ids))
    results = []
    for cid in company_ids:
        task = enrich_company.delay(cid)
        results.append({"company_id": cid, "task_id": task.id})
    return {"dispatched": len(results), "tasks": results}
