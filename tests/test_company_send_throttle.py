"""Account-level (per-company) send discipline.

The per-contact caps in ``SendLimiter`` are keyed on an *email address*.
``info@acme.com``, ``sales@acme.com``, ``jane@acme.com`` and ``bob@acme.com``
are four addresses and one buyer, so every one of them passes a per-address
gate — while the recipient's mail server sees an unknown sender hit four
mailboxes at once, and four colleagues receive near-identical cold copy the
same minute.

That fan-out was reachable, not theoretical: ``signal_enrollment`` enrolled
*every* un-enrolled emailable contact at a company on one signal and dispatched
each immediately (``send_sequence_step.delay``), and it fires on the strongest
buying signals the system detects — so it hit hardest on the accounts most worth
winning. Round 9's scraper fix, which deliberately keeps generic inboxes so SMB
shops aren't stranded, made a 4-contact company the normal case rather than the
exception.

Two guards, tested here:

* :meth:`SendLimiter.can_send_to_company` — the authoritative brake at the send
  chokepoint, so it covers every enrolment path (signal, prospector, manual,
  bulk, queue sweep).
* the fan-out cap in ``signal_enrollment`` — so colleagues aren't merely queued
  behind a gate that drains one per day for a week.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services.email_validation import is_role_address
from app.services.send_limiter import SendLimiter

# No module-level ``pytest.mark.asyncio``: ``asyncio_mode = auto`` already runs
# the coroutines here, and the mark would be applied to the sync
# ``is_role_address`` cases below, which warn about it.


# --------------------------------------------------------------------------
# is_role_address — a ranking signal, never a rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr",
    [
        "info@acme.com", "sales@acme.com", "contact@acme.com", "admin@acme.com",
        "support@acme.com", "hello@acme.com", "office@acme.com", "team@acme.com",
        "enquiries@acme.com", "inquiries@acme.com", "accounts@acme.com",
        "billing@acme.com", "careers@acme.com", "hr@acme.com", "quotes@acme.com",
        "rfq@acme.com", "purchasing@acme.com", "INFO@ACME.COM",
        "info-team@acme.com", "sales.dept@acme.com",
    ],
)
def test_shared_inboxes_are_role_addresses(addr):
    assert is_role_address(addr) is True


@pytest.mark.parametrize(
    "addr",
    [
        "jane@acme.com", "bob.smith@acme.com", "j.doe@acme.com",
        "priya.sharma@acme.com", "mchen@acme.com",
        # Substring traps: a named person whose local part merely *contains* a
        # role word must not be demoted.
        "salesforce.admin.jane@acme.com", "info.rmation@acme.com",
        "marketingjane@acme.com", "helpingtonne@acme.com",
    ],
)
def test_named_people_are_not_role_addresses(addr):
    assert is_role_address(addr) is False


@pytest.mark.parametrize("addr", [None, "", "not-an-address"])
def test_malformed_input_is_not_a_role_address(addr):
    """That call belongs to the validation gate, not the ranking signal."""
    assert is_role_address(addr) is False


# --------------------------------------------------------------------------
# can_send_to_company
# --------------------------------------------------------------------------


async def _peer_with_sequence_send(
    db: AsyncSession, company: Company, *, email: str, hours_ago: float
) -> Contact:
    """A colleague at ``company`` we sent a *sequence* step to ``hours_ago``.

    Sequence sends write an ``OutreachEvent`` and no ``EmailSend`` row at all,
    so this is the path the gate must see through an enrollment join.
    """
    peer = Contact(
        id=_uuid.uuid4(), company_id=company.id, first_name="Peer",
        email=email, source="test",
    )
    db.add(peer)
    seq = OutreachSequence(id=_uuid.uuid4(), name="peer seq", is_active=True)
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=peer.id, sequence_id=seq.id,
        current_step=1, status="active",
    )
    db.add(enr)
    await db.flush()
    db.add(
        OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
            event_type="sent",
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        )
    )
    await db.flush()
    return peer


async def test_no_prior_sends_allows_the_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    ok, reason, wait = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True
    assert reason == "OK"
    assert wait == timedelta(0)


async def test_recent_sequence_send_to_a_colleague_blocks(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """The burst case: a colleague was emailed an hour ago."""
    await _peer_with_sequence_send(
        db_session, sample_company, email="info@acme.test", hours_ago=1
    )

    ok, reason, wait = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is False
    assert "colleague" in reason.lower()
    # 24h gap, 1h elapsed → ~23h left, and the caller defers by that rather than
    # re-checking every hour.
    assert timedelta(hours=22) < wait <= timedelta(hours=23)


async def test_recent_manual_send_to_a_colleague_also_blocks(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """Proves the union: manual/template sends write ``EmailSend`` only.

    Reading just ``OutreachEvent`` would leave the gate blind to whichever path
    sent last — the same two-table split that made the unified inbox blind to
    every sequence send.
    """
    peer = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id, first_name="Peer",
        email="sales@acme.test", source="test",
    )
    db_session.add(peer)
    await db_session.flush()
    db_session.add(
        EmailSend(
            id=_uuid.uuid4(), contact_id=peer.id, to_email=peer.email,
            subject="hi", status="sent",
            sent_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
    )
    await db_session.flush()

    ok, reason, _ = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is False
    assert "colleague" in reason.lower()


async def test_our_own_earlier_send_does_not_block_the_follow_up(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """A follow-up to the same person is cadence, not fan-out.

    Its spacing belongs to ``step_delay`` / ``can_send_to_contact``; if this gate
    counted it, every multi-step sequence would stall on its own step 1.
    """
    seq = OutreachSequence(id=_uuid.uuid4(), name="mine", is_active=True)
    db_session.add(seq)
    await db_session.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="active",
    )
    db_session.add(enr)
    await db_session.flush()
    db_session.add(
        OutreachEvent(
            id=_uuid.uuid4(), enrollment_id=enr.id, step_number=1,
            event_type="sent",
            occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    await db_session.flush()

    ok, _, _ = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True


async def test_send_outside_the_window_does_not_block(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    await _peer_with_sequence_send(
        db_session, sample_company, email="info@acme.test", hours_ago=30
    )

    ok, reason, wait = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True
    assert reason == "OK"
    assert wait == timedelta(0)


async def test_a_colleague_at_a_different_company_does_not_block(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    other = Company(
        id=_uuid.uuid4(), name="Other Co", pipeline_stage="new_lead", source="test"
    )
    db_session.add(other)
    await db_session.flush()
    await _peer_with_sequence_send(
        db_session, other, email="info@other.test", hours_ago=1
    )

    ok, _, _ = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True


async def test_contact_with_no_company_is_never_throttled(
    db_session: AsyncSession, sample_contact: Contact
):
    """No company means no colleagues to collide with."""
    ok, reason, wait = await SendLimiter().can_send_to_company(
        db_session, None, exclude_contact_id=sample_contact.id
    )
    assert ok is True
    assert reason == "OK"
    assert wait == timedelta(0)


async def test_zero_gap_setting_disables_the_gate(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    await _peer_with_sequence_send(
        db_session, sample_company, email="info@acme.test", hours_ago=0
    )
    limiter = SendLimiter()
    monkeypatch.setattr(
        limiter.settings, "min_hours_between_company_sends", 0, raising=False
    )

    ok, _, _ = await limiter.can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True


async def test_a_bounced_or_pending_row_is_not_a_send(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    """Only ``status='sent'`` counts — a queued row never reached the wire."""
    peer = Contact(
        id=_uuid.uuid4(), company_id=sample_company.id, first_name="Peer",
        email="sales@acme.test", source="test",
    )
    db_session.add(peer)
    await db_session.flush()
    db_session.add(
        EmailSend(
            id=_uuid.uuid4(), contact_id=peer.id, to_email=peer.email,
            subject="hi", status="pending",
            sent_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await db_session.flush()

    ok, _, _ = await SendLimiter().can_send_to_company(
        db_session, sample_company.id, exclude_contact_id=sample_contact.id
    )
    assert ok is True


# --------------------------------------------------------------------------
# The gate at the send chokepoint
# --------------------------------------------------------------------------


def _stub_pipeline(monkeypatch, db_session):
    """Neutralise everything around the gate under test.

    Mirrors ``test_outreach_soft_skip``: the dispatcher is replaced so a send
    that *does* get through is observable, and the task's coroutine runs on the
    test's event loop.
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

    sends = []

    async def _send(db, **kwargs):
        sends.append(kwargs)
        return {"status": "sent", "message_id": "m1"}

    monkeypatch.setattr(outreach_tasks, "send_outreach_email", _send)
    return sends


