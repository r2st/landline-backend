"""Celery task: purge uploaded discovery document files after 30 days.

Only the file bytes go; the extracted candidates (and anything imported from
them) stay. Platform-wide on purpose — retention is the same for every org and
the sweep only nulls a column — so it runs unscoped rather than per org.
"""

import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.discovery_documents.purge_expired_document_files")
def purge_expired_document_files():
    async def _run():
        from app.database_celery import celery_session
        from app.services.discovery_documents import purge_expired_files

        async with celery_session() as db:
            purged = await purge_expired_files(db)
            await db.commit()
            return purged

    purged = _run_async(_run())
    logger.info("Purged %d expired discovery document files", purged)
    return {"purged": purged}
