"""Celery tasks for web scraping."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.scrapers.thomasnet import ThomasNetScraper
from app.scrapers.census import CensusBPScraper
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async function in a new event loop for Celery workers."""
    loop = asyncio.new_event_loop()
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
):
    """Scrape ThomasNet for manufacturing companies.

    Args:
        keyword: Search keyword for ThomasNet
        state: Optional US state filter
        max_pages: Max pages to scrape (default 3)
    """
    logger.info("Starting ThomasNet scrape: keyword='%s', state='%s'", keyword, state)

    async def _run():
        scraper = ThomasNetScraper()
        async with async_session_factory() as db:
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
):
    """Query Census Bureau CBP API for manufacturing establishment data.

    Args:
        naics_codes: 4-digit NAICS codes to query
        states: FIPS state codes (e.g., ["39"] for Ohio)
        min_employees: Minimum employee count filter
    """
    logger.info("Starting Census CBP scrape: naics=%s, states=%s", naics_codes, states)

    async def _run():
        scraper = CensusBPScraper()
        async with async_session_factory() as db:
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
