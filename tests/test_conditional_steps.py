"""Tests for declarative conditional branching on sequence steps."""

import uuid

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.tasks.outreach import evaluate_step_condition


async def _setup(db, send_condition="always", step_number=2):
    company = Company(id=uuid.uuid4(), name="Acme", source="test", country="US")
    db.add(company)
    await db.flush()
    contact = Contact(id=uuid.uuid4(), company_id=company.id, email="d@acme.com", source="test")
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq")
    db.add_all([contact, seq])
    await db.flush()
    step = SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=step_number,
        delay_days=2, subject_template="Hi", body_template="Body",
        send_condition=send_condition,
    )
    enr = OutreachEnrollment(id=uuid.uuid4(), contact_id=contact.id,
                             sequence_id=seq.id, current_step=step_number, status="active")
    db.add_all([step, enr])
    await db.flush()
    return enr, step


async def _event(db, enr, step_number, event_type, metadata=None):
    db.add(OutreachEvent(id=uuid.uuid4(), enrollment_id=enr.id,
                         step_number=step_number, event_type=event_type,
                         event_metadata=metadata or {}))
    await db.flush()


# A ``sent`` event carrying an open pixel. Steps gated on a positive open signal
# can only reach a real verdict when the prior send was actually instrumented —
# an untracked send means "open state unknown", not "not opened".
_TRACKED = {"tracked": True}


async def test_always_sends(db_session):
    enr, step = await _setup(db_session, "always")
    should, _ = await evaluate_step_condition(db_session, enr, step)
    assert should is True


async def test_if_not_opened_sends_when_not_opened(db_session):
    enr, step = await _setup(db_session, "if_not_opened")
    await _event(db_session, enr, 1, "sent")  # sent but not opened
    should, _ = await evaluate_step_condition(db_session, enr, step)
    assert should is True


async def test_if_not_opened_skips_when_opened(db_session):
    enr, step = await _setup(db_session, "if_not_opened")
    await _event(db_session, enr, 1, "opened")
    should, reason = await evaluate_step_condition(db_session, enr, step)
    assert should is False
    assert "opened" in reason


async def test_if_opened_no_reply_sends_when_opened_and_no_reply(db_session):
    enr, step = await _setup(db_session, "if_opened_no_reply")
    await _event(db_session, enr, 1, "opened")
    should, _ = await evaluate_step_condition(db_session, enr, step)
    assert should is True


async def test_if_opened_no_reply_skips_when_replied(db_session):
    enr, step = await _setup(db_session, "if_opened_no_reply")
    await _event(db_session, enr, 1, "opened")
    await _event(db_session, enr, 1, "replied")
    should, reason = await evaluate_step_condition(db_session, enr, step)
    assert should is False
    assert "replied" in reason


async def test_if_opened_no_reply_skips_when_not_opened(db_session):
    # Tracked send + no open recorded = a genuine negative, so skipping is right.
    enr, step = await _setup(db_session, "if_opened_no_reply")
    await _event(db_session, enr, 1, "sent", _TRACKED)
    should, reason = await evaluate_step_condition(db_session, enr, step)
    assert should is False
    assert "not opened" in reason


async def test_if_opened_no_reply_sends_when_prior_send_was_untracked(db_session):
    # Untracked send: no open could ever have been recorded, so gating on a
    # positive open signal would strand this step permanently. Send instead.
    enr, step = await _setup(db_session, "if_opened_no_reply")
    await _event(db_session, enr, 1, "sent")  # no pixel — open state unknown
    should, reason = await evaluate_step_condition(db_session, enr, step)
    assert should is True
    assert "without open tracking" in reason


async def test_click_counts_as_opened(db_session):
    # A click implies an open even if no explicit 'opened' event was recorded.
    enr, step = await _setup(db_session, "if_opened_no_reply")
    await _event(db_session, enr, 1, "clicked")
    should, _ = await evaluate_step_condition(db_session, enr, step)
    assert should is True


async def test_default_send_condition_is_always(db_session):
    # A step created without send_condition defaults to 'always'.
    enr, step = await _setup(db_session, step_number=1)
    assert step.send_condition == "always"
