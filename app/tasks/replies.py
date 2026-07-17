"""Celery task + routing logic for Dialtone reply classification (Feature 1).

``classify_new_replies`` runs every 5 minutes on the ``enrichment`` queue. It
picks up every ``email_replies`` row that hasn't been classified yet
(``reply_classification IS NULL`` — the idempotency guard), runs the keyword
pre-filter → :meth:`AIService.classify_reply`, applies the confidence gate, then
dispatches the per-category auto-action:

* ``interested``     → flag for the priority inbox + notify the operator.
* ``not_interested`` → complete the contact's active enrollments.
* ``out_of_office``  → re-queue the next step until the return date (or +7d).
* ``wrong_person``   → mark the contact for list cleanup + stash any referral.
* ``unsubscribe``    → suppress the contact everywhere + write a consent record.
* ``bounce``         → record the failed recipient in the bounce pipeline (hard
  bounce → suppressed), so a DSN that lands as a reply is handled like any other.
* ``unknown``        → no auto-action (surfaced for manual review).

The heavy lifting lives in plain ``async`` helpers so it is unit-testable
without Celery; the task is a thin ``_run_async`` wrapper.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.company import Company
from app.models.consent import ConsentRecord
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment
from app.services import bounce_handler
from app.services.ai_service import get_ai_service
from app.services.notifications import get_notification_service
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Auto-action confidence gate. A category from the LLM is only auto-actioned at
# or above this confidence; below it the reply is treated as ``unknown`` and
# left for manual review. Keyword hits bypass the gate (they are high-precision
# by construction, and unsubscribe must never be gated).
AUTO_ACTION_THRESHOLD = 0.75

# Default OOO re-queue delay when no return date is parseable.
OOO_DEFAULT_REQUEUE_DAYS = 7

# Enrollment statuses that a reply can still act on (a completed/bounced/
# unsubscribed enrollment is already terminal).
_ACTIONABLE_STATUSES = ("active", "paused")


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def apply_confidence_gate(
    category: str,
    confidence: float,
    source: str,
    threshold: float = AUTO_ACTION_THRESHOLD,
) -> str:
    """Return the *effective* category after the confidence gate.

    Keyword-sourced results (``source == 'keyword'``) are trusted outright —
    they are high-precision and, for unsubscribe, compliance-critical. An
    AI-sourced category below ``threshold`` degrades to ``unknown`` so a
    low-confidence guess doesn't trigger an auto-action.
    """
    if category == "unknown":
        return "unknown"
    if source == "keyword":
        return category
    if confidence >= threshold:
        return category
    return "unknown"


async def _enrollments_for_contact(
    db: AsyncSession, contact_id, statuses=_ACTIONABLE_STATUSES
) -> list[OutreachEnrollment]:
    """Load a contact's enrollments in the given (actionable) statuses."""
    if contact_id is None:
        return []
    result = await db.execute(
        select(OutreachEnrollment).where(
            OutreachEnrollment.contact_id == contact_id,
            OutreachEnrollment.status.in_(statuses),
        )
    )
    return list(result.scalars().all())


async def suppress_contact(db: AsyncSession, contact: Contact, reason: str) -> int:
    """Compliance-critical hard suppression for an unsubscribe.

    Sets the single-source-of-truth ``suppressed`` flag (and the legacy
    ``unsubscribed`` flag), writes a revoked consent record for the audit trail,
    and marks every actionable enrollment ``unsubscribed`` so the queue stops
    dispatching immediately. Returns the number of enrollments stopped.
    """
    contact.suppressed = True
    contact.unsubscribed = True

    now = datetime.now(timezone.utc)
    db.add(
        ConsentRecord(
            contact_id=contact.id,
            consent_type="none",
            basis=reason[:2000] if reason else "unsubscribe reply",
            granted_at=None,
            revoked_at=now,
        )
    )

    enrollments = await _enrollments_for_contact(db, contact.id)
    for enr in enrollments:
        enr.status = "unsubscribed"
        enr.completed_at = now
    return len(enrollments)


