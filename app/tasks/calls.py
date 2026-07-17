"""Celery tasks for the cold-call notification system.

These keep a rep's call workflow moving without manual poking:

* ``generate_morning_call_queue`` — daily at 8 AM: build a prioritised call
  list for the day.
* ``send_precall_reminders`` — notify ~15 min before a scheduled call.
* ``send_followup_reminders`` — notify when a scheduled callback is due.
* ``detect_hot_leads`` — alert when a contact opens an email 3+ times or clicks
  a link, and (if none is open) queue a hot call task.
* ``flag_missed_calls`` — at day's end, flag scheduled calls that weren't made.

Every trigger writes a :class:`CallNotification` row (the rep-facing queue read
by ``GET /calls/notifications``). The core logic of each lives in a
``*_for_session`` coroutine so it's testable against any AsyncSession.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import func, select

from app.tasks.org_runner import for_each_org
from app.models.call import CallNotification, CallTask
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.services.call_queue import generate_call_queue_for_session
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)

# Window before a scheduled call in which we fire the pre-call reminder.
PRECALL_WINDOW_MINUTES = 15
# Opens at/above this count on a single contact trigger a hot-lead alert.
HOT_OPEN_THRESHOLD = 3
_OPEN_CALL_STATUSES = ("pending", "rescheduled")


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _existing_notification_task_ids(db, notification_type: str, task_ids: list) -> set:
    """Task ids that already have a notification of ``notification_type`` — so we
    don't notify the same task twice."""
    if not task_ids:
        return set()
    rows = await db.execute(
        select(CallNotification.call_task_id).where(
            CallNotification.notification_type == notification_type,
            CallNotification.call_task_id.in_(task_ids),
        )
    )
    return {r[0] for r in rows.all()}


# --------------------------------------------------------------------------- #
# Core logic (testable)
# --------------------------------------------------------------------------- #


async def generate_morning_call_queue_for_session(db, limit: int = 25) -> dict:
    """Build the day's prioritised call queue and announce it."""
    result = await generate_call_queue_for_session(db, limit=limit)
    # Pre-warm AI custom scripts for the day's hot/warm leads so the rep's
    # briefing has a personalised script ready before they start dialling.
    try:
        from app.services import ai_script_generator
        await ai_script_generator.pregenerate_for_tasks(db, result["tasks"])
    except Exception as e:  # noqa: BLE001 - never fail the morning queue
        logger.warning("Pre-generating morning call scripts failed: %s", e)
    if result["generated"]:
        db.add(
            CallNotification(
                call_task_id=None,
                notification_type="upcoming_call",
                message=(
                    f"Your call queue for today is ready: {result['generated']} calls "
                    f"({result['hot']} hot, {result['warm']} warm, {result['cold']} cold)."
                ),
                sent_at=datetime.now(timezone.utc),
            )
        )
    return result


async def send_precall_reminders_for_session(db, now: datetime | None = None) -> dict:
    """Create reminders for calls scheduled within the next 15 minutes."""
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=PRECALL_WINDOW_MINUTES)

    tasks = list(
        (
            await db.execute(
                select(CallTask).where(
                    CallTask.status.in_(_OPEN_CALL_STATUSES),
                    CallTask.scheduled_at.isnot(None),
                )
            )
        ).scalars().all()
    )
    # Filter in Python so naive SQLite timestamps compare correctly.
    due = []
    for t in tasks:
        sched = _aware(t.scheduled_at)
        if sched is not None and now <= sched <= horizon:
            due.append(t)

    already = await _existing_notification_task_ids(
        db, "upcoming_call", [t.id for t in due]
    )
    created = 0
    for t in due:
        if t.id in already:
            continue
        sched = _aware(t.scheduled_at)
        company = (
            await db.execute(select(Company).where(Company.id == t.company_id))
        ).scalar_one_or_none()
        name = company.name if company else "a lead"
        db.add(
            CallNotification(
                call_task_id=t.id,
                notification_type="upcoming_call",
                message=(
                    f"Call {name} ({t.priority}) at "
                    f"{sched.strftime('%H:%M UTC') if sched else 'soon'}. "
                    "Open the briefing before you dial."
                ),
                sent_at=now,
            )
        )
        created += 1
    logger.info("Created %d pre-call reminders", created)
    return {"created": created, "due": len(due)}


async def send_followup_reminders_for_session(db, now: datetime | None = None) -> dict:
    """Remind on scheduled callbacks that are now due.

    A callback is a task with ``outcome='callback_scheduled'`` whose
    ``scheduled_at`` has arrived but the task is still open.
    """
    now = now or datetime.now(timezone.utc)
    tasks = list(
        (
            await db.execute(
                select(CallTask).where(
                    CallTask.status.in_(_OPEN_CALL_STATUSES),
                    CallTask.outcome == "callback_scheduled",
                    CallTask.scheduled_at.isnot(None),
                )
            )
        ).scalars().all()
    )
    due = [t for t in tasks if (_aware(t.scheduled_at) or now) <= now]

    already = await _existing_notification_task_ids(
        db, "follow_up_due", [t.id for t in due]
    )
    created = 0
    for t in due:
        if t.id in already:
            continue
        company = (
            await db.execute(select(Company).where(Company.id == t.company_id))
        ).scalar_one_or_none()
        name = company.name if company else "a lead"
        db.add(
            CallNotification(
                call_task_id=t.id,
                notification_type="follow_up_due",
                message=f"Callback due: you promised to call {name} back. Time to follow up.",
                sent_at=now,
            )
        )
        created += 1
    logger.info("Created %d callback reminders", created)
    return {"created": created, "due": len(due)}


