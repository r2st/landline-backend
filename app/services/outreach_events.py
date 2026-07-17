"""Shared helpers for writing ``OutreachEvent`` rows correctly.

Exists because of one subtle, load-bearing detail: **which step number a reply
belongs to.**

``send_sequence_step`` stamps its ``sent`` event with ``enrollment.current_step``
and *then* increments the counter. So a step-1 send logs ``step_number=1`` and
leaves ``current_step`` at 2. A reply arriving afterwards is a reply *to step 1*,
but the naive ``step_number=enrollment.current_step`` stamps it as step 2.

That mis-attribution is invisible in a timeline view and fatal to anything that
joins sends to outcomes on the step number. ``ab_test_resolver.compute_variant_stats``
does exactly that (``OutreachEvent.step_number == step.step_number``), so with
replies landing one step late every variant's ``replied`` count was 0 — while
``AbTest.metric`` defaults to ``"replied"``. The bandit and the significance test
were running on an all-zero column.

``last_sent_step`` resolves the step a reply is actually answering: the most
recent step this enrollment genuinely sent.
"""

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent

logger = logging.getLogger(__name__)


async def last_sent_step(db: AsyncSession, enrollment: OutreachEnrollment) -> int:
    """The step number of the most recent ``sent`` event for this enrollment.

    This is the step a reply is responding to. Falls back to
    ``max(current_step - 1, 1)`` when no send is logged (legacy enrollments
    predating event logging), which reverses the off-by-one the old code
    introduced rather than repeating it.

    Ordered by ``step_number``, not ``occurred_at``: an enrollment only ever
    advances forward (``current_step += 1``), so the highest step sent is by
    definition the latest one. Timestamps would tie — and resolve arbitrarily —
    whenever two events share a ``server_default`` clock reading.
    """
    row = (
        await db.execute(
            select(OutreachEvent.step_number)
            .where(
                OutreachEvent.enrollment_id == enrollment.id,
                OutreachEvent.event_type == "sent",
            )
            .order_by(OutreachEvent.step_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None:
        return int(row)
    return max(int(enrollment.current_step or 1) - 1, 1)


async def build_reply_event(
    db: AsyncSession,
    enrollment: OutreachEnrollment,
    *,
    metadata: Optional[dict] = None,
) -> OutreachEvent:
    """A ``replied`` event attributed to the step that was actually sent."""
    step = await last_sent_step(db, enrollment)
    from datetime import datetime, timezone

    return OutreachEvent(
        enrollment_id=enrollment.id,
        step_number=step,
        event_type="replied",
        occurred_at=datetime.now(timezone.utc),
        event_metadata=metadata or {},
    )
