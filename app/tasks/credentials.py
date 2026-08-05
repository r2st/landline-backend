"""Celery task: re-apply credential overrides on every worker.

:mod:`app.services.credential_store` resolves overrides once per process and
writes them onto the ``Settings`` singleton, because the fifteen call sites that
read ``settings.google_client_id`` are spread across request handlers, beat
tasks and synchronous OAuth helpers — several with no database session in hand.
That makes the resolution cheap and the *propagation* explicit: the worker that
served the write has the new value immediately, and every other worker has the
old one until it refreshes.

This is that refresh. Without it, rotating a credential from the dashboard would
take effect on one API worker and nowhere else until the next deploy — which is
worse than not having the feature, because the UI would report success while the
beat worker went on using the retired key.

Five minutes: these are credentials, not feature flags. They change about once a
year, and a shorter interval buys nothing but load.

Deliberately **not** per-org (no ``for_each_org``): the table is platform-wide,
and the value being applied is a process-global. Never raises — a refresh that
fails leaves the previously applied values in place, which is the last known
good state.
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


@celery_app.task(name="app.tasks.credentials.refresh_credentials")
def refresh_credentials():
    """Re-read ``app_credentials`` and apply it to this worker's settings."""

    async def _run():
        from app.database import async_session_factory
        from app.services import credential_store

        async with async_session_factory() as db:
            overrides = await credential_store.hydrate(db)
            return {"applied": sorted(overrides)}

    try:
        return _run_async(_run())
    except Exception as exc:
        # The previously applied values stay in force — last known good.
        logger.error("Credential refresh failed: %s", exc)
        return {"applied": [], "error": str(exc)}
