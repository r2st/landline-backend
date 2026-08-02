"""Send pacing where it actually runs: inside ``send_sequence_step``.

``test_send_pacing.py`` tests the reservation primitive. This tests the wiring,
which is where the risk is — the gate sits on the live send path between the
spam scan and the daily-cap reservation, and it holds a resource that has to be
handed back on every path that claims it and then doesn't send. Two failure
modes, both quiet:

* **Leaked slot** — a mailbox that declined to send still burns its spacing
  window, so one flapping SMTP host enforces a minute of silence on every
  enrollment queued behind it. Outreach slows to a crawl and nothing logs an
  error.
* **Unpaced path** — pacing that resolves the sender *after* claiming means a
  first touch (no pinned ``sender_account_id``) is paced against ``None`` and
  therefore not paced at all. That is the highest-volume path in the system, so
  the gate would be absent exactly where bursts are worst.

The bounce case is the one asymmetry worth pinning: a soft skip returns the
slot, a bounce keeps it. A bounce means we opened a connection and a server
rejected us, which is the moment to keep slowing down.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import send_pacing

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_pacing_state():
    send_pacing._reset_for_tests()
    yield
    send_pacing._reset_for_tests()


@pytest.fixture
def pacing_on(monkeypatch):
    """Pacing enabled, no jitter, per-process path — deterministic gaps."""
    settings = get_settings()
    monkeypatch.setattr(settings, "send_pacing_enabled", True)
    monkeypatch.setattr(settings, "send_min_gap_seconds", 60)
    monkeypatch.setattr(settings, "send_gap_jitter_seconds", 0)
    monkeypatch.setattr(send_pacing, "_redis_disabled", True)
    return settings


async def _enrollment_due_now(db, company: Company, contact: Contact, *, sender_id=None):
    """An active enrollment on step 1, due now, optionally pinned to a mailbox."""
    seq = OutreachSequence(id=_uuid.uuid4(), name="Pacing probe", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="{{first_name}}?", body_template="<p>Hi {{first_name}},</p>",
        )
    )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        sender_account_id=sender_id,
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return enrollment


def _stub_pipeline(monkeypatch, db_session, send_result, *, reserve=(True, "ok")):
    """Everything between "enrollment is due" and the dispatcher, neutralised.

    The pacing gate itself is left alone — it is the code under test.
    ``send_result`` may be a dict or a callable raising, to drive the failure
    paths. Returns the list of dispatcher kwargs, so a test can assert both that
    a send happened and which mailbox it went out from.
    """
    import app.tasks.outreach as outreach_tasks
    from app.services import ai_compose

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _compose(db, enrollment, contact, company, step, subject, body):
        return ai_compose.ComposeResult("send", subject, body, reason="static template")

    monkeypatch.setattr(ai_compose, "compose_step", _compose)

    class _Optimizer:
        async def decide(self, *a, **k):
            return {"action": "send"}

    monkeypatch.setattr(outreach_tasks, "get_sequence_optimizer", lambda: _Optimizer())
    monkeypatch.setattr(outreach_tasks, "record_decision", lambda *a, **k: None)

    # The daily cap is a separate rail with its own tests; hold it open (or
    # closed, per ``reserve``) so what's observed here is pacing alone.
    released = []

    class _Limiter:
        async def can_send_today(self, db):
            # The advisory pre-check, upstream of pacing. Held open so these
            # tests never stop short of the gate they exist to exercise.
            return True, "ok"

        async def can_send_to_company(self, db, company_id, **kwargs):
            # Account-level burst control, also upstream of pacing. Held open
            # for the same reason. Covered by test_company_send_throttle.
            from datetime import timedelta

            return True, "OK", timedelta(0)

        async def reserve_daily_send(self, db):
            return reserve

        async def release_daily_send(self):
            released.append(True)

    monkeypatch.setattr(outreach_tasks, "get_send_limiter", lambda: _Limiter())

    calls = []

    async def _send(db, **kwargs):
        calls.append(kwargs)
        if callable(send_result):
            return send_result()
        return dict(send_result)

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return calls


def _pin_sender(monkeypatch, account_id):
    """Make sender rotation resolve to ``account_id`` without a real mailbox."""
    from app.services import email_dispatch

    class _Info:
        def __init__(self, aid):
            self.account_id = aid

    async def _resolve(db, sender_account_id=None):
        return _Info(account_id)

    monkeypatch.setattr(email_dispatch, "resolve_sender_info", _resolve)


async def test_a_paced_mailbox_defers_instead_of_sending(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """The second send inside the gap is deferred, and never reaches the wire."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    enrollment_id = enrollment.id
    was_due_at = enrollment.next_send_at
    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    # Burn the mailbox's slot, as a send moments earlier would have.
    assert (await send_pacing.claim_send_slot(account_id)).allowed is True

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert result.get("paced") is True
    assert calls == [], "a paced enrollment must not reach the dispatcher"

    # Deferred past the window, not dropped — and still active, because pacing
    # is a "not yet", never an "abandon this contact".
    next_send_at, status = (
        await db_session.execute(
            select(OutreachEnrollment.next_send_at, OutreachEnrollment.status)
            .where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()
    if next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)
    assert next_send_at > was_due_at
    assert next_send_at > datetime.now(timezone.utc)
    assert status == "active"


async def test_an_unpaced_mailbox_sends_and_then_holds_the_slot(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """A free mailbox sends, and the send closes its window behind it."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "sent"
    assert len(calls) == 1
    # The slot the send claimed is still held, so the next one waits.
    assert (await send_pacing.claim_send_slot(account_id)).allowed is False


async def test_first_touch_resolves_its_sender_so_it_is_actually_paced(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """No pinned mailbox → resolve first, then pace, then send from that mailbox.

    A cold first touch carries no ``sender_account_id``; rotation picks one at
    dispatch. Pacing before resolving would key the gate on ``None``, which
    ``claim_send_slot`` lets straight through — leaving the highest-volume path
    in the system unpaced. Resolving first also pins the send, so the mailbox
    that was paced is the mailbox the message leaves from.
    """
    import app.tasks.outreach as outreach_tasks

    rotated_to = _uuid.uuid4()
    _pin_sender(monkeypatch, rotated_to)
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=None
    )
    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "sent"
    # The reservation and the send agree on the mailbox.
    assert calls[0]["sender_account_id"] == rotated_to
    # And the rotated-to mailbox really was paced, not waved through as None.
    assert (await send_pacing.claim_send_slot(rotated_to)).allowed is False


async def test_daily_cap_refusal_hands_the_slot_back(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """A cap the send never touched must not cost the mailbox its window."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    calls = _stub_pipeline(
        monkeypatch, db_session, {"status": "sent"},
        reserve=(False, "daily send limit reached (100/100)"),
    )

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result["status"] == "skipped"
    assert result.get("daily_limit_reached") is True
    assert calls == []
    assert (await send_pacing.claim_send_slot(account_id)).allowed is True


async def test_dispatcher_exception_hands_the_slot_back(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """A transient SMTP blow-up must not silence the queue behind it."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )

    def _boom():
        raise RuntimeError("smtp host went away")

    _stub_pipeline(monkeypatch, db_session, _boom)

    with pytest.raises(Exception):
        await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert (await send_pacing.claim_send_slot(account_id)).allowed is True


# The soft-skip/bounce asymmetry takes two tests rather than one: the task
# rolls back internally, which expires every instance in the shared session, so
# a second run in the same test would lazy-load an expired attribute and raise
# MissingGreenlet — the failure mode ``test_outreach_soft_skip`` exists to keep
# out of the send path in the first place.


async def test_soft_skip_hands_the_slot_back(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """Declined before the wire: the dispatcher never opened a connection, so
    the mailbox owes nothing and the next enrollment shouldn't wait on it."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    _stub_pipeline(
        monkeypatch, db_session,
        {"status": "skipped", "reason": "warm-up daily limit reached (day 1: 5/5)"},
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert (await send_pacing.claim_send_slot(account_id)).allowed is True


async def test_a_bounce_keeps_the_brakes_on(
    db_session, sample_company, sample_contact, monkeypatch, pacing_on
):
    """Rejected *at* the wire: a receiving server pushed back, which is the
    moment to keep slowing down, not to let the next message follow at once."""
    import app.tasks.outreach as outreach_tasks

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    _stub_pipeline(
        monkeypatch, db_session, {"status": "bounced", "reason": "550 no such user"},
    )

    await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert (await send_pacing.claim_send_slot(account_id)).allowed is False


async def test_pacing_disabled_leaves_the_send_path_untouched(
    db_session, sample_company, sample_contact, monkeypatch
):
    """The gate is a feature flag, and off means genuinely absent.

    Back-to-back sends from one mailbox both go out — the pre-pacing behaviour,
    so the flag is a real rollback if pacing ever misbehaves in production.
    """
    import app.tasks.outreach as outreach_tasks

    settings = get_settings()
    monkeypatch.setattr(settings, "send_pacing_enabled", False)
    monkeypatch.setattr(send_pacing, "_redis_disabled", True)

    account_id = _uuid.uuid4()
    enrollment = await _enrollment_due_now(
        db_session, sample_company, sample_contact, sender_id=account_id
    )
    calls = _stub_pipeline(monkeypatch, db_session, {"status": "sent", "message_id": "m1"})

    assert (await outreach_tasks.send_sequence_step.run(str(enrollment.id)))["status"] == "sent"
    assert len(calls) == 1
    assert (await send_pacing.claim_send_slot(account_id)).allowed is True
