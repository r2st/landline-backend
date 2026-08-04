"""Two overlapping classification passes must not action one reply twice.

``classify_new_replies_async`` selected on ``reply_classification IS NULL`` and
called that its idempotency guard. It is one *within* a pass. The classification
a pass writes is invisible to anyone else until the task commits, and the task
commits once, after the whole batch — up to 200 replies, each costing an LLM
round trip, against a Beat cadence of five minutes. The original docstring
anticipated exactly that overlap and drew the wrong conclusion from it: a second
pass starting mid-batch re-selects the same uncommitted rows and runs every
auto-action again.

The auto-actions are not uniformly safe to repeat. A duplicate ``out_of_office``
pushes the return date out a second time. A duplicate ``interested`` notifies
the operator twice. A duplicate ``bounce`` calls ``record_bounce``, which is
documented as *not* idempotent — it increments ``Contact.bounce_count``, so two
passes over one soft bounce count two, and enough of that suppresses a live
contact for no reason but how the sweeps overlapped.

The batch is now claimed with a conditional UPDATE before it is worked — the
``send_claim`` mechanism, and it holds for the same reason: the row lock.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email_reply import EmailReply
from app.tasks.replies import classify_new_replies_async


class FakeAI:
    """Records every classification request so double work is visible."""

    def __init__(self, category: str = "not_interested") -> None:
        self.category = category
        self.calls: list[str] = []

    async def classify_reply(self, **kwargs):
        self.calls.append(kwargs.get("from_email") or "")
        return {"category": self.category, "confidence": 0.95, "source": "ai"}


class FakeNotify:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def notify_interested_reply(self, *args, **kwargs):
        self.sent.append((args, kwargs))

    async def send_notification(self, *args, **kwargs):
        self.sent.append((args, kwargs))


async def _reply(db: AsyncSession, *, subject: str = "no thanks") -> EmailReply:
    reply = EmailReply(
        gmail_message_id=f"msg-{uuid.uuid4()}",
        from_email="john.smith@acmestamp.test",
        subject=subject,
        body_text=subject,
        received_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    db.add(reply)
    await db.flush()
    return reply


@pytest.mark.asyncio
async def test_a_claimed_reply_is_not_reclassified(db_session: AsyncSession):
    """The second pass over the same rows must claim — and get — nothing."""
    await _reply(db_session)
    await _reply(db_session)
    await db_session.commit()

    first_ai = FakeAI()
    first = await classify_new_replies_async(
        db_session, ai_service=first_ai, notification_service=FakeNotify()
    )
    assert first["processed"] == 2
    assert len(first_ai.calls) == 2

    # Second pass, same session state a concurrent worker would see after the
    # first committed its claim.
    await db_session.commit()
    second_ai = FakeAI()
    second = await classify_new_replies_async(
        db_session, ai_service=second_ai, notification_service=FakeNotify()
    )

    assert second["processed"] == 0
    assert second_ai.calls == [], "a claimed reply was handed to the AI again"


@pytest.mark.asyncio
async def test_the_claim_stamps_classified_at(db_session: AsyncSession):
    """``classified_at`` is the marker; it must actually be written."""
    reply = await _reply(db_session)
    await db_session.commit()

    await classify_new_replies_async(
        db_session, ai_service=FakeAI(), notification_service=FakeNotify()
    )
    await db_session.commit()

    stored = (
        await db_session.execute(select(EmailReply).where(EmailReply.id == reply.id))
    ).scalars().one()
    assert stored.classified_at is not None
    assert stored.reply_classification == "not_interested"


@pytest.mark.asyncio
async def test_an_operator_correction_is_never_reclassified(db_session: AsyncSession):
    """``PATCH /replies/{id}/classify`` sets the category and no stamp.

    Claiming on the stamp alone would hand a human's correction back to the AI
    and overwrite it with a guess, so the selection requires *both* to be unset.
    """
    reply = await _reply(db_session)
    reply.reply_classification = "interested"   # operator's call, no stamp
    reply.classified_at = None
    await db_session.commit()

    ai = FakeAI("not_interested")
    report = await classify_new_replies_async(
        db_session, ai_service=ai, notification_service=FakeNotify()
    )
    await db_session.commit()

    assert report["processed"] == 0
    assert ai.calls == []
    stored = (
        await db_session.execute(select(EmailReply).where(EmailReply.id == reply.id))
    ).scalars().one()
    assert stored.reply_classification == "interested"


@pytest.mark.asyncio
async def test_a_reply_arriving_after_the_claim_is_still_picked_up(
    db_session: AsyncSession,
):
    """The claim must bound one batch, not latch the whole task off."""
    await _reply(db_session)
    await db_session.commit()
    await classify_new_replies_async(
        db_session, ai_service=FakeAI(), notification_service=FakeNotify()
    )
    await db_session.commit()

    await _reply(db_session, subject="still no")
    await db_session.commit()

    ai = FakeAI()
    report = await classify_new_replies_async(
        db_session, ai_service=ai, notification_service=FakeNotify()
    )

    assert report["processed"] == 1
    assert len(ai.calls) == 1
