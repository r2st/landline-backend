"""Celery task for RAG content indexing (feature 3.4)."""

import asyncio
import logging

from app.database_celery import celery_session
from app.services.rag_service import get_rag_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.rag.index_new_content")
def index_new_content(since_days: int = 7):
    """Embed recently created content into the knowledge base."""
    logger.info("Indexing new content into RAG store (last %d days)", since_days)

    async def _run():
        rag = get_rag_service()
        async with celery_session() as db:
            counts = await rag.index_new_content(db, since_days=since_days)
            await db.commit()
            logger.info("RAG indexing complete: %s", counts)
            return counts

    return _run_async(_run())