async def route_auto_action(
    db: AsyncSession,
    reply: EmailReply,
    contact: Optional[Contact],
    company: Optional[Company],
    effective_category: str,
    result: dict,
    notification_service=None,
) -> str:
    """Perform the per-category auto-action; return the ``handled_action`` code.

    Idempotent side effects only — the caller has already ensured this reply
    hasn't been classified before. Returns one of ``requeued`` | ``unsubscribed``
    | ``sequence_removed`` | ``flagged_cleanup`` | ``none``.
    """
    now = datetime.now(timezone.utc)
    contact_name = contact.full_name if contact else (reply.from_email or "a contact")
    company_name = company.name if company else "their company"

    if effective_category == "interested":
        # Route to the priority inbox and tell the operator. Do NOT auto-advance
        # or complete the sequence — a human takes it from here.
        reply.priority_flag = True
        notif = notification_service or get_notification_service()
        try:
            await notif.notify_reply(contact_name, company_name, reply.from_email or "")
        except Exception as exc:  # pragma: no cover - notification is best-effort
            logger.info("interested notify failed for reply %s: %s", reply.id, exc)
        return "none"

    if effective_category == "unsubscribe":
        # Compliance-critical: suppress everywhere + consent log. Also notify the
        # operator as an audit line.
        target = contact
        if target is None and reply.from_email:
            target = (
                await db.execute(
                    select(Contact).where(Contact.email == reply.from_email)
                )
            ).scalars().first()
        stopped = 0
        if target is not None:
            stopped = await suppress_contact(
                db, target, reason=f"Unsubscribe reply: {reply.subject or ''}"
            )
        notif = notification_service or get_notification_service()
        try:
            await notif._send_notification(
                subject=f"[Dialtone] Unsubscribe — {contact_name}",
                body=(
                    f"{contact_name} ({reply.from_email}) requested to unsubscribe. "
                    f"They have been suppressed across all sequences "
                    f"({stopped} enrollment(s) stopped)."
                ),
            )
        except Exception as exc:  # pragma: no cover - best-effort audit line
            logger.info("unsubscribe audit notify failed for reply %s: %s", reply.id, exc)
        return "unsubscribed"

    if effective_category == "out_of_office":
        # Re-queue: push the next step out to the return date (parsed) or +7d.
        # Keep the enrollment active so the queue resumes it automatically.
        return_date = result.get("return_date")
        resume_at = None
        if return_date:
            try:
                parsed = datetime.fromisoformat(str(return_date))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                resume_at = parsed
            except (ValueError, TypeError):
                resume_at = None
        if resume_at is None:
            resume_at = now + timedelta(days=OOO_DEFAULT_REQUEUE_DAYS)
        # Never let an OOO permanently drop an enrollment — only ever push it
        # forward, never backward.
        if resume_at <= now:
            resume_at = now + timedelta(days=OOO_DEFAULT_REQUEUE_DAYS)

        enrollments = await _enrollments_for_contact(db, reply.contact_id, statuses=("active",))
        for enr in enrollments:
            enr.next_send_at = resume_at
        reply.classification_meta = {
            **(reply.classification_meta or {}),
            "requeued_until": resume_at.isoformat(),
        }
        return "requeued"

    if effective_category == "bounce":
        # A delivery-failure notice for a dead address. Route it into the shared
        # bounce pipeline so the *failed recipient* (not the mail daemon that
        # sent the DSN) is recorded and suppressed on a hard bounce. The address
        # to suppress is the contact we resolved, else the original send's
        # recipient — never ``reply.from_email`` (that's mailer-daemon@…).
        target_email = contact.email if contact is not None else None
        if not target_email and reply.email_send_id is not None:
            send = (
                await db.execute(
                    select(EmailSend).where(EmailSend.id == reply.email_send_id)
                )
            ).scalars().first()
            if send is not None:
                target_email = send.to_email
        if not target_email:
            # Couldn't tie the bounce to a recipient — surface for manual review
            # rather than guess.
            return "none"
        summary = await bounce_handler.record_bounce(
            db,
            target_email,
            diagnostic=f"{reply.subject or ''} {(reply.body_text or reply.snippet or '')[:500]}",
        )
        reply.classification_meta = {
            **(reply.classification_meta or {}),
            "bounce": {
                "recipient": target_email,
                "kind": summary.get("kind"),
                "suppressed": summary.get("suppressed"),
            },
        }
        return "bounced"

    if effective_category == "not_interested":
        # Remove from active sequences (complete), no notification.
        enrollments = await _enrollments_for_contact(db, reply.contact_id)
        for enr in enrollments:
            enr.status = "completed"
            enr.completed_at = now
        return "sequence_removed"

    if effective_category == "wrong_person":
        # Flag for list cleanup; stash any referral for a Feature-4-style handoff.
        if contact is not None:
            contact.status = "wrong_person"
        referral = result.get("referral") or {}
        if referral.get("name") or referral.get("email"):
            reply.classification_meta = {
                **(reply.classification_meta or {}),
                "referral": {"name": referral.get("name"), "email": referral.get("email")},
            }
        enrollments = await _enrollments_for_contact(db, reply.contact_id)
        for enr in enrollments:
            enr.status = "completed"
            enr.completed_at = now
        return "flagged_cleanup"

    # unknown → surfaced for manual review, no side effects.
    return "none"


