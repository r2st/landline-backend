"""Click-keyed follow-up branches, and the engagement lead scoring reads.

Two things ship together here because they share one root: the open signal is
the weakest one we have (Apple MPP pre-fetches images, Gmail proxies them) and
the click signal is close to ground truth — and both were under-used.

  * ``if_clicked_no_reply`` / ``if_not_clicked`` let a sequence branch on the
    deliberate act rather than on a mail server's behaviour.
  * ``scoring`` was counting engagement out of ``email_sends`` only, so every
    sequence open/click/reply — essentially all cold volume — scored zero.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.tasks.outreach import evaluate_step_condition


async def _sequence(db, condition, step_number=2):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add(seq)
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number, delay_days=2,
        subject_template="S", body_template="B", send_condition=condition,
    )
    db.add(step)
    await db.flush()
    return seq, step


async def _enrollment(db, seq, contact, step_number=2):
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=step_number, status="active",
    )
    db.add(enr)
    await db.flush()
    return enr


async def _event(db, enr, event_type, step=1, tracked=True):
    db.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=step, event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        event_metadata={"tracked": tracked} if event_type == "sent" else {},
    ))
    await db.flush()


# ─── if_clicked_no_reply ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clicked_no_reply_sends_after_a_click(db_session, sample_contact):
    seq, step = await _sequence(db_session, "if_clicked_no_reply")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1)
    await _event(db_session, enr, "clicked", step=1)

    should_send, reason = await evaluate_step_condition(db_session, enr, step)
    assert should_send is True
    assert "clicked" in reason


@pytest.mark.asyncio
async def test_clicked_no_reply_skips_once_they_reply(db_session, sample_contact):
    seq, step = await _sequence(db_session, "if_clicked_no_reply")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1)
    await _event(db_session, enr, "clicked", step=1)
    await _event(db_session, enr, "replied", step=1)

    should_send, reason = await evaluate_step_condition(db_session, enr, step)
    assert should_send is False
    assert "replied" in reason


@pytest.mark.asyncio
async def test_clicked_no_reply_skips_without_a_click(db_session, sample_contact):
    seq, step = await _sequence(db_session, "if_clicked_no_reply")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1, tracked=True)
    await _event(db_session, enr, "opened", step=1)  # an open is not a click

    should_send, reason = await evaluate_step_condition(db_session, enr, step)
    assert should_send is False
    assert "no click" in reason


@pytest.mark.asyncio
async def test_clicked_no_reply_fails_open_on_an_untracked_prior_send(
    db_session, sample_contact
):
    """A click can only be observed on a tracked send. Gating on a positive click
    after an untracked one is a branch that could never fire — the exact silent
    dead-end the open branch shipped with."""
    seq, step = await _sequence(db_session, "if_clicked_no_reply")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1, tracked=False)

    should_send, reason = await evaluate_step_condition(db_session, enr, step)
    assert should_send is True
    assert "unknown" in reason


@pytest.mark.asyncio
async def test_a_click_on_any_earlier_step_counts(db_session, sample_contact):
    """Interest shown at step 2 is still interest when step 4 decides how direct
    to be — unlike an open, which is about one message."""
    seq, step = await _sequence(db_session, "if_clicked_no_reply", step_number=4)
    enr = await _enrollment(db_session, seq, sample_contact, step_number=4)
    await _event(db_session, enr, "sent", step=2)
    await _event(db_session, enr, "clicked", step=2)
    await _event(db_session, enr, "sent", step=3, tracked=True)

    should_send, _ = await evaluate_step_condition(db_session, enr, step)
    assert should_send is True


# ─── if_not_clicked ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_not_clicked_sends_when_nobody_clicked(db_session, sample_contact):
    seq, step = await _sequence(db_session, "if_not_clicked")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1)
    await _event(db_session, enr, "opened", step=1)

    should_send, _ = await evaluate_step_condition(db_session, enr, step)
    assert should_send is True


@pytest.mark.asyncio
async def test_not_clicked_skips_after_a_click(db_session, sample_contact):
    seq, step = await _sequence(db_session, "if_not_clicked")
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "sent", step=1)
    await _event(db_session, enr, "clicked", step=1)

    should_send, reason = await evaluate_step_condition(db_session, enr, step)
    assert should_send is False
    assert "already clicked" in reason


@pytest.mark.asyncio
async def test_first_step_has_nothing_to_branch_on(db_session, sample_contact):
    """No prior touch: the not-clicked branch sends, the clicked branch doesn't."""
    seq, step = await _sequence(db_session, "if_not_clicked", step_number=1)
    enr = await _enrollment(db_session, seq, sample_contact, step_number=1)
    assert (await evaluate_step_condition(db_session, enr, step))[0] is True

    seq2, step2 = await _sequence(db_session, "if_clicked_no_reply", step_number=1)
    enr2 = await _enrollment(db_session, seq2, sample_contact, step_number=1)
    # Nothing was ever sent, so "prior_tracked" is False and the branch fails
    # open rather than wedging the enrollment forever.
    assert (await evaluate_step_condition(db_session, enr2, step2))[0] is True


