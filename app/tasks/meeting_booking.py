"""Celery tasks for the Meeting Booking Agent.

Two periodic sweeps, both org-scoped via :func:`for_each_org`:

* :func:`check_booking_replies` (every 15m) — for each ``proposed`` meeting,
  look for a new reply in its Gmail thread, parse which offered slot the prospect
  picked (or whether they want a different time), and confirm + invite / re-offer
  accordingly.
* :func:`send_meeting_reminders` (every 30m) — send the one-hour pre-meeting
  reminder for confirmed meetings whose start is inside the look-ahead window and
  that haven't been reminded yet; and mark meetings whose end has passed
  ``completed``.

The scheduling logic lives in :mod:`app.services.meeting_booking_agent`; these
tasks are thin per-org orchestration + the DB queries that find the work.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.meeting_booking import BookedMeeting, MeetingBookingAgent
from app.services import meeting_booking_agent as agent_service
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

# How far ahead of a meeting's start the reminder fires. The sweep runs every
# 30m, so a 90-minute window guarantees every meeting gets exactly one reminder
# roughly an hour out without needing minute-level precision.
REMINDER_LOOKAHEAD_MINUTES = 90


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _last_contact_turn_at(meeting: BookedMeeting) -> datetime | None:
    """Timestamp of the most recent contact turn already recorded on the thread.

    Used so we only act on replies that arrived *after* what we've already
    processed — the idempotency guard for the polling sweep.
    """
    latest = None
    for turn in meeting.conversation_thread or []:
        if turn.get("role") != "contact":
            continue
        raw = turn.get("at")
        if not raw:
            continue
        try:
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if latest is None or ts > latest:
            latest = ts
    return latest


async def _new_reply_for_meeting(
    db: AsyncSession, meeting: BookedMeeting
) -> EmailReply | None:
    """The newest unprocessed reply in this meeting's thread, if any.

    Matches on ``gmail_thread_id`` and requires the reply to be newer than both
    the meeting's creation and the last contact turn we already logged.
    """
    if not meeting.gmail_thread_id:
        return None
    after = _last_contact_turn_at(meeting) or meeting.created_at
    stmt = (
        select(EmailReply)
        .where(EmailReply.gmail_thread_id == meeting.gmail_thread_id)
        .order_by(EmailReply.received_at.desc())
        .limit(1)
    )
    reply = (await db.execute(stmt)).scalar_one_or_none()
    if reply is None:
        return None
    received = reply.received_at
    if received is not None and received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    if after is not None:
        after_aware = after if after.tzinfo else after.replace(tzinfo=timezone.utc)
        if received is not None and received <= after_aware:
            return None
    return reply


async def _load_contact(db: AsyncSession, meeting: BookedMeeting) -> Contact | None:
    if meeting.contact_id is None:
        return None
    return (
        await db.execute(select(Contact).where(Contact.id == meeting.contact_id))
    ).scalar_one_or_none()


async def check_booking_replies_async(db: AsyncSession, *, now: datetime | None = None) -> dict:
    """Advance every ``proposed`` meeting that has a fresh reply. Idempotent."""
    now = now or datetime.now(timezone.utc)
    agent = (await db.execute(select(MeetingBookingAgent).limit(1))).scalar_one_or_none()
    if agent is None:
        return {"processed": 0, "confirmed": 0, "reproposed": 0}

    meetings = list(
        (
            await db.execute(
                select(BookedMeeting).where(BookedMeeting.status == "proposed")
            )
        ).scalars().all()
    )

    confirmed = reproposed = processed = 0
    for meeting in meetings:
        reply = await _new_reply_for_meeting(db, meeting)
        if reply is None:
            continue
        processed += 1
        contact = await _load_contact(db, meeting)
        reply_text = reply.body_text or reply.snippet or ""

        # Record the prospect's turn so the next sweep won't re-process it.
        meeting.conversation_thread = [
            *(meeting.conversation_thread or []),
            {"role": "contact", "text": reply_text, "at": now.isoformat()},
        ]

        selection = await agent_service.handle_slot_selection(reply_text, meeting.proposed_slots or [])
        if selection.get("selected_slot"):
            await agent_service.confirm_meeting(
                db, meeting=meeting, agent=agent, slot=selection["selected_slot"],
                contact=contact, now=now,
            )
            confirmed += 1
        elif selection.get("wants_reschedule"):
            did = await agent_service.repropose_meeting(
                db, meeting=meeting, agent=agent, contact=contact, now=now,
            )
            if did:
                reproposed += 1
        # else: an ambiguous reply — left in 'proposed' for the next sweep / a human.

    return {"processed": processed, "confirmed": confirmed, "reproposed": reproposed}


async def send_meeting_reminders_async(db: AsyncSession, *, now: datetime | None = None) -> dict:
    """Send due reminders and mark elapsed meetings completed. Idempotent."""
    now = now or datetime.now(timezone.utc)
    agent = (await db.execute(select(MeetingBookingAgent).limit(1))).scalar_one_or_none()

    reminded = completed = 0

    # 1. Mark confirmed meetings whose end time has passed as completed.
    elapsed = list(
        (
            await db.execute(
                select(BookedMeeting).where(
                    BookedMeeting.status == "confirmed",
                    BookedMeeting.end_time.isnot(None),
                    BookedMeeting.end_time < now,
                )
            )
        ).scalars().all()
    )
    for meeting in elapsed:
        meeting.status = "completed"
        completed += 1

    if agent is None:
        return {"reminded": 0, "completed": completed}

    # 2. Send reminders for upcoming confirmed meetings not yet reminded.
    horizon = now + timedelta(minutes=REMINDER_LOOKAHEAD_MINUTES)
    due = list(
        (
            await db.execute(
                select(BookedMeeting).where(
                    BookedMeeting.status == "confirmed",
                    BookedMeeting.reminder_sent_at.is_(None),
                    BookedMeeting.start_time.isnot(None),
                    BookedMeeting.start_time > now,
                    BookedMeeting.start_time <= horizon,
                )
            )
        ).scalars().all()
    )
    for meeting in due:
        contact = await _load_contact(db, meeting)
        # send_reminder stamps reminder_sent_at before sending, so a failed send
        # still won't double-remind on the next sweep. That is deliberate — one
        # missed reminder beats a retry loop mailing someone every 15 minutes —
        # and it is exactly why the count must come from the return value. This
        # counted the *attempt*, so the report read "reminded: 12" on a sweep
        # where a dead Gmail token meant twelve people were told nothing, and
        # the stamp guarantees no later sweep will correct it.
        if await agent_service.send_reminder(
            db, meeting=meeting, agent=agent, contact=contact, now=now
        ):
            reminded += 1

    return {"reminded": reminded, "completed": completed}


@celery_app.task(name="app.tasks.meeting_booking.check_booking_replies")
def check_booking_replies():
    """Periodic: confirm/re-offer proposed meetings from prospect replies (15m)."""
    logger.info("Checking meeting-booking replies")

    async def _run():
        async def _body(db):
            report = await check_booking_replies_async(db)
            await db.commit()
            return report

        return await for_each_org(_body, label="check_booking_replies")

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("check_booking_replies failed: %s", exc)
        return {"status": "error", "error": str(exc)}


@celery_app.task(name="app.tasks.meeting_booking.send_meeting_reminders")
def send_meeting_reminders():
    """Periodic: send pre-meeting reminders + close out elapsed meetings (30m)."""
    logger.info("Sending meeting reminders")

    async def _run():
        async def _body(db):
            report = await send_meeting_reminders_async(db)
            await db.commit()
            return report

        return await for_each_org(_body, label="send_meeting_reminders")

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("send_meeting_reminders failed: %s", exc)
        return {"status": "error", "error": str(exc)}
