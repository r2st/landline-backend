"""Celery task: ingest complaint / feedback-loop (FBL) reports from mailboxes.

Deliberately thin — all logic lives in :mod:`app.services.complaint_ingestion`,
mirroring the ``bounces`` task / ``bounce_ingestion`` split.

Runs on a schedule rather than a webhook because Gmail is the primary transport
and exposes no feedback loop: an ARF report from a provider FBL simply lands in
the monitored mailbox. The sweep is safe to repeat — every report is deduped on
``gmail_message_id`` before the (non-idempotent) complaint handler suppresses the
complainer.

Never raises: an ingestion bug must not crash the worker.
"""

import asyncio
import logging

from app.services.complaint_ingestion import get_complaint_ingestion_service
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


@celery_app.task(name="app.tasks.complaints.ingest_complaints")
def ingest_complaints():
    """Scan mailboxes for FBL/abuse reports and suppress the complainers."""

    async def _run():
        service = get_complaint_ingestion_service()

        # Per-org: each tenant's connected mailboxes / complaint records are
        # isolated, so the sweep runs once per organization with its context.
        async def _body(db):
            from app.config import get_settings

            lookback = int(getattr(get_settings(), "complaint_lookback_days", 3) or 3)
            result = await service.ingest_all_accounts(db, lookback_days=lookback)
            if result.get("complaints_ingested"):
                logger.info(
                    "Complaint sweep: %d ingested (%d suppressed) across %d accounts",
                    result["complaints_ingested"], result["contacts_suppressed"],
                    result["accounts_scanned"],
                )
            return result

        return await for_each_org(_body, label="ingest_complaints")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("ingest_complaints failed: %s", exc)
        return {"status": "error", "error": str(exc)}
