"""The sweep that actually sends a "send later" batch.

``POST /email-templates/compose`` accepts a ``scheduled_at`` — the frontend's
"Send later" mode posts it — and wrote an ``EmailSend`` row with
``status='scheduled'``. **Nothing anywhere read that status back.** There was no
beat entry, no sweep, no query for it: the row sat in the table forever while
the endpoint had already told the operator "scheduled for later" and counted it
in the batch's ``sent`` total. A feature that reports success and never does
anything is worse than one that isn't there, because the operator stops watching
for the mail.

Two properties matter more than the sweep itself:

- **Claim before sending, on a committed transaction.** Same reasoning as
  ``app.services.send_claim`` for sequence steps: the gap between "the transport
  accepted it" and "the row saying so committed" is where duplicates come from.
  The status flip out of ``scheduled`` is a conditional UPDATE committed on its
  own, so a crash mid-send leaves the row claimed rather than eligible, and two
  workers racing the same row have exactly one winner.
- **Go through the dispatcher.** ``send_outreach_email`` is where the kill
  switch, the do-not-contact list, the spam gate and both volume caps live. A
  scheduled send is cold outreach with a delay on it, not an exemption from any
  of that — a message queued on Monday and sent on Thursday must still see the
  Wednesday unsubscribe.
- **A hold puts the row back.** Three of those gates are temporary — the kill
  switch, the daily cap, the warm-up ceiling — and a message that meets one of
  them has not been refused, it has been asked to wait. Writing the dispatcher's
  hold status onto the row was fatal, because nothing selects a row that is no
  longer ``scheduled``. See :func:`_is_transient_hold`.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend
from app.services import template_attachments
from app.services.email_dispatch import send_outreach_email
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

#: How many due rows one pass will send per org. A blast scheduled for a single
#: minute should not become one unbounded Celery task; the next pass takes the
#: rest, and the daily cap governs the real ceiling either way.
BATCH_LIMIT = 100

#: How far forward a held row's ``scheduled_at`` moves before it is due again.
#: Long enough that a spent daily cap or an engaged kill switch is not re-asked
#: every minute; short enough that a hold lifting mid-morning does not cost the
#: message the rest of the day.
HOLD_BACKOFF = timedelta(minutes=30)


def _is_transient_hold(result: dict) -> bool:
    """Whether the dispatcher *held* this message rather than refusing it.

    The dispatcher declines a send for five reasons and they are not the same
    kind of answer:

    * the kill switch is engaged (``halted``) — an operator's pause, which they
      will lift;
    * the warm-up ramp or the daily cap is spent (``skipped``) — a ceiling that
      resets on its own;
    * the recipient is on the do-not-contact list (``suppressed``) — permanent,
      and the whole reason a week-old scheduled send is re-checked at send time;
    * the spam gate blocked the copy (``skipped`` + ``spam_blocked``) — a
      deterministic verdict on this exact body, so retrying it unchanged is just
      the same refusal on a timer;
    * anything else (``error``, a bounce) — already terminal.

    The first two are holds. Everything else is a decision.
    """
    if result.get("spam_blocked"):
        return False
    return result.get("status") in ("halted", "skipped")


def _run_async(coro):
    """Run async code in Celery's sync context with a fresh event loop."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _claim(db: AsyncSession, send_id) -> bool:
    """Flip one row out of ``scheduled`` and commit, returning whether we won.

    The ``status == 'scheduled'`` predicate is the whole interlock: whoever's
    UPDATE lands first changes the row out from under the other, whose
    ``rowcount`` is then 0. Committed immediately so the claim survives a
    rollback of the send that follows it.
    """
    result = await db.execute(
        update(EmailSend)
        .where(EmailSend.id == send_id, EmailSend.status == "scheduled")
        .values(status="pending")
    )
    await db.commit()
    return result.rowcount == 1


