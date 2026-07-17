"""Celery tasks for web scraping."""

import asyncio
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.scrapers.thomasnet import ThomasNetScraper
from app.scrapers.census import CensusBPScraper
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import org_scope

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.scraping.scrape_thomasnet", bind=True, max_retries=3)
def scrape_thomasnet(
    self,
    keyword: str = "press manufacturer",
    state: str = None,
    max_pages: int = 3,
    org_id: str = None,
):
    """Scrape ThomasNet for manufacturing companies.

    Args:
        keyword: Search keyword for ThomasNet
        state: Optional US state filter
        max_pages: Max pages to scrape (default 3)
        org_id: Organization the scraped companies belong to. Passed explicitly
            because Celery doesn't carry the request's org context over the wire;
            when omitted, companies fall back to the first-party org.
    """
    logger.info("Starting ThomasNet scrape: keyword='%s', state='%s'", keyword, state)

    async def _run():
        scraper = ThomasNetScraper()
        async with celery_session() as db, org_scope(UUID(org_id) if org_id else None):
            try:
                result = await scraper.run(
                    db, keyword=keyword, state=state, max_pages=max_pages
                )
                await db.commit()
                return result
            except Exception as e:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("ThomasNet scrape failed: %s", str(exc))
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))


@celery_app.task(name="app.tasks.scraping.scrape_census", bind=True, max_retries=3)
def scrape_census(
    self,
    naics_codes: list[str] = None,
    states: list[str] = None,
    min_employees: int = 50,
    org_id: str = None,
):
    """Query Census Bureau CBP API for manufacturing establishment data.

    Args:
        naics_codes: 4-digit NAICS codes to query
        states: FIPS state codes (e.g., ["39"] for Ohio)
        min_employees: Minimum employee count filter
        org_id: Organization the scraped companies belong to (see
            ``scrape_thomasnet``); falls back to the first-party org when omitted.
    """
    logger.info("Starting Census CBP scrape: naics=%s, states=%s", naics_codes, states)

    async def _run():
        scraper = CensusBPScraper()
        async with celery_session() as db, org_scope(UUID(org_id) if org_id else None):
            try:
                result = await scraper.run(
                    db,
                    naics_codes=naics_codes,
                    states=states,
                    min_employees=min_employees,
                )
                await db.commit()
                return result
            except Exception as e:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("Census scrape failed: %s", str(exc))
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
