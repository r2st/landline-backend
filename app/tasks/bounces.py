"""Celery task: ingest bounce notifications from connected mailboxes.

Deliberately thin — all logic lives in :mod:`app.services.bounce_ingestion`,
mirroring the ``danger_signals`` task / ``danger_monitor`` split.

Runs on a schedule rather than a webhook because Gmail is the primary transport
and Gmail has no bounce webhook: a DSN simply lands in the mailbox. The sweep is
safe to repeat — every notification is deduplicated on ``gmail_message_id``
before the (non-idempotent) bounce handler sees it.

Never raises: an ingestion bug must not crash the worker.
"""

import asyncio
import logging

from app.services.bounce_ingestion import get_bounce_ingestion_service
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.bounces.ingest_bounces")
def ingest_bounces():
    """Scan mailboxes for delivery-failure notifications and suppress dead addresses."""

    async def _run():
        service = get_bounce_ingestion_service()

        # Per-org: each tenant's connected mailboxes / bounce records are
        # isolated, so the sweep runs once per organization with its context.
        async def _body(db):
            result = await service.ingest_all_accounts(db)
            if result.get("bounces_ingested"):
                logger.info(
                    "Bounce sweep: %d ingested (%d hard, %d soft) across %d accounts",
                    result["bounces_ingested"], result["hard"], result["soft"],
                    result["accounts_scanned"],
                )
            return result

        return await for_each_org(_body, label="ingest_bounces")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("ingest_bounces failed: %s", exc)
        return {"status": "error", "error": str(exc)}
