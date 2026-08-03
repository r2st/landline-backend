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
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_template import EmailSend
from app.services.email_dispatch import send_outreach_email
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

logger = logging.getLogger(__name__)

#: How many due rows one pass will send per org. A blast scheduled for a single
#: minute should not become one unbounded Celery task; the next pass takes the
#: rest, and the daily cap governs the real ceiling either way.
BATCH_LIMIT = 100


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

    sent = failed = abandoned = 0
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
            result = await send_outreach_email(
                db,
                to_email=row.to_email,
                subject=row.subject or "",
                body_html=row.body_html,
                sender_account_id=row.sender_account_id,
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
            "Scheduled sends: %d due, %d sent, %d not sent, %d abandoned",
            len(due), sent, failed, abandoned,
        )
    return {
        "due": len(due), "sent": sent, "failed": failed, "abandoned": abandoned,
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
    "send_due_scheduled_emails",
    "send_due_scheduled_for_session",
]
