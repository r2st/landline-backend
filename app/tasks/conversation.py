"""Celery tasks for Conversation Intelligence (feature 1.5)."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database_celery import celery_session
from app.models.email_reply import EmailReply
from app.services.conversation_intelligence import get_conversation_intelligence_service
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org, org_scope

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.conversation.analyze_thread")
def analyze_thread(gmail_thread_id: str):
    """Analyze a single email thread and store a ConversationAnalysis."""
    logger.info("Analyzing thread %s", gmail_thread_id)

    async def _run():
        service = get_conversation_intelligence_service()
        async with celery_session() as db:
            # This task is dispatched per-thread, so the org context set by the
            # scheduler is lost over the Celery wire. Re-derive the owning org
            # from the thread's reply (unfiltered) and bind it before analyzing,
            # so the ConversationAnalysis is read/written in the right tenant.
            org_id = (
                await db.execute(
                    select(EmailReply.org_id)
                    .where(EmailReply.gmail_thread_id == gmail_thread_id)
                    .limit(1)
                    .execution_options(skip_org_filter=True)
                )
            ).scalar_one_or_none()
            async with org_scope(org_id):
                analysis = await service.analyze_thread(db, gmail_thread_id)
                if analysis is None:
                    return {"status": "no_analysis", "thread_id": gmail_thread_id}
                return {
                    "status": "analyzed",
                    "thread_id": gmail_thread_id,
                    "momentum": analysis.momentum_score,
                    "label": analysis.momentum_label,
                }

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("analyze_thread failed for %s: %s", gmail_thread_id, str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.conversation.analyze_active_threads")
def analyze_active_threads(limit: int = 25):
    """Periodic task: analyze threads with recent two-way activity.

    Finds gmail_thread_ids that have both a sent email and a received reply, with
    activity in the last ``conversation_active_days`` days, and dispatches a
    per-thread analysis for each. Only threads that got a real reply are worth
    the (paid) thread-level analysis.
    """
    settings = get_settings()
    if not settings.conversation_intelligence_enabled:
        return {"status": "disabled"}

    logger.info("Scanning for active threads to analyze (limit=%d)", limit)

    async def _run():
        # Per-org: replies / threads are org-scoped. Each org's active threads
        # are found and dispatched under that org's context.
        async def _body(db):
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=settings.conversation_active_days
            )

            # Threads that received a reply recently (the signal that a thread is
            # a live conversation worth analyzing).
            recent_reply_threads = (
                await db.execute(
                    select(EmailReply.gmail_thread_id)
                    .where(
                        EmailReply.gmail_thread_id.isnot(None),
                        EmailReply.received_at >= cutoff,
                    )
                    .distinct()
                )
            ).scalars().all()

            thread_ids = [t for t in recent_reply_threads if t]
            dispatched = 0
            for tid in thread_ids[:limit]:
                try:
                    analyze_thread.delay(tid)
                    dispatched += 1
                except Exception as exc:  # pragma: no cover - broker-dependent
                    logger.warning("Could not dispatch analysis for %s: %s", tid, exc)
            return {"active_threads": len(thread_ids), "dispatched": dispatched}

        return await for_each_org(_body, label="analyze_active_threads")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("analyze_active_threads failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
