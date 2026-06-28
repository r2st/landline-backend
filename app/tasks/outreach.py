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
from app.services.email_compose import render, build_merge_context
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

                # Delay guard: verify enough time has passed since the last
                # send before firing the next step. This prevents premature
                # sends when the task is dispatched by both enroll and the
                # periodic processor (double-dispatch race).
                if enrollment.current_step > 1 and step.delay_days > 0:
                    now = datetime.now(timezone.utc)

                    # Prefer next_send_at (set by previous step completion)
                    if enrollment.next_send_at is not None:
                        due_at = enrollment.next_send_at
                        if due_at.tzinfo is None:
                            due_at = due_at.replace(tzinfo=timezone.utc)
                        if now < due_at:
                            logger.info(
                                "Enrollment %s step %d not yet due (due %s)",
                                enrollment_id, enrollment.current_step, due_at,
                            )
                            return {
                                "status": "deferred",
                                "reason": f"step not due until {due_at.isoformat()}",
                                "step": enrollment.current_step,
                            }
                    else:
                        # Fallback: check last sent event timestamp
                        last_sent = await db.execute(
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
                        last_event = last_sent.scalar_one_or_none()
                        if last_event:
                            occurred = last_event.occurred_at
                            if occurred.tzinfo is None:
                                occurred = occurred.replace(tzinfo=timezone.utc)
                            due_at = occurred + timedelta(days=step.delay_days)
                            if now < due_at:
                                # Set next_send_at for efficiency on next check
                                enrollment.next_send_at = due_at
                                await db.commit()
                                logger.info(
                                    "Enrollment %s step %d not yet due (due %s, from last event)",
                                    enrollment_id, enrollment.current_step, due_at,
                                )
                                return {
                                    "status": "deferred",
                                    "reason": f"step not due until {due_at.isoformat()}",
                                    "step": enrollment.current_step,
                                }

                # Build template context (includes demo_link resolved by industry)
                template_context = build_merge_context(contact, company)
                # Keep backward compat fields used by older sequences
                template_context["company_domain"] = company.domain or ""
                template_context["company_industry"] = company.industry_segment or ""

                # Send email via the active connected Gmail account (falls back
                # to SES when no account is connected). Templates are rendered
                # here; the dispatcher appends the CAN-SPAM footer.
                subject = render(step.subject_template, template_context)
                body = render(step.body_template, template_context)

                # Thread-linking: for steps after the first, chain into the
                # same Gmail thread so the prospect sees a single conversation.
                thread_id = None
                in_reply_to = None
                references = None
                send_account_id = None

                if enrollment.current_step > 1 and enrollment.gmail_thread_id:
                    thread_id = enrollment.gmail_thread_id
                    if enrollment.gmail_message_id:
                        # Gmail message IDs need angle brackets for RFC 2822
                        msg_ref = f"<{enrollment.gmail_message_id}@mail.gmail.com>"
                        in_reply_to = msg_ref
                        references = msg_ref
                    # Prefix "Re: " to match Gmail threading convention
                    if not subject.lower().startswith("re:"):
                        subject = f"Re: {subject}"

                if enrollment.sender_account_id:
                    send_account_id = enrollment.sender_account_id

                send_result = await send_outreach_email(
                    db,
                    to_email=contact.email,
                    subject=subject,
                    body_html=body,
                    sender_account_id=send_account_id,
                    thread_id=thread_id,
                    in_reply_to=in_reply_to,
                    references=references,
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
                    # Store threading info for subsequent steps
                    if send_result.get("gmail_thread_id"):
                        enrollment.gmail_thread_id = send_result["gmail_thread_id"]
                    if send_result.get("gmail_message_id"):
                        enrollment.gmail_message_id = send_result["gmail_message_id"]
                    if send_result.get("sender_account_id"):
                        from uuid import UUID as _UUID
                        enrollment.sender_account_id = _UUID(send_result["sender_account_id"])

                    # Advance to next step and calculate next_send_at
                    enrollment.current_step += 1

                    # Look ahead to see if there's a next step and set next_send_at
                    next_step_result = await db.execute(
                        select(SequenceStep).where(
                            and_(
                                SequenceStep.sequence_id == enrollment.sequence_id,
                                SequenceStep.step_number == enrollment.current_step,
                            )
                        )
                    )
                    next_step = next_step_result.scalar_one_or_none()
                    if next_step:
                        enrollment.next_send_at = datetime.now(timezone.utc) + timedelta(
                            days=next_step.delay_days
                        )
                    else:
                        enrollment.status = "completed"
                        enrollment.completed_at = datetime.now(timezone.utc)
                elif event_type == "bounced":
                    enrollment.status = "bounced"

                await db.commit()
                logger.info(
                    "Sent step %d to %s (thread=%s) — result: %s",
                    step.step_number, contact.email,
                    enrollment.gmail_thread_id, send_result["status"],
                )
                return {
                    "status": event_type,
                    "contact_email": contact.email,
                    "step": step.step_number,
                    "send_result": send_result,
                    "gmail_thread_id": enrollment.gmail_thread_id,
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

            # Anti-spam: don't send outside business hours / on weekends. Cold
            # mail that lands at 3 AM reads as a bot and tanks reply rates.
            limiter = get_send_limiter()
            in_window, window_reason = limiter.within_send_window()
            if not in_window:
                logger.info("Skipping outreach queue: %s", window_reason)
                return {"active_enrollments": len(enrollments), "dispatched": 0, "reason": window_reason}

            now = datetime.now(timezone.utc)
            for enrollment in enrollments:
                # Fast path: if next_send_at is set, use it directly
                if enrollment.next_send_at is not None:
                    next_at = enrollment.next_send_at
                    if next_at.tzinfo is None:
                        next_at = next_at.replace(tzinfo=timezone.utc)
                    if now < next_at:
                        continue  # Not yet due
                else:
                    # Legacy path: check delay from last event
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
                        enrollment.completed_at = now
                        continue

                    # Check delay
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


@celery_app.task(name="app.tasks.outreach.sync_email_replies")
def sync_email_replies():
    """Periodic task: sync email replies from Gmail to local DB."""

    async def _run():
        from app.services.email_history import EmailHistoryService

        service = EmailHistoryService()
        async with celery_session() as db:
            result = await service.sync_all_accounts(db)
            return result

    try:
        return _run_async(_run())
    except Exception as exc:
        logger.error("sync_email_replies failed: %s", str(exc))
        return {"status": "error", "error": str(exc)}
