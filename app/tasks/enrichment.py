"""Celery tasks for data enrichment via Apollo.io."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.company import Company
from app.services.enrichment import ApolloEnrichmentService
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
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
        async with celery_session() as db:
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


@celery_app.task(name="app.tasks.enrichment.auto_enrich_companies")
def auto_enrich_companies():
    """Periodic task: find companies missing industry or employee data and enrich via Apollo."""
    logger.info("Auto-enriching companies missing data")

    async def _run():
        service = ApolloEnrichmentService()
        async with celery_session() as db:
            # Find companies missing key data that have a domain (needed for Apollo)
            result = await db.execute(
                select(Company)
                .where(
                    Company.domain.isnot(None),
                    Company.domain != "",
                    or_(
                        Company.industry_segment.is_(None),
                        Company.industry_segment == "",
                        Company.employee_count.is_(None),
                    ),
                )
                .limit(10)  # Process in small batches to avoid API rate limits
            )
            companies = list(result.scalars().all())

            if not companies:
                logger.info("No companies need enrichment")
                return {"enriched": 0}

            enriched = 0
            for company in companies:
                try:
                    await service.enrich_company(db, company)
                    enriched += 1
                except Exception as e:
                    logger.warning("Auto-enrich failed for %s: %s", company.name, str(e))
                    continue

            await db.commit()
            logger.info("Auto-enriched %d / %d companies", enriched, len(companies))
            return {"enriched": enriched, "total_candidates": len(companies)}

    return _run_async(_run())


@celery_app.task(name="app.tasks.enrichment.auto_score_leads")
def auto_score_leads():
    """Periodic task: find companies with no AI score and score them."""
    logger.info("Auto-scoring unscored leads")

    async def _run():
        from app.services.ai_service import get_ai_service
        from app.models.lead_score import LeadScore

        ai = get_ai_service()
        async with celery_session() as db:
            # Get IDs of companies that already have AI scores
            scored_result = await db.execute(
                select(LeadScore.company_id).where(
                    LeadScore.score_type == "ai"
                ).distinct()
            )
            scored_ids = set()
            for row in scored_result.all():
                scored_ids.add(row[0])

            # Get unscored companies
            if scored_ids:
                result = await db.execute(
                    select(Company).where(
                        Company.id.notin_(scored_ids)
                    ).limit(10)
                )
            else:
                result = await db.execute(
                    select(Company).limit(10)
                )
            companies = list(result.scalars().all())

            if not companies:
                logger.info("No companies need scoring")
                return {"scored": 0}

            scored = 0
            for company in companies:
                try:
                    score_result = await ai.score_lead(
                        company_name=company.name,
                        industry=company.industry_segment or "Unknown",
                        employee_count=company.employee_count,
                        location=f"{company.city or ''}, {company.state_province or ''}, {company.country or ''}".strip(", "),
                    )

                    if "error" not in score_result:
                        # Map AI response to LeadScore model fields
                        raw_score = score_result.get("score", 50)
                        lead_score = LeadScore(
                            company_id=company.id,
                            score_type="ai",
                            fit_score=raw_score,
                            factors={
                                "grade": score_result.get("grade", "C"),
                                "reasoning": score_result.get("reasoning", ""),
                                "recommended_action": score_result.get("recommended_action", ""),
                                "priority": score_result.get("priority", "medium"),
                            },
                        )
                        db.add(lead_score)

                        # Update company stage if still 'new'
                        if company.stage == "new":
                            company.stage = "scored"

                        scored += 1
                except Exception as e:
                    logger.warning("Auto-score failed for %s: %s", company.name, str(e))
                    continue

            await db.commit()
            logger.info("Auto-scored %d / %d companies", scored, len(companies))
            return {"scored": scored, "total_candidates": len(companies)}

    return _run_async(_run())
