"""One DSN, two pipelines, one increment.

``bounce_ingestion`` sweeps the mailbox for delivery-failure notices and ledgers
each one in ``BounceRecord``, keyed on its Gmail message id. It does that
because ``record_bounce`` is explicitly not idempotent — it increments
``Contact.bounce_count`` — so re-processing a DSN would suppress a live contact
purely as an artifact of polling frequency. ``BounceRecord``'s own docstring
says as much.

The reply classifier reads the *same* mailbox into ``email_replies`` and routes
anything it classifies as ``bounce`` into ``record_bounce`` too — with no ledger
at all. So a DSN that landed in both pipelines was counted once by each, against
a threshold that has no idea the two sweeps overlap.

The direction that costs is soft bounces. A hard bounce suppresses on the first
one and a second count changes nothing. A soft bounce suppresses at
``soft_bounce_suppress_threshold``, so double-counting reaches the threshold in
half the real bounces and takes a deliverable contact out of play.

Both paths now dedup against the same ledger row.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bounce_record import BounceRecord
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.tasks.replies import route_auto_action

SOFT_DSN = "Delivery delayed: 4.2.2 mailbox full, will retry"


async def _contact(db: AsyncSession) -> Contact:
    company = Company(
        name=f"Acme {uuid.uuid4().hex[:6]}",
        domain=f"{uuid.uuid4().hex[:8]}.test",
        source="test",
    )
    db.add(company)
    await db.flush()
    contact = Contact(
        company_id=company.id,
        email=f"pat.{uuid.uuid4().hex[:6]}@acmestamp.test",
        first_name="Pat",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _dsn_reply(db: AsyncSession, message_id: str) -> EmailReply:
    reply = EmailReply(
        gmail_message_id=message_id,
        from_email="mailer-daemon@googlemail.test",
        subject=SOFT_DSN,
        body_text=SOFT_DSN,
        received_at=datetime.now(timezone.utc),
    )
    db.add(reply)
    await db.flush()
    return reply


@pytest.mark.asyncio
async def test_a_dsn_the_sweep_already_recorded_is_not_counted_again(
    db_session: AsyncSession,
):
    contact = await _contact(db_session)
    message_id = f"msg-{uuid.uuid4()}"

    # The bounce sweep got there first and ledgered it, exactly as it does.
    db_session.add(
        BounceRecord(
            gmail_message_id=message_id,
            recipient_email=contact.email,
            bounce_kind="soft",
            result={},
        )
    )
    await db_session.flush()
    contact.bounce_count = 1  # the sweep's own increment
    await db_session.commit()

    reply = await _dsn_reply(db_session, message_id)
    handled = await route_auto_action(db_session, reply, contact, None, "bounce", {})
    await db_session.commit()

    assert handled == "bounced"
    await db_session.refresh(contact)
    assert contact.bounce_count == 1, "the same DSN was counted by both pipelines"


@pytest.mark.asyncio
async def test_a_dsn_only_the_reply_path_saw_is_still_counted(db_session: AsyncSession):
    """Dedup must not become "never count a bounce that arrives as a reply"."""
    contact = await _contact(db_session)
    reply = await _dsn_reply(db_session, f"msg-{uuid.uuid4()}")

    handled = await route_auto_action(db_session, reply, contact, None, "bounce", {})
    await db_session.commit()

    assert handled == "bounced"
    await db_session.refresh(contact)
    assert contact.bounce_count == 1


@pytest.mark.asyncio
async def test_the_reply_path_ledgers_so_the_sweep_skips_it(db_session: AsyncSession):
    """The dedup has to work in the other order too.

    Whichever pipeline reaches the DSN first must leave the row that stops the
    other one, or the fix only covers half the race.
    """
    contact = await _contact(db_session)
    message_id = f"msg-{uuid.uuid4()}"
    reply = await _dsn_reply(db_session, message_id)

    await route_auto_action(db_session, reply, contact, None, "bounce", {})
    await db_session.commit()

    ledgered = (
        await db_session.execute(
            select(BounceRecord).where(BounceRecord.gmail_message_id == message_id)
        )
    ).scalars().all()
    assert len(ledgered) == 1
    assert ledgered[0].recipient_email == contact.email
    assert ledgered[0].result.get("source") == "reply_classification"


@pytest.mark.asyncio
async def test_replaying_the_same_reply_does_not_increment_twice(
    db_session: AsyncSession,
):
    """The end-to-end shape of the bug: one message, two passes, one count."""
    contact = await _contact(db_session)
    message_id = f"msg-{uuid.uuid4()}"
    reply = await _dsn_reply(db_session, message_id)

    await route_auto_action(db_session, reply, contact, None, "bounce", {})
    await db_session.commit()
    await route_auto_action(db_session, reply, contact, None, "bounce", {})
    await db_session.commit()

    await db_session.refresh(contact)
    assert contact.bounce_count == 1
