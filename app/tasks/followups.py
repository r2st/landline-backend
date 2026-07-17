"""Celery tasks for autonomous follow-up suggestions and lead re-scoring.

These keep the pipeline moving without a human poking it:

* ``generate_stale_lead_followups`` — for leads in an active pipeline stage that
  have gone quiet (no email in N days) and have no open reminder, create a
  pending follow-up so the lead resurfaces in the UI instead of going cold.
* ``rescore_active_leads`` — re-run rule-based scoring for active-pipeline
  companies so engagement signals (opens/clicks/replies, news) keep their
  fit score current.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func

from app.config import get_settings
from app.tasks.org_runner import for_each_org
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.follow_up import FollowUp
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
settings = get_settings()


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def generate_stale_lead_followups_for_session(db) -> dict:
    """Core logic for stale-lead follow-up generation (testable with any session).

    A lead qualifies when: its company is in a chase-worthy stage, the contact
    has an email and isn't unsubscribed, the last email send was more than
    ``stale_lead_followup_days`` ago (or never), and there is no pending
    follow-up already open for it.
    """
    days = settings.stale_lead_followup_days
    stages = settings.stale_lead_followup_stages
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=days)

    # Last email send per contact.
    last_send = (
        select(
            EmailSend.contact_id.label("contact_id"),
            func.max(EmailSend.sent_at).label("last_sent_at"),
        )
        .where(EmailSend.contact_id.isnot(None))
        .group_by(EmailSend.contact_id)
        .subquery()
    )

    rows = await db.execute(
        select(Contact.id, Contact.company_id, last_send.c.last_sent_at)
        .join(Company, Company.id == Contact.company_id)
        .outerjoin(last_send, last_send.c.contact_id == Contact.id)
        .where(
            Contact.email.isnot(None),
            Contact.unsubscribed.is_(False),
            Contact.suppressed.is_(False),
            Company.stage.in_(stages),
        )
    )

    # Contacts that already have an open reminder — don't nag twice.
    open_fu = await db.execute(
        select(FollowUp.contact_id).where(
            FollowUp.status == "pending", FollowUp.contact_id.isnot(None)
        )
    )
    has_open = {r[0] for r in open_fu.all()}

    created = 0
    for contact_id, company_id, last_sent_at in rows.all():
        if created >= settings.auto_followup_batch_limit:
            break
        if contact_id in has_open:
            continue
        # Normalize possibly-naive timestamps from SQLite.
        if last_sent_at is not None and last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
        if last_sent_at is not None and last_sent_at > threshold:
            continue  # contacted recently — not stale

        if last_sent_at is None:
            reason = "Active-pipeline lead has never been emailed — send the first touch."
        else:
            gap = (now - last_sent_at).days
            reason = f"No reply and {gap} days since last email — send a follow-up or call."

        db.add(FollowUp(
            contact_id=contact_id,
            company_id=company_id,
            due_date=now,
            reason=reason,
            status="pending",
        ))
        has_open.add(contact_id)
        created += 1

    logger.info("Created %d stale-lead follow-up reminders", created)
    return {"created": created}


async def rescore_active_leads_for_session(db) -> dict:
    """Core logic for re-scoring active-pipeline companies (testable)."""
    from app.services.scoring import score_company_rule_based

    stages = settings.stale_lead_followup_stages
    result = await db.execute(
        select(Company)
        .where(Company.stage.in_(stages))
        .order_by(Company.updated_at.asc())
        .limit(settings.rescore_batch_limit)
    )
    companies = list(result.scalars().all())

    rescored = 0
    for company in companies:
        try:
            await score_company_rule_based(db, company)
            rescored += 1
        except Exception as e:
            logger.warning("Re-score failed for %s: %s", company.name, str(e))
            continue

    logger.info("Re-scored %d active leads", rescored)
    return {"rescored": rescored, "candidates": len(companies)}


@celery_app.task(name="app.tasks.followups.generate_stale_lead_followups")
def generate_stale_lead_followups():
    """Beat task: create follow-up reminders for active-pipeline leads gone quiet."""
    logger.info("Scanning for stale active-pipeline leads needing follow-up")

    async def _run():
        # Per-org: leads / follow-ups are org-scoped.
        async def _body(db):
            try:
                result = await generate_stale_lead_followups_for_session(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label="generate_stale_lead_followups")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("generate_stale_lead_followups failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.followups.generate_weekly_lead_quality_report")
def generate_weekly_lead_quality_report():
    """Beat task: compute and log the weekly lead-quality report.

    Summarises the grade distribution, qualified vs disqualified counts, and new
    leads by source so the team gets a standing read on lead quality without
    opening the dashboard. Returns the report dict (also logged).
    """
    logger.info("Generating weekly lead-quality report")

    async def _run():
        from app.routers.reports import build_lead_quality_report

        # Per-org: the lead-quality report is computed per tenant.
        async def _body(db):
            report = await build_lead_quality_report(db, days=7)
            grades = report.get("grade_distribution", {})
            logger.info(
                "Weekly lead quality — total=%s grades A/B/C/D/F=%s/%s/%s/%s/%s "
                "qualified=%s disqualified=%s avg_fit=%s new(7d)=%s",
                report.get("total_companies"),
                grades.get("A"), grades.get("B"), grades.get("C"),
                grades.get("D"), grades.get("F"),
                report.get("qualified_leads"), report.get("disqualified_leads"),
                report.get("average_fit_score"), report.get("new_leads_in_window"),
            )
            return report

        return await for_each_org(_body, label="generate_weekly_lead_quality_report")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("generate_weekly_lead_quality_report failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.followups.rescore_active_leads")
def rescore_active_leads():
    """Beat task: re-run rule-based scoring for active-pipeline companies."""
    logger.info("Re-scoring active-pipeline leads")

    async def _run():
        # Per-org: active-pipeline companies are org-scoped.
        async def _body(db):
            try:
                result = await rescore_active_leads_for_session(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label="rescore_active_leads")

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("rescore_active_leads failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