@pytest.mark.asyncio
async def test_existing_conditions_are_unchanged(db_session, sample_contact):
    """Backwards compatibility: the open branches keep their exact behaviour."""
    seq, step = await _sequence(db_session, "always")
    enr = await _enrollment(db_session, seq, sample_contact)
    assert (await evaluate_step_condition(db_session, enr, step))[0] is True

    seq2, step2 = await _sequence(db_session, "if_not_opened")
    enr2 = await _enrollment(db_session, seq2, sample_contact)
    await _event(db_session, enr2, "sent", step=1)
    await _event(db_session, enr2, "opened", step=1)
    should_send, _ = await evaluate_step_condition(db_session, enr2, step2)
    assert should_send is False


# ─── Lead scoring reads both send tables ─────────────────────────────────────


@pytest.mark.asyncio
async def test_sequence_engagement_scores_the_company(db_session, sample_company, sample_contact):
    """The bug this fixes: a company that replied to a cold sequence email
    contributed zero engagement points to its own fit score, because the query
    only looked at ``email_sends``."""
    from app.services.scoring import _engagement_signal

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    enr = await _enrollment(db_session, seq, sample_contact)
    await _event(db_session, enr, "opened", step=1)
    await _event(db_session, enr, "clicked", step=1)
    await _event(db_session, enr, "replied", step=1)

    points, breakdown = await _engagement_signal(db_session, sample_company)
    assert points > 0
    assert breakdown["replied"] == 1
    assert breakdown["clicked"] == 1
    assert breakdown["opened"] == 1


@pytest.mark.asyncio
async def test_sequence_engagement_counts_toward_velocity(
    db_session, sample_company, sample_contact
):
    """Velocity's burst detection could not fire on the only channel that
    produces bursts."""
    from app.services.scoring import _engagement_velocity

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    enr = await _enrollment(db_session, seq, sample_contact)
    # Three engagements across two steps. One open per step, not two on one:
    # migration 117 makes ``opened``/``clicked`` unique per (enrollment, step),
    # so the burst a velocity test needs is several *steps* landing close
    # together — which is what a real burst is.
    for kind, step_no in (("opened", 1), ("opened", 2), ("clicked", 1)):
        await _event(db_session, enr, kind, step=step_no)

    count, most_recent = await _engagement_velocity(db_session, sample_company)
    assert count == 3
    assert most_recent is not None


@pytest.mark.asyncio
async def test_stale_sequence_events_are_outside_the_velocity_window(
    db_session, sample_company, sample_contact
):
    from app.services.scoring import _engagement_velocity

    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db_session.add(seq)
    await db_session.flush()
    enr = await _enrollment(db_session, seq, sample_contact)
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enr.id, step_number=1, event_type="clicked",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=3),
    ))
    await db_session.flush()

    count, _ = await _engagement_velocity(db_session, sample_company)
    assert count == 0
