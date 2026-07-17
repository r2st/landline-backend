"""Celery tasks for Google Places SMB discovery (P4).

Two sweeps on the ``enrichment`` queue:

* ``run_scheduled_discoveries`` (weekly) — re-run every active saved search.
  **Weekly, not daily, is a deliberate cost decision**: local business data
  changes slowly, so a daily cadence would pay seven times over for nearly the
  same result set. Places' own dedup (``place_id``) means a re-run mostly
  returns businesses we already have.
* ``enrich_discovered_emails`` (daily) — walk staged businesses that have a
  website but no email and try to recover one (Hunter domain-search, then the
  free scraper). This is the hop where the funnel actually narrows, and it is
  separated from discovery on purpose: an email lookup shouldn't be gated on a
  Places search succeeding, and a Places search shouldn't block on scraping a
  few hundred websites.

Both no-op without ``GOOGLE_PLACES_API_KEY``, both stay inside the daily credit
budget, and neither imports anything by itself. **Import stays a human
decision**: Places yields business data, not people, so a staged business is a
lead on a prospect — auto-importing the long tail would fill the pipeline with
companies nobody can email.
"""

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select

from app.config import get_settings
from app.models.discovered_business import DiscoveredBusiness
from app.models.discovery_search import DiscoverySearch
from app.services.places_discovery import get_places_service
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

# Per-run ceilings. The credit budget is the real bound; these keep any one run
# inside the worker's soft time limit.
_MAX_SEARCHES_PER_RUN = 25
_MAX_ENRICH_PER_RUN = 100
# Results pulled per saved search. Places caps a query at 60 regardless.
_RESULTS_PER_SEARCH = 60


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _is_due(search: DiscoverySearch, now: datetime) -> bool:
    """Whether a saved search should run in this sweep.

    ``one_time`` runs once, ever — its ``last_run_at`` is the record of that.
    Cadence is checked against the last run rather than assumed from the beat
    interval, so a missed or re-fired beat doesn't double-charge or skip.
    """
    if not search.is_active:
        return False
    last = search.last_run_at
    if last is None:
        return True
    if search.schedule == "one_time":
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    # A day of slack, so a beat that fires slightly early doesn't defer a
    # search by a whole cycle.
    interval = timedelta(days=1) if search.schedule == "daily" else timedelta(days=7)
    return now - last >= interval - timedelta(hours=1)


async def _run_scheduled_discoveries() -> dict:
    places = get_places_service()
    if not places.enabled:
        logger.info("Places discovery: no GOOGLE_PLACES_API_KEY configured — skipping")
        return {"status": "skipped", "reason": "no_api_key", "searches": 0}

    now = datetime.now(timezone.utc)
    summary = {
        "status": "ok",
        "searches": 0,
        "created": 0,
        "duplicates": 0,
        "credits_spent": 0,
        "errors": 0,
    }

    # Per-org: saved Places searches are org-scoped. The daily credit budget is
    # global and enforced inside places.discover(), so a per-org sweep still
    # honours the platform-wide cap — a later org just gets budget_exhausted.
    async def _body(db):
        candidates = (
            await db.execute(
                select(DiscoverySearch)
                .where(DiscoverySearch.is_active.is_(True))
                # Never-run first, then longest-since-run: a new search shouldn't
                # wait behind a backlog, and a capped run should always advance
                # the most stale.
                .order_by(
                    DiscoverySearch.last_run_at.is_(None).desc(),
                    DiscoverySearch.last_run_at.asc(),
                )
                .limit(_MAX_SEARCHES_PER_RUN)
            )
        ).scalars().all()

        for search in candidates:
            if not _is_due(search, now):
                continue
            try:
                outcome = await places.discover(db, search, limit=_RESULTS_PER_SEARCH)
            except Exception as e:
                # One bad search must never fail the sweep.
                logger.exception("Places discovery failed for search %s", search.id)
                search.last_run_at = now
                search.last_status = "error"
                search.last_error = str(e)
                summary["errors"] += 1
                await db.commit()
                continue

            search.last_run_at = now
            search.last_status = outcome.status
            search.last_error = outcome.error
            search.results_count = (search.results_count or 0) + outcome.created
            search.credits_spent = (search.credits_spent or 0) + outcome.credits_spent
            summary["searches"] += 1
            summary["created"] += outcome.created
            summary["duplicates"] += outcome.duplicates
            summary["credits_spent"] += outcome.credits_spent
            await db.commit()

            if outcome.status == "budget_exhausted":
                # Stop the whole sweep, don't just skip this row: the budget is
                # global, so every remaining search would fail identically and
                # each one would still cost a gate check.
                logger.warning(
                    "Places daily budget exhausted — stopping sweep after %d search(es)",
                    summary["searches"],
                )
                summary["status"] = "budget_exhausted"
                break

    await for_each_org(_body, label="run_scheduled_discoveries")

    logger.info("Places discovery sweep: %s", summary)
    return summary


async def _enrich_discovered_emails() -> dict:
    settings = get_settings()
    if not settings.google_places_api_key:
        return {"status": "skipped", "reason": "no_api_key", "checked": 0}

    # by_source is the number worth watching: if Hunter is contributing nothing
    # over the free scraper, it isn't earning its credits on this pipeline.
    summary = {
        "status": "ok",
        "checked": 0,
        "enriched": 0,
        "not_found": 0,
        "by_source": Counter(),
    }
    places = get_places_service()

    # Per-org: discovered businesses are org-scoped. Hunter credit checks live
    # inside places.extract_email(), so the global budget still holds.
    async def _body(db):
        rows = (
            await db.execute(
                select(DiscoveredBusiness)
                .where(
                    DiscoveredBusiness.email.is_(None),
                    DiscoveredBusiness.domain.isnot(None),
                    # Only rows nobody has dispositioned yet. Enriching a
                    # skipped or already-imported business is pure credit burn.
                    or_(
                        DiscoveredBusiness.status == "new",
                        DiscoveredBusiness.status == "enriched",
                    ),
                )
                .order_by(DiscoveredBusiness.created_at.asc())
                .limit(_MAX_ENRICH_PER_RUN)
            )
        ).scalars().all()

        for business in rows:
            summary["checked"] += 1
            try:
                result = await places.extract_email(db, business)
            except Exception:
                logger.exception("Email extraction failed for %s", business.domain)
                continue
            if result.get("status") == "enriched":
                summary["enriched"] += 1
                summary["by_source"][result.get("source", "unknown")] += 1
            else:
                summary["not_found"] += 1
            # Deliberately no budget bail-out here. An exhausted Hunter budget
            # doesn't end the sweep — extract_email falls through to the free
            # scraper, which is still worth running for every remaining row.
            await db.commit()

    await for_each_org(_body, label="enrich_discovered_emails")

    # Plain dict: the task result is JSON-serialized by Celery.
    summary["by_source"] = dict(summary["by_source"])
    logger.info("Places email enrichment: %s", summary)
    return summary


@celery_app.task(name="app.tasks.places_discovery.run_scheduled_discoveries")
def run_scheduled_discoveries():
    """Re-run every due saved Places search. Weekly."""
    return _run_async(_run_scheduled_discoveries())


@celery_app.task(name="app.tasks.places_discovery.enrich_discovered_emails")
def enrich_discovered_emails():
    """Recover emails for staged businesses that have a website but no address."""
    return _run_async(_enrich_discovered_emails())
