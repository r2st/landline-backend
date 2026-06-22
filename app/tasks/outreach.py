"""Celery tasks for outreach email sequences."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from uuid import UUID

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.company import Company
from app.models.contact import Contact
from app.models.consent import ConsentRecord
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.consent import check_consent
from app.services.email_compose import render
from app.services.email_dispatch import send_outreach_email
from app.tasks.celery_app import celery_app
from app.services.send_limiter import get_send_limiter

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(name="app.tasks.outreach.send_sequence_step", bind=True, max_retries=3)
def send_sequence_step(self, enrollment_id: str):
    """Send the current step of a sequence to the enrolled contact.

    Checks consent, renders the template, sends via SES, and records the event.
    """
    logger.info("Processing sequence step for enrollment %s", enrollment_id)

    async def _run():
        async with celery_session() as db:
            try:
                # Load enrollment with related data
                result = await db.execute(
                    select(OutreachEnrollment)
                    .where(OutreachEnrollment.id == UUID(enrollment_id))
                )
                enrollment = result.scalar_one_or_none()
                if not enrollment:
                    return {"status": "not_found", "enrollment_id": enrollment_id}

                if enrollment.status != "active":
                    return {"status": "skipped", "reason": f"enrollment status is {enrollment.status}"}

                # Load contact
                result = await db.execute(
                    select(Contact).where(Contact.id == enrollment.contact_id)
                )
                contact = result.scalar_one_or_none()
                if not contact or not contact.email:
                    enrollment.status = "paused"
                    await db.commit()
                    return {"status": "paused", "reason": "no_contact_email"}

                # Load company for consent check
                result = await db.execute(
                    select(Company).where(Company.id == contact.company_id)
                )
                company = result.scalar_one_or_none()
                if not company:
                    return {"status": "error", "reason": "company_not_found"}

                # Check consent
                consent_result = await check_consent(db, contact, company)
                if not consent_result.can_email:
                    enrollment.status = "paused"
                    await db.commit()
                    logger.warning(
                        "Cannot email %s: %s", contact.email, consent_result.reason
                    )
                    return {
                        "status": "blocked",
                        "reason": consent_result.reason,
                        "regime": consent_result.regime,
                    }

                # Load the sequence step
                result = await db.execute(
                    select(SequenceStep).where(
                        and_(
                            SequenceStep.sequence_id == enrollment.sequence_id,
                            SequenceStep.step_number == enrollment.current_step,
                        )
                    )
                )
                step = result.scalar_one_or_none()
                if not step:
                    enrollment.status = "completed"
                    enrollment.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    return {"status": "completed", "reason": "no_more_steps"}

                # Build template context
                template_context = {
                    "first_name": contact.first_name or "",
                    "last_name": contact.last_name or "",
                    "full_name": contact.full_name,
                    "title": contact.title or "",
                    "company_name": company.name,
                    "company_domain": company.domain or "",
                    "company_industry": company.industry_segment or "",
                }

                # Send email via the active connected Gmail account (falls back
                # to SES when no account is connected). Templates are rendered
                # here; the dispatcher appends the CAN-SPAM footer.
                subject = render(step.subject_template, template_context)
                body = render(step.body_template, template_context)
                send_result = await send_outreach_email(
                    db,
                    to_email=contact.email,
                    subject=subject,
                    body_html=body,
                    body_text=body,
                )

                # Record event
                event_type = "sent" if send_result["status"] == "sent" else "bounced"
                event = OutreachEvent(
                    enrollment_id=enrollment.id,
                    step_number=enrollment.current_step,
                    event_type=event_type,
                    event_metadata=send_result,
                )
                db.add(event)

                if event_type == "sent":
                    enrollment.current_step += 1
                elif event_type == "bounced":
                    enrollment.status = "bounced"

                await db.commit()
                logger.info(
                    "Sent step %d to %s — result: %s",
                    step.step_number, contact.email, send_result["status"],
                )
                return {
                    "status": event_type,
                    "contact_email": contact.email,
                    "step": step.step_number,
                    "send_result": send_result,
                }

            except Exception as e:
                await db.rollback()
                raise

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("Outreach step failed for enrollment %s: %s", enrollment_id, str(exc))
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))


@celery_app.task(name="app.tasks.outreach.process_outreach_queue")
def process_outreach_queue():
    """Periodic task: find all active enrollments due for their next step and dispatch sends.

    Runs every 5 minutes via Celery Beat. Checks delay_days on each step
    against the last sent event to determine if the next step is due.
    """
    logger.info("Processing outreach queue")

    async def _run():
        async with celery_session() as db:
            result = await db.execute(
                select(OutreachEnrollment)
                .where(OutreachEnrollment.status == "active")
            )
            enrollments = list(result.scalars().all())
            dispatched = 0

            for enrollment in enrollments:
                # Check if enough time has passed since last send
                last_event_result = await db.execute(
                    select(OutreachEvent)
                    .where(
                        and_(
                            OutreachEvent.enrollment_id == enrollment.id,
                            OutreachEvent.event_type == "sent",
                        )
                    )
                    .order_by(OutreachEvent.occurred_at.desc())
                    .limit(1)
                )
                last_event = last_event_result.scalar_one_or_none()

                # Load current step to check delay
                step_result = await db.execute(
                    select(SequenceStep).where(
                        and_(
                            SequenceStep.sequence_id == enrollment.sequence_id,
                            SequenceStep.step_number == enrollment.current_step,
                        )
                    )
                )
                step = step_result.scalar_one_or_none()

                if not step:
                    enrollment.status = "completed"
                    enrollment.completed_at = datetime.now(timezone.utc)
                    continue

                # Check delay
                now = datetime.now(timezone.utc)
                if last_event:
                    due_date = last_event.occurred_at + timedelta(days=step.delay_days)
                    if now < due_date:
                        continue  # Not yet due
                else:
                    # First step — check against enrollment date
                    due_date = enrollment.enrolled_at + timedelta(days=step.delay_days)
                    if now < due_date:
                        continue

                # --- Anti-spam checks ---
                limiter = get_send_limiter()

                # Check daily limit — if hit, stop processing entirely
                can_send, reason = await limiter.can_send_today(db)
                if not can_send:
                    logger.warning("Daily send limit reached: %s — stopping queue processing", reason)
                    break

                # Load contact to check per-contact limits
                contact_result = await db.execute(
                    select(Contact).where(Contact.id == enrollment.contact_id)
                )
                contact = contact_result.scalar_one_or_none()
                if contact and contact.email:
                    can_send_contact, contact_reason = await limiter.can_send_to_contact(db, contact.email)
                    if not can_send_contact:
                        logger.info("Skipping enrollment %s: %s", enrollment.id, contact_reason)
                        continue

                # Dispatch the send
                send_sequence_step.delay(str(enrollment.id))
                dispatched += 1

            await db.commit()
            return {"active_enrollments": len(enrollments), "dispatched": dispatched}

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("process_outreach_queue failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.outreach.check_email_replies")
def check_email_replies():
    """Periodic task: check Gmail for replies and auto-respond with AI.

    Uses the SmartReplyService to detect replies, classify intent with Claude,
    draft human-like responses, and optionally auto-send for clear positive
    intents.  Falls back to logging-only when AI analysis is unavailable.
    """
    logger.info("Checking Gmail for outreach replies (smart reply)")

    async def _run():
        from app.services.smart_reply import get_smart_reply_service

        service = get_smart_reply_service()
        async with celery_session() as db:
            results = await service.process_replies(db)
            if results:
                logger.info("Processed %d replies", len(results))
                for r in results:
                    logger.info(
                        "Reply from %s (%s) -- intent: %s, auto-sent: %s",
                        r.get("contact_name"),
                        r.get("from_email"),
                        r.get("analysis", {}).get("intent", "unknown"),
                        r.get("auto_sent", False),
                    )
            else:
                logger.info("No new replies found")
            return results

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("check_email_replies failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
