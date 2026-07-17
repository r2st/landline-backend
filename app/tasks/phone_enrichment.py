"""Celery task: enrich warm leads with a phone number.

``enrich_warm_lead_phones`` runs every 30 minutes on the ``enrichment`` queue.
It finds prospects that are showing real interest (3+ distinct opens, or any
click / reply) but have no phone on file, and asks Apollo for one — so the
operator can pick up the phone while the lead is warm instead of waiting on the
next email.

The heavy lifting lives in ``app.services.phone_enrichment`` as plain ``async``
helpers so it is unit-testable without Celery; this task is a thin
``_run_async`` wrapper that owns the session and the per-sweep batch size.
"""

import asyncio
import logging

from app.services.enrichment import ApolloEnrichmentService
from app.services.phone_enrichment import (
    enrich_contact_phone,
    warm_contacts_needing_phone,
)
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

# How many warm leads to enrich per sweep. Bounded so a burst of engagement
# can't spend the whole Apollo credit budget in one 30-minute tick; the rest
# roll to the next sweep.
BATCH_SIZE = 25


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def enrich_warm_lead_phones_async() -> dict:
    service = ApolloEnrichmentService()
    if not service.api_key:
        logger.info("Apollo API key not configured — skipping warm-lead phone enrichment")
        return {"status": "skipped", "reason": "no_api_key", "enriched": 0}

    # ``BATCH_SIZE`` bounds Apollo spend per sweep GLOBALLY, not per tenant, so a
    # single shared remaining-counter is threaded across every org's body —
    # otherwise each org would attempt a full batch and a busy platform could
    # blow the credit budget in one tick.
    remaining = {"n": BATCH_SIZE}

    async def _body(db):
        if remaining["n"] <= 0:
            return {"status": "ok", "enriched": 0, "candidates": 0}
        contacts = await warm_contacts_needing_phone(db, limit=remaining["n"])
        if not contacts:
            return {"status": "ok", "enriched": 0, "candidates": 0}

        enriched = 0
        results = []
        for contact in contacts:
            if remaining["n"] <= 0:
                break
            try:
                res = await enrich_contact_phone(db, contact, service=service)
                results.append(res)
                remaining["n"] -= 1
                if res.get("source"):
                    enriched += 1
            except Exception as exc:
                logger.warning(
                    "Phone enrichment failed for contact %s: %s", contact.id, exc
                )
                continue

        await db.commit()
        return {
            "status": "ok",
            "enriched": enriched,
            "candidates": len(contacts),
            "results": results,
        }

    per_org = await for_each_org(_body, label="enrich_warm_lead_phones")
    total_enriched = sum(r.get("enriched", 0) for r in per_org)
    total_candidates = sum(r.get("candidates", 0) for r in per_org)
    logger.info(
        "Warm-lead phone enrichment: found %d / %d attempted across orgs",
        total_enriched,
        total_candidates,
    )
    return {
        "status": "ok",
        "enriched": total_enriched,
        "candidates": total_candidates,
    }


@celery_app.task(name="app.tasks.phone_enrichment.enrich_warm_lead_phones")
def enrich_warm_lead_phones():
    """Periodic sweep: enrich warm leads (opens/clicks/replies) with a phone."""
    return _run_async(enrich_warm_lead_phones_async())
