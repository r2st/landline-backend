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
from app.models.follow_up import FollowUp
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.consent import check_consent
from app.services.email_compose import render, build_merge_context
from app.services.email_dispatch import send_outreach_email
from app.services.sequence_optimizer import get_sequence_optimizer, record_decision
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


async def _advance_step(db: AsyncSession, enrollment: OutreachEnrollment) -> None:
    """Advance an enrollment to its next step without sending.

    Increments ``current_step`` and either schedules ``next_send_at`` from the
    upcoming step's delay, or completes the enrollment when no step remains.
    Used by optimizer skip/switch-channel paths (the send path advances inline).
    """
    now = datetime.now(timezone.utc)
    enrollment.current_step += 1
    next_step = (
        await db.execute(
            select(SequenceStep).where(
                and_(
                    SequenceStep.sequence_id == enrollment.sequence_id,
                    SequenceStep.step_number == enrollment.current_step,
                )
            )
        )
    ).scalar_one_or_none()
    if next_step is not None:
        enrollment.next_send_at = now + timedelta(days=next_step.delay_days)
    else:
        enrollment.status = "completed"
        enrollment.completed_at = now


async def _prior_step_engagement(
    db: AsyncSession, enrollment: OutreachEnrollment, prior_step_number: int
) -> dict:
    """Whether the enrollment's prior step was opened / the contact ever replied.

    Reads ``OutreachEvent`` rows for this enrollment: an ``opened``/``clicked``
    event on ``prior_step_number`` means the prior touch was opened; a
    ``replied`` event on any step means they responded. Used to evaluate a
    step's ``send_condition``.
    """
    rows = (
        await db.execute(
            select(OutreachEvent.event_type, OutreachEvent.step_number).where(
                OutreachEvent.enrollment_id == enrollment.id
            )
        )
    ).all()
    opened = any(
        et in ("opened", "clicked") and sn == prior_step_number for et, sn in rows
    )
    replied = any(et == "replied" for et, _ in rows)
    return {"opened": opened, "replied": replied}


async def evaluate_step_condition(
    db: AsyncSession, enrollment: OutreachEnrollment, step: SequenceStep
) -> tuple[bool, str]:
    """Decide whether a step should send given its ``send_condition``.

    * ``always`` (or unknown) → always send.
    * ``if_not_opened`` → send only if the prior step was NOT opened.
    * ``if_opened_no_reply`` → send only if the prior step WAS opened and the
      contact has not replied.

    The first step (no prior touch) has nothing to branch on: ``if_not_opened``
    sends (nothing was opened yet); ``if_opened_no_reply`` does not (nothing was
    opened yet). Returns ``(should_send, reason)``.
    """
    condition = getattr(step, "send_condition", "always") or "always"
    if condition == "always":
        return True, "always"

    eng = await _prior_step_engagement(db, enrollment, step.step_number - 1)
    if condition == "if_not_opened":
        if eng["opened"]:
            return False, "prior step was opened — skipping the not-opened branch"
        return True, "prior step not opened"
    if condition == "if_opened_no_reply":
        if eng["replied"]:
            return False, "contact already replied — skipping"
        if not eng["opened"]:
            return False, "prior step not opened — skipping the opened-no-reply branch"
        return True, "prior step opened, no reply yet"
    return True, "unknown condition — defaulting to send"


