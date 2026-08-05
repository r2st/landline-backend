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

from sqlalchemy import select, func, union_all

from app.config import get_settings
from app.tasks.org_runner import for_each_org
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend, WIRE_STATUSES
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment, OutreachEvent
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


#: Addresses the list query asks about per round trip. The candidate set is
#: bounded by the org's active pipeline and is normally small, but an `IN` list
#: is not somewhere to find that out.
_SUPPRESSION_CHUNK = 200


def _last_send_subquery():
    """Most recent message actually put on the wire, per contact.

    **Reads both send tables**, per "Sent mail lives in two tables" in
    ``CLAUDE.md``. It read ``EmailSend`` alone, and the sequence engine — where
    the bulk of outreach volume goes — writes no ``EmailSend`` row at all. So a
    prospect three steps into a live cadence had no send this query could see,
    came back ``last_sent_at IS NULL``, and was filed as *"Active-pipeline lead
    has never been emailed — send the first touch."*

    That is the failure mode this task exists to prevent, produced by the task
    itself: the follow-up queue filled with the contacts we were mailing most,
    and the reminder asked a human to open a cold first touch on top of an
    automated cadence already mid-flight. Being wrong in the "never contacted"
    direction is also what makes it self-sustaining — nothing the operator does
    writes an ``EmailSend`` row either unless they reply from the inbox, so the
    same contact resurfaces every run, and each one consumes a slot of
    ``auto_followup_batch_limit`` that a genuinely stale lead never gets.

    ``WIRE_STATUSES`` rather than every row, for the opposite error. A
    ``skipped`` send stamps ``sent_at`` and put no message on the wire — the
    template blast writes one for a contact with no address, one who
    unsubscribed and one on the list, and the dispatcher returns it for a
    spam-gate block, a warm-up hold, a spent cap and an engaged kill switch. Any
    of those read as "contacted recently" and silently held the lead out of the
    queue: the day a cap holds a blast is the day this stops chasing the leads
    it held.
    """
    direct = select(
        EmailSend.contact_id.label("contact_id"),
        EmailSend.sent_at.label("sent_at"),
    ).where(
        EmailSend.contact_id.isnot(None),
        EmailSend.status.in_(WIRE_STATUSES),
    )
    # ``OutreachEvent`` carries no ``contact_id`` — the enrollment holds it,
    # which is the whole reason this table is the easy one to forget.
    sequence = (
        select(
            OutreachEnrollment.contact_id.label("contact_id"),
            OutreachEvent.occurred_at.label("sent_at"),
        )
        .join(OutreachEnrollment, OutreachEnrollment.id == OutreachEvent.enrollment_id)
        .where(OutreachEvent.event_type == "sent")
    )
    both = union_all(direct, sequence).subquery()
    return (
        select(
            both.c.contact_id.label("contact_id"),
            func.max(both.c.sent_at).label("last_sent_at"),
        )
        .group_by(both.c.contact_id)
        .subquery()
    )


async def generate_stale_lead_followups_for_session(db) -> dict:
    """Core logic for stale-lead follow-up generation (testable with any session).

    A lead qualifies when: its company is in a chase-worthy stage, the contact
    may be contacted at all, the last message *on the wire* was more than
    ``stale_lead_followup_days`` ago (or there has never been one), and there is
    no pending follow-up already open for it.
    """
    from app.services import suppression as _suppression

    days = settings.stale_lead_followup_days
    stages = settings.stale_lead_followup_stages
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=days)

    last_send = _last_send_subquery()

    rows = await db.execute(
        select(Contact.id, Contact.company_id, Contact.email, last_send.c.last_sent_at)
        .join(Company, Company.id == Contact.company_id)
        .outerjoin(last_send, last_send.c.contact_id == Contact.id)
        .where(
            Contact.email.isnot(None),
            Contact.email != "",
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

    candidates: list[tuple] = []
    for contact_id, company_id, email, last_sent_at in rows.all():
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
        candidates.append((contact_id, company_id, email, reason))

    # The org do-not-contact LIST, which the two contact flags above cannot
    # answer: ``add_entry`` deliberately does not touch ``Contact.suppressed``,
    # because an address outlives every row that carries it. A reminder is not a
    # send, so nothing downstream refuses it — this task's whole output is
    # "reach out to this person", handed to a human who will. That is the same
    # mistake as enrolling them with a person carrying it out, and the only gate
    # in front of it is this one.
    #
    # Filtered in chunks, and *before* the batch limit is spent rather than
    # after: dropping a suppressed candidate from an already-capped batch would
    # let it consume a slot on every run forever, which is the starvation the
    # limit exists to bound.
    created = 0
    limit = settings.auto_followup_batch_limit
    for start in range(0, len(candidates), _SUPPRESSION_CHUNK):
        if created >= limit:
            break
        window = candidates[start:start + _SUPPRESSION_CHUNK]
        listed = await _suppression.suppressed_addresses(db, [c[2] for c in window])
        for contact_id, company_id, email, reason in window:
            if created >= limit:
                break
            if listed and _suppression.normalize_email(email) in listed:
                continue
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