async def send_due_scheduled_for_session(db: AsyncSession, *, now=None) -> dict:
    """Send every scheduled row whose time has come. Testable with any session."""
    now = now or datetime.now(timezone.utc)

    due = list(
        (
            await db.execute(
                select(EmailSend)
                .where(
                    EmailSend.status == "scheduled",
                    EmailSend.scheduled_at.isnot(None),
                    EmailSend.scheduled_at <= now,
                )
                # ``scheduled_at`` alone ties for a batch scheduled together —
                # every row in it was written in one transaction. Ordering by
                # (time, id) makes repeat passes agree on which came first.
                .order_by(EmailSend.scheduled_at.asc(), EmailSend.id.asc())
                .limit(BATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )

    sent = failed = abandoned = deferred = 0
    for row in due:
        if not row.to_email or not row.body_html:
            # Rows scheduled before the compose endpoint persisted the rendered
            # copy have a subject and nothing to send. The merge context that
            # produced the body is long gone, so there is no honest way to
            # recover them — mark them rather than leave them re-read forever by
            # every future pass.
            if not await _claim(db, row.id):
                continue
            row.status = "error"
            row.error = "scheduled before send support existed — no stored body"
            await db.commit()
            abandoned += 1
            continue

        if not await _claim(db, row.id):
            continue  # another worker took it

        try:
            # The body was frozen at compose time; the files are read now. An
            # attachment removed from the template in the meantime was removed
            # because it should not go out, which includes this send.
            attachments = await template_attachments.load_for_send(db, row.template_id)
            result = await send_outreach_email(
                db,
                to_email=row.to_email,
                subject=row.subject or "",
                body_html=row.body_html,
                sender_account_id=row.sender_account_id,
                attachments=attachments,
            )
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the batch
            logger.exception("Scheduled send %s failed", row.id)
            await db.rollback()
            row.status = "error"
            row.error = str(exc)[:500]
            await db.commit()
            failed += 1
            continue

        status = result.get("status", "error")

        # A hold is not an outcome. The claim above moved this row out of
        # ``scheduled``, and writing the dispatcher's ``halted``/``skipped``
        # straight onto it made that move permanent: nothing selects a row in
        # any of those statuses, so a message whose minute arrived while the
        # day's cap was spent — or while the kill switch was engaged, or while
        # its mailbox was inside its warm-up ceiling — was simply deleted, in a
        # status the operator has to go looking for, having been told at compose
        # time that it was scheduled. The sequence engine has always treated
        # exactly these statuses as a soft skip and deferred the enrollment;
        # this sweep is the same situation with a different table, and the fix
        # is the same. Put it back in the queue, a backoff later so a
        # long-running hold neither re-asks every pass nor starves the newer
        # rows behind it in ``BATCH_LIMIT``.
        if _is_transient_hold(result):
            row.status = "scheduled"
            row.scheduled_at = now + HOLD_BACKOFF
            # Kept, not cleared: "why is this still queued" is the question the
            # operator will ask, and the dispatcher's reason is the answer.
            row.error = result.get("reason") or result.get("detail")
            await db.commit()
            deferred += 1
            logger.info(
                "Scheduled send %s held (%s); retrying after %s",
                row.id, status, row.scheduled_at.isoformat(),
            )
            continue

        row.status = status
        row.error = None if status == "sent" else (
            result.get("reason") or result.get("detail")
        )
        # The dispatcher stamps the send time; the row's own ``sent_at`` was set
        # to the moment it was *composed*, which for a send scheduled a week out
        # is a week early and lands the send in the wrong reporting window.
        row.sent_at = datetime.now(timezone.utc)
        if result.get("provider_message_id") or result.get("message_id"):
            row.provider_message_id = (
                result.get("provider_message_id") or result.get("message_id")
            )
        if result.get("from_email"):
            row.from_email = result["from_email"]
        await db.commit()
        if status == "sent":
            sent += 1
        else:
            failed += 1

    if due:
        logger.info(
            "Scheduled sends: %d due, %d sent, %d not sent, %d held, %d abandoned",
            len(due), sent, failed, deferred, abandoned,
        )
    return {
        "due": len(due), "sent": sent, "failed": failed,
        "deferred": deferred, "abandoned": abandoned,
    }


@celery_app.task(name="app.tasks.scheduled_sends.send_due_scheduled_emails")
def send_due_scheduled_emails():
    """Beat task: put every due "send later" message on the wire."""

    async def _run():
        return await for_each_org(
            send_due_scheduled_for_session, label="send_due_scheduled_emails"
        )

    try:
        return _run_async(_run())
    except Exception as exc:  # noqa: BLE001
        logger.error("send_due_scheduled_emails failed: %s", exc)
        return {"status": "error", "error": str(exc)}


__all__ = [
    "BATCH_LIMIT",
    "HOLD_BACKOFF",
    "send_due_scheduled_emails",
    "send_due_scheduled_for_session",
]