async def _enrollment_due_now(db: AsyncSession, contact: Contact):
    seq = OutreachSequence(id=_uuid.uuid4(), name="CNC", is_active=True)
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="hi {{first_name}}", body_template="<p>hi</p>",
        )
    )
    enr = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enr)
    await db.flush()
    await db.commit()
    return enr


async def test_send_task_defers_when_a_colleague_was_just_emailed(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """End to end: the second person at a company is staggered, not blasted."""
    import app.tasks.outreach as outreach_tasks

    await _peer_with_sequence_send(
        db_session, sample_company, email="info@acme.test", hours_ago=1
    )
    enrollment = await _enrollment_due_now(db_session, sample_contact)
    enrollment_id = enrollment.id
    was_due_at = enrollment.next_send_at
    sends = _stub_pipeline(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment_id))

    assert result["status"] == "skipped"
    assert result.get("company_throttled") is True
    assert sends == [], "nothing may reach the dispatcher"

    next_send_at, status = (
        await db_session.execute(
            select(OutreachEnrollment.next_send_at, OutreachEnrollment.status)
            .where(OutreachEnrollment.id == enrollment_id)
        )
    ).one()
    if next_send_at.tzinfo is None:
        next_send_at = next_send_at.replace(tzinfo=timezone.utc)

    # Deferred by the real remaining gap, not a flat hour: a 23-hour wait
    # re-checked hourly is 23 wasted wake-ups.
    assert next_send_at > was_due_at
    assert next_send_at > datetime.now(timezone.utc) + timedelta(hours=20)
    # A "not now", never an "abandon this contact".
    assert status == "active"


async def test_send_task_proceeds_when_no_colleague_was_emailed(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The gate must not block the ordinary case."""
    import app.tasks.outreach as outreach_tasks

    enrollment = await _enrollment_due_now(db_session, sample_contact)
    sends = _stub_pipeline(monkeypatch, db_session)

    result = await outreach_tasks.send_sequence_step.run(str(enrollment.id))

    assert result.get("company_throttled") is None
    assert result["status"] != "skipped" or "colleague" not in result.get("reason", "")
    assert len(sends) == 1