async def classify_reply_record(
    db: AsyncSession,
    reply: EmailReply,
    ai_service=None,
    notification_service=None,
) -> dict:
    """Classify one reply, persist the result, and dispatch its auto-action.

    Writes ``reply_classification`` / ``classification_confidence`` /
    ``priority_flag`` / ``classified_at`` / ``classification_meta`` /
    ``handled_action`` on the reply. Returns a small summary dict.
    """
    ai_service = ai_service or get_ai_service()

    # Resolve contact/company for context + routing (fall back to email match).
    contact = None
    if reply.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == reply.contact_id))
        ).scalars().first()
    if contact is None and reply.from_email:
        contact = (
            await db.execute(select(Contact).where(Contact.email == reply.from_email))
        ).scalars().first()

    company = None
    if reply.company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == reply.company_id))
        ).scalars().first()
    elif contact is not None:
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalars().first()

    contact_name = contact.full_name if contact else ""
    company_name = company.name if company else ""

    result = await ai_service.classify_reply(
        subject=reply.subject,
        body_text=reply.body_text or reply.snippet,
        original_subject=reply.subject or "",
        contact_name=contact_name,
        company_name=company_name,
        from_email=reply.from_email,
    )

    raw_category = result.get("category", "unknown")
    confidence = float(result.get("confidence", 0.0) or 0.0)
    source = result.get("source", "ai")
    effective_category = apply_confidence_gate(raw_category, confidence, source)

    # Persist classification fields.
    reply.reply_classification = effective_category
    reply.classification_confidence = confidence
    reply.classified_at = datetime.now(timezone.utc)
    reply.classification_meta = {
        **(reply.classification_meta or {}),
        "source": source,
        "model": result.get("model"),
        "reason": result.get("reason"),
        "raw_category": raw_category,
        "unsubscribe_explicit": result.get("unsubscribe_explicit", False),
        "return_date": result.get("return_date"),
        "referral": result.get("referral") or {"name": None, "email": None},
    }

    handled = await route_auto_action(
        db, reply, contact, company, effective_category, result,
        notification_service=notification_service,
    )
    reply.handled_action = handled

    logger.info(
        "Classified reply %s from %s → %s (conf=%.2f, source=%s) → action=%s",
        reply.id, reply.from_email, effective_category, confidence, source, handled,
    )
    return {
        "reply_id": str(reply.id),
        "category": effective_category,
        "raw_category": raw_category,
        "confidence": confidence,
        "source": source,
        "handled_action": handled,
    }


async def classify_new_replies_async(
    db: AsyncSession,
    ai_service=None,
    notification_service=None,
    limit: int = 200,
) -> dict:
    """Classify every not-yet-processed reply (``reply_classification IS NULL``).

    Idempotent: rows already carrying a classification are skipped, so re-runs
    (and the 5-minute Beat cadence overlapping a long run) never double-action.
    """
    result = await db.execute(
        select(EmailReply)
        .where(EmailReply.reply_classification.is_(None))
        .order_by(EmailReply.received_at.asc())
        .limit(limit)
    )
    replies = list(result.scalars().all())

    summaries = []
    for reply in replies:
        try:
            summaries.append(
                await classify_reply_record(
                    db, reply, ai_service=ai_service, notification_service=notification_service
                )
            )
        except Exception as exc:  # noqa: BLE001 — one bad reply must not stop the batch
            logger.error("Failed to classify reply %s: %s", reply.id, exc)

    by_category: dict[str, int] = {}
    for s in summaries:
        by_category[s["category"]] = by_category.get(s["category"], 0) + 1

    return {"processed": len(summaries), "by_category": by_category}


@celery_app.task(name="app.tasks.replies.classify_new_replies")
def classify_new_replies():
    """Periodic task: classify newly-arrived replies and route them (every 5m)."""
    logger.info("Classifying new email replies")

    async def _run():
        async with celery_session() as db:
            report = await classify_new_replies_async(db)
            await db.commit()
            return report

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("classify_new_replies failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