@celery_app.task(name="app.tasks.outreach.send_sequence_step", bind=True, max_retries=3)
def send_sequence_step(self, enrollment_id: str):
    """Send the current step of a sequence to the enrolled contact.

    Checks consent, renders the template, sends via SES, and records the event.
    """
    logger.info("Processing sequence step for enrollment %s", enrollment_id)

    async def _run():
        async with celery_session() as db:
            try:
                # Global kill switch — never send while sending is halted. Checked
                # first so a paused system does no work and takes no side effects.
                from app.services import system_settings

                if not await system_settings.is_sending_enabled(db):
                    logger.warning(
                        "send_sequence_step halted for %s: kill switch engaged", enrollment_id
                    )
                    return {"status": "halted", "reason": "sending kill switch engaged",
                            "enrollment_id": enrollment_id}

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

                # Hard suppression (Feature 1): never send to a suppressed contact.
                if getattr(contact, "suppressed", False):
                    enrollment.status = "unsubscribed"
                    enrollment.completed_at = datetime.now(timezone.utc)
                    await db.commit()
                    logger.info(
                        "Enrollment %s stopped: contact %s is suppressed",
                        enrollment_id, contact.email,
                    )
                    return {"status": "skipped", "reason": "contact_suppressed"}

                # Recipient validation — a domain with no MX can't receive mail,
                # so sending guarantees a hard bounce. Validate once (lazily) and
                # pause the enrollment on a definitively invalid domain.
                from app.services.email_validation import validate_contact

                if contact.email_status is None:
                    await validate_contact(db, contact)
                if contact.email_status == "invalid":
                    enrollment.status = "paused"
                    await db.commit()
                    logger.warning(
                        "Enrollment %s paused: %s has an undeliverable domain",
                        enrollment_id, contact.email,
                    )
                    return {"status": "skipped", "reason": "invalid_recipient_domain",
                            "contact_email": contact.email}

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

                # --- Non-email channel step ---
                # A "call"/"linkedin"/"video"/"direct_mail" step isn't an email:
                # surface a human task on the right channel and advance the
                # sequence rather than sending mail.
                if step.channel and step.channel != "email":
                    reminder = FollowUp(
                        contact_id=contact.id,
                        company_id=company.id,
                        due_date=datetime.now(timezone.utc),
                        reason=f"Sequence step {step.step_number}: reach out via {step.channel}"[:500],
                        status="pending",
                    )
                    db.add(reminder)
                    await _advance_step(db, enrollment)
                    await db.commit()
                    logger.info(
                        "Non-email step %d (%s) for enrollment %s → follow-up task created",
                        step.step_number, step.channel, enrollment_id,
                    )
                    return {
                        "status": "channel_task_created",
                        "channel": step.channel,
                        "step": step.step_number,
                    }

                # --- Conditional branching ---
                # Honour the step's send_condition against the prior step's
                # engagement. When the condition isn't met, advance past the
                # step without sending.
                should_send, cond_reason = await evaluate_step_condition(db, enrollment, step)
                if not should_send:
                    await _advance_step(db, enrollment)
                    await db.commit()
                    logger.info(
                        "Condition '%s' not met for enrollment %s step %d: %s",
                        step.send_condition, enrollment_id, step.step_number, cond_reason,
                    )
                    return {
                        "status": "skipped_by_condition",
                        "condition": step.send_condition,
                        "reason": cond_reason,
                        "step": step.step_number,
                    }

                # Build template context (includes demo_link resolved by industry)
                template_context = build_merge_context(contact, company)
                # Keep backward compat fields used by older sequences
                template_context["company_domain"] = company.domain or ""
                template_context["company_industry"] = company.industry_segment or ""

                # --- Calendar Booking Injection (Feature 4) ---
                # Resolve {{booking_link}} to the sender's booking URL only when
                # the interest gate is met (or the step opts out of the gate via
                # ``always_inject``). Otherwise it renders empty and its
                # scaffolding line is stripped, so we never send a dangling
                # "Grab a time here:" with no link.
                from app.services.booking import (
                    apply_booking_placeholder,
                    resolve_booking_url,
                    should_inject_booking_link,
                )

                always_inject = bool(getattr(step, "always_inject", False))
                booking_url = ""
                if always_inject or await should_inject_booking_link(contact, enrollment, db):
                    booking_url = await resolve_booking_url(db, enrollment.sender_account_id)
                template_context["booking_link"] = booking_url

                # --- A/B testing (Feature 3) ---
                # Resolve which copy variant to send BEFORE rendering. For a step
                # with an active A/B test (≥2 active variants) this assigns a
                # variant and persists the assignment before the send (crash
                # safety); for every other step it returns the step's own
                # template, i.e. the exact current behaviour (backwards compat).
                from app.services.ab_test_resolver import (
                    backfill_assignment_send,
                    resolve_variant,
                )

                resolution = await resolve_variant(db, enrollment, step)

                # Send email via the active connected Gmail account (falls back
                # to SES when no account is connected). Render the chosen variant
                # copy, with the booking placeholder applied to it; the dispatcher
                # appends the CAN-SPAM footer.
                subject = render(
                    apply_booking_placeholder(resolution.subject_template, booking_url),
                    template_context,
                )
                body = render(
                    apply_booking_placeholder(resolution.body_template, booking_url),
                    template_context,
                )

                # --- AI-Optimized Sequence Engine (feature 1.2) ---
                # Ask the optimizer what to do with this step before sending. It
                # may adjust delay, rewrite content, switch channel, or skip. The
                # optimizer is fail-safe: it returns a plain "send" on any error
                # or when disabled, so this never blocks a send.
                optimizer = get_sequence_optimizer()
                decision = await optimizer.decide(
                    db, enrollment, contact, company, step, subject, body
                )
                record_decision(enrollment, enrollment.current_step, decision)
                action = decision.get("action", "send")

                if action == "skip":
                    # They're in a live conversation — don't pile on a scheduled
                    # touch. Advance past this step without sending.
                    await _advance_step(db, enrollment)
                    await db.commit()
                    logger.info(
                        "Optimizer SKIP for enrollment %s step %d: %s",
                        enrollment_id, step.step_number, decision.get("reasoning"),
                    )
                    return {
                        "status": "skipped_by_optimizer",
                        "step": step.step_number,
                        "reasoning": decision.get("reasoning"),
                    }

                if action == "delay":
                    delay_days = decision.get("delay_days", 1)
                    enrollment.next_send_at = datetime.now(timezone.utc) + timedelta(days=delay_days)
                    await db.commit()
                    logger.info(
                        "Optimizer DELAY %dd for enrollment %s step %d: %s",
                        delay_days, enrollment_id, step.step_number, decision.get("reasoning"),
                    )
                    return {
                        "status": "delayed_by_optimizer",
                        "step": step.step_number,
                        "delay_days": delay_days,
                        "reasoning": decision.get("reasoning"),
                    }

                if action == "switch_channel":
                    # Hold the email and surface a human task to reach out on the
                    # recommended channel. Advance the step so the email sequence
                    # continues from the next touch.
                    channel = decision.get("channel", "call")
                    reminder = FollowUp(
                        contact_id=contact.id,
                        company_id=company.id,
                        due_date=datetime.now(timezone.utc),
                        reason=f"AI: switch to {channel} — {decision.get('reasoning', '')}"[:500],
                        status="pending",
                    )
                    db.add(reminder)
                    await _advance_step(db, enrollment)
                    await db.commit()
                    logger.info(
                        "Optimizer SWITCH_CHANNEL→%s for enrollment %s step %d: %s",
                        channel, enrollment_id, step.step_number, decision.get("reasoning"),
                    )
                    return {
                        "status": "channel_switch_by_optimizer",
                        "step": step.step_number,
                        "channel": channel,
                        "reasoning": decision.get("reasoning"),
                    }

                if action == "rewrite":
                    # Send now, but with the AI-adapted copy.
                    if decision.get("subject"):
                        subject = decision["subject"]
                    if decision.get("body"):
                        body = decision["body"]
                    logger.info(
                        "Optimizer REWRITE for enrollment %s step %d: %s",
                        enrollment_id, step.step_number, decision.get("reasoning"),
                    )
                # action == "send" (or anything else): fall through and send as-is.

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
                    # A/B backfill: link the assignment to its send. The sequence
                    # path records no EmailSend row, so this is a no-op here
                    # (send_result carries none) and per-variant metrics attribute
                    # through the assignment's enrollment + step number instead.
                    if resolution.assignment_id is not None:
                        await backfill_assignment_send(
                            db, resolution.assignment_id, send_result.get("email_send_id")
                        )

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
            # Global kill switch pre-check — when engaged, dispatch nothing.
            from app.services import system_settings

            if not await system_settings.is_sending_enabled(db):
                logger.warning("Dialtone paused: outreach queue halted — sending kill switch engaged")
                return {"status": "halted", "reason": "sending kill switch engaged", "dispatched": 0}

            result = await db.execute(
                select(OutreachEnrollment)
                .where(OutreachEnrollment.status == "active")
            )
            enrollments = list(result.scalars().all())
            dispatched = 0
            skipped_window = 0

            # Anti-spam: don't send outside business hours / on weekends. Cold
            # mail that lands at 3 AM reads as a bot and tanks reply rates. The
            # window is evaluated per contact in THEIR timezone when known (see
            # the per-enrollment check below), so a lead in Los Angeles isn't
            # emailed at 5 AM their time even when the global window is open.
            limiter = get_send_limiter()

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

                # Load contact to check per-contact limits + timezone window
                contact_result = await db.execute(
                    select(Contact).where(Contact.id == enrollment.contact_id)
                )
                contact = contact_result.scalar_one_or_none()

                # Hard suppression (Feature 1): a contact suppressed via an
                # unsubscribe reply is never emailed again. Skip before any send.
                if contact and getattr(contact, "suppressed", False):
                    logger.info("Skipping enrollment %s: contact suppressed", enrollment.id)
                    continue

                # Timezone-aware send window: use the contact's own timezone when
                # set, else the global default. Skip (don't drop) if it's outside
                # their business-hours window right now.
                tz = getattr(contact, "timezone", None) if contact else None
                in_window, window_reason = limiter.within_send_window(tz)
                if not in_window:
                    skipped_window += 1
                    logger.debug("Enrollment %s outside send window: %s", enrollment.id, window_reason)
                    continue

                # Skip enrollments whose recipient domain is already known-bad.
                if contact and contact.email_status == "invalid":
                    logger.info("Skipping enrollment %s: invalid recipient domain", enrollment.id)
                    continue

                if contact and contact.email:
                    can_send_contact, contact_reason = await limiter.can_send_to_contact(db, contact.email)
                    if not can_send_contact:
                        logger.info("Skipping enrollment %s: %s", enrollment.id, contact_reason)
                        continue

                # Dispatch the send
                send_sequence_step.delay(str(enrollment.id))
                dispatched += 1

            await db.commit()
            return {
                "active_enrollments": len(enrollments),
                "dispatched": dispatched,
                "skipped_outside_window": skipped_window,
            }

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
