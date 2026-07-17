"""Reply → step attribution.

``send_sequence_step`` stamps its ``sent`` event with ``current_step`` and then
increments the counter, so by the time a reply lands ``current_step`` has already
moved on. Stamping the reply with ``current_step`` therefore filed it against a
step that was never sent — one step late, every time.

That is invisible on a timeline but fatal to anything joining sends to outcomes
on the step number, which is exactly what ``compute_variant_stats`` does. With
replies landing on step N+1 and sends on step N, every A/B variant's reply count
was 0 — while ``AbTest.metric`` defaults to ``"replied"``.
"""

import uuid

import pytest

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.outreach_events import build_reply_event, last_sent_step


async def _enrollment_with_sends(db, contact, sent_steps: list[int], current_step: int):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add(seq)
    await db.flush()
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=current_step,
    )
    db.add(enrollment)
    await db.flush()
    for n in sent_steps:
        db.add(
            SequenceStep(
                id=uuid.uuid4(), sequence_id=seq.id, step_number=n, delay_days=0,
                subject_template="s", body_template="b",
            )
        )
        db.add(
            OutreachEvent(
                id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=n,
                event_type="sent", event_metadata={"tracked": True},
            )
        )
    await db.flush()
    return seq, enrollment


@pytest.mark.asyncio
async def test_last_sent_step_returns_the_step_actually_sent(db_session, sample_contact):
    # Step 1 sent; the send path then advanced current_step to 2.
    _, enrollment = await _enrollment_with_sends(db_session, sample_contact, [1], current_step=2)
    assert await last_sent_step(db_session, enrollment) == 1


@pytest.mark.asyncio
async def test_reply_is_attributed_to_the_sent_step_not_current_step(
    db_session, sample_contact
):
    """The regression: a reply to step 1 must not be filed against step 2."""
    _, enrollment = await _enrollment_with_sends(db_session, sample_contact, [1], current_step=2)

    event = await build_reply_event(db_session, enrollment, metadata={"from_email": "a@b.com"})

    assert event.step_number == 1, "reply must land on the step that was sent"
    assert event.step_number != enrollment.current_step
    assert event.event_type == "replied"


@pytest.mark.asyncio
async def test_reply_attributes_to_most_recent_send_in_multi_step(
    db_session, sample_contact
):
    _, enrollment = await _enrollment_with_sends(
        db_session, sample_contact, [1, 2, 3], current_step=4
    )
    event = await build_reply_event(db_session, enrollment)
    assert event.step_number == 3


@pytest.mark.asyncio
async def test_last_sent_step_falls_back_without_send_events(db_session, sample_contact):
    """Legacy enrollments with no logged send: reverse the off-by-one, not repeat it."""
    _, enrollment = await _enrollment_with_sends(db_session, sample_contact, [], current_step=3)
    assert await last_sent_step(db_session, enrollment) == 2


@pytest.mark.asyncio
async def test_last_sent_step_never_returns_below_one(db_session, sample_contact):
    _, enrollment = await _enrollment_with_sends(db_session, sample_contact, [], current_step=1)
    assert await last_sent_step(db_session, enrollment) == 1


@pytest.mark.asyncio
async def test_variant_reply_stats_see_the_reply(db_session, sample_contact):
    """End-to-end: the A/B engine's reply_rate must be non-zero after a reply.

    This is the assertion that would have caught the original bug — the bandit
    optimizes on reply_rate by default, so a structurally-zero column meant it
    was choosing between variants on noise.
    """
    from sqlalchemy import select

    from app.models.ab_test import AbTestAssignment, AbTestVariant
    from app.services.ab_test_resolver import compute_variant_stats

    seq, enrollment = await _enrollment_with_sends(
        db_session, sample_contact, [1], current_step=2
    )
    step = (await db_session.execute(
        select(SequenceStep).where(
            SequenceStep.sequence_id == seq.id, SequenceStep.step_number == 1
        )
    )).scalar_one()

    variant = AbTestVariant(
        id=uuid.uuid4(), sequence_step_id=step.id, label="A",
        subject_template="s", body_template="b", is_control=True, is_active=True,
    )
    db_session.add(variant)
    await db_session.flush()
    db_session.add(
        AbTestAssignment(
            id=uuid.uuid4(), sequence_step_id=step.id, variant_id=variant.id,
            enrollment_id=enrollment.id,
        )
    )
    await db_session.flush()

    # Record the reply the way the reply path now does.
    db_session.add(await build_reply_event(db_session, enrollment))
    await db_session.flush()

    stats = await compute_variant_stats(db_session, step, [variant], "replied")
    assert stats[0]["sent"] == 1
    assert stats[0]["replied"] == 1, "reply must attribute to the variant's step"
    assert stats[0]["reply_rate"] == 1.0