async def detect_hot_leads_for_session(db, now: datetime | None = None) -> dict:
    """Alert on contacts that opened an email 3+ times or clicked a link.

    Fires once per contact (dedup via an existing unread hot_lead_alert tied to
    the contact's open call task). If the hot contact has no open call task, one
    is created so it lands in the queue.
    """
    now = now or datetime.now(timezone.utc)

    # Contacts with strong engagement signals from EmailSend.
    rows = await db.execute(
        select(
            EmailSend.contact_id,
            func.count(EmailSend.opened_at),
            func.count(EmailSend.clicked_at),
        )
        .where(EmailSend.contact_id.isnot(None))
        .group_by(EmailSend.contact_id)
    )
    hot_contacts = []
    for contact_id, opens, clicks in rows.all():
        if (clicks or 0) > 0 or (opens or 0) >= HOT_OPEN_THRESHOLD:
            hot_contacts.append((contact_id, opens or 0, clicks or 0))

    created_alerts = 0
    created_tasks = 0
    for contact_id, opens, clicks in hot_contacts:
        contact = (
            await db.execute(select(Contact).where(Contact.id == contact_id))
        ).scalar_one_or_none()
        if contact is None:
            continue

        # Find or create an open call task for this contact.
        task = (
            await db.execute(
                select(CallTask).where(
                    CallTask.contact_id == contact_id,
                    CallTask.status.in_(_OPEN_CALL_STATUSES),
                )
            )
        ).scalars().first()
        if task is None:
            task = CallTask(
                contact_id=contact_id,
                company_id=contact.company_id,
                status="pending",
                priority="hot",
                scheduled_at=now,
                notes="Auto-queued (hot): strong email engagement detected.",
            )
            db.add(task)
            await db.flush()
            created_tasks += 1
        elif task.priority != "hot":
            task.priority = "hot"

        # Dedup: skip if this task already has a hot_lead_alert.
        existing = await _existing_notification_task_ids(db, "hot_lead_alert", [task.id])
        if task.id in existing:
            continue

        signal = (
            f"clicked a link" if clicks else f"opened your email {opens} times"
        )
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalar_one_or_none()
        name = f"{contact.full_name} at {company.name}" if company else contact.full_name
        db.add(
            CallNotification(
                call_task_id=task.id,
                notification_type="hot_lead_alert",
                message=f"Hot lead: {name} {signal}. Call while it's fresh.",
                sent_at=now,
            )
        )
        created_alerts += 1

    logger.info(
        "Hot-lead detection: %d alerts, %d new call tasks", created_alerts, created_tasks
    )
    return {"alerts": created_alerts, "tasks_created": created_tasks}


async def flag_missed_calls_for_session(db, now: datetime | None = None) -> dict:
    """Flag open calls that were scheduled earlier today but never completed."""
    now = now or datetime.now(timezone.utc)
    today = now.date()

    tasks = list(
        (
            await db.execute(
                select(CallTask).where(
                    CallTask.status == "pending",
                    CallTask.scheduled_at.isnot(None),
                )
            )
        ).scalars().all()
    )
    missed = []
    for t in tasks:
        sched = _aware(t.scheduled_at)
        if sched is not None and sched <= now and sched.date() <= today:
            missed.append(t)

    already = await _existing_notification_task_ids(
        db, "missed_call", [t.id for t in missed]
    )
    created = 0
    for t in missed:
        if t.id in already:
            continue
        company = (
            await db.execute(select(Company).where(Company.id == t.company_id))
        ).scalar_one_or_none()
        name = company.name if company else "a lead"
        db.add(
            CallNotification(
                call_task_id=t.id,
                notification_type="missed_call",
                message=(
                    f"Missed call: {name} ({t.priority}) was scheduled today and "
                    "isn't done. Reschedule or mark it."
                ),
                sent_at=now,
            )
        )
        created += 1
    logger.info("Flagged %d missed calls", created)
    return {"flagged": created, "candidates": len(missed)}


# --------------------------------------------------------------------------- #
# Celery beat entrypoints
# --------------------------------------------------------------------------- #


def _beat(coro_factory, label: str):
    """Run a *_for_session coroutine per active org in a committed session.

    Call notifications, contacts and scheduled calls are all org-scoped, so each
    tenant's queue is built under its own org context.
    """
    async def _run():
        async def _body(db):
            try:
                result = await coro_factory(db)
                await db.commit()
                return result
            except Exception:
                await db.rollback()
                raise

        return await for_each_org(_body, label=label)

    try:
        return _run_async(_run())
    except Exception as exc:  # pragma: no cover - defensive logging path
        logger.error("%s failed: %s", label, str(exc))
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.calls.generate_morning_call_queue")
def generate_morning_call_queue():
    """Beat task: build the day's prioritised call queue (daily at 8 AM)."""
    logger.info("Generating morning call queue")
    return _beat(generate_morning_call_queue_for_session, "generate_morning_call_queue")


@celery_app.task(name="app.tasks.calls.send_precall_reminders")
def send_precall_reminders():
    """Beat task: remind ~15 min before scheduled calls."""
    return _beat(send_precall_reminders_for_session, "send_precall_reminders")


@celery_app.task(name="app.tasks.calls.send_followup_reminders")
def send_followup_reminders():
    """Beat task: remind on due callbacks."""
    return _beat(send_followup_reminders_for_session, "send_followup_reminders")


@celery_app.task(name="app.tasks.calls.detect_hot_leads")
def detect_hot_leads():
    """Beat task: alert on contacts showing strong email engagement."""
    return _beat(detect_hot_leads_for_session, "detect_hot_leads")


@celery_app.task(name="app.tasks.calls.flag_missed_calls")
def flag_missed_calls():
    """Beat task: flag scheduled calls not completed by end of day."""
    return _beat(flag_missed_calls_for_session, "flag_missed_calls")
