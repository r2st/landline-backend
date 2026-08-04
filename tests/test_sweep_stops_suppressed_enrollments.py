"""An opt-out ends the enrollment, and the sweep is the only thing that can end it.

``process_outreach_queue`` checks the three suppression sources on every due
enrollment — the ``suppressed`` flag, the legacy ``unsubscribed`` flag, and the
org do-not-contact list — and it did the right thing about the mail: it did not
dispatch. What it did about the *enrollment* was nothing, on the stated grounds
that "send_sequence_step re-checks and stops the enrollment properly".

Skipping the dispatch is precisely what stops that task from ever running. So
nothing ever closed the row. The enrollment stayed ``active`` forever: selected,
ranked and suppression-checked again on every five-minute sweep, for every
contact who has ever opted out, and displayed as a live enrollment on a campaign
that can never send it another message. The mail stopped, which is why this was
survivable; the bookkeeping never did, which is why it accumulated — an org that
has been running sequences for a year carries a year of opt-outs in its active
queue, paying a suppression lookup each per sweep and reporting a campaign
larger than it is.

``opt_out.record`` already ends enrollments this way when it is the path taken
(``ACTIONABLE_STATUSES`` → ``unsubscribed``). The sweep needs to agree with it,
because it sees the two cases ``record`` cannot: a contact suppressed by some
other writer (a bounce, a complaint, an erasure), and an address that reached the
org suppression list without a contact row of its own — a bulk compliance import,
which is exactly the case the list exists for.

``unsubscribed``, never ``bounced``: a compliance stop is not a delivery failure
and must not move sender-reputation metrics.
"""

import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence, SequenceStep
from app.models.suppression import SuppressionEntry

pytestmark = pytest.mark.asyncio


async def _enrollment_due_now(db: AsyncSession, contact: Contact):
    """An active enrollment on step 1 of a live sequence, due five minutes ago."""
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Deal Cracker", is_active=True, status="active",
    )
    db.add(seq)
    await db.flush()
    db.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
            subject_template="{{first_name}}?", body_template="<p>Hi</p>",
        )
    )
    enrollment = OutreachEnrollment(
        id=_uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        current_step=1, status="active",
        next_send_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(enrollment)
    await db.flush()
    await db.commit()
    return seq, enrollment


def _stub_sweep(monkeypatch, db_session):
    """Run the sweep against the test session, recording what it dispatches."""
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks, "_run_async", lambda coro: coro)

    @asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(outreach_tasks, "celery_session", _session)

    async def _for_each_org(body, label=None):
        return await body(db_session)

    monkeypatch.setattr(outreach_tasks, "for_each_org", _for_each_org)

    limiter = outreach_tasks.get_send_limiter()
    monkeypatch.setattr(
        type(limiter), "within_send_window", lambda self, tz=None: (True, "open")
    )

    class _NeverDefer:
        async def should_defer(self, *a, **k):
            return False

    monkeypatch.setattr(
        outreach_tasks, "get_send_time_optimizer", lambda: _NeverDefer()
    )

    dispatched = []
    monkeypatch.setattr(
        outreach_tasks.send_sequence_step,
        "delay",
        lambda enrollment_id, *a, **k: dispatched.append(str(enrollment_id)),
    )
    return dispatched


async def test_a_suppressed_contacts_enrollment_is_ended_not_merely_skipped(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The regression: it used to stay ``active``, forever."""
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(db_session, sample_contact)
    sample_contact.suppressed = True
    await db_session.commit()

    dispatched = _stub_sweep(monkeypatch, db_session)
    result = await outreach_tasks.process_outreach_queue.run()

    assert dispatched == [], "a suppressed contact must not be dispatched"
    assert result["stopped_suppressed"] == 1
    await db_session.refresh(enrollment)
    assert enrollment.status == "unsubscribed"
    assert enrollment.completed_at is not None


async def test_the_stop_is_not_recorded_as_a_bounce(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """A compliance stop must not dent sender-reputation metrics.

    ``bounced`` is the status that says a mailbox rejected us, and it feeds the
    bounce rate the danger monitor pauses accounts on. Somebody choosing to
    leave is not a delivery failure.
    """
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(db_session, sample_contact)
    sample_contact.unsubscribed = True
    await db_session.commit()

    _stub_sweep(monkeypatch, db_session)
    await outreach_tasks.process_outreach_queue.run()

    await db_session.refresh(enrollment)
    assert enrollment.status == "unsubscribed"
    assert enrollment.status != "bounced"


async def test_the_suppression_list_ends_it_even_with_clean_contact_flags(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The case only the sweep can see.

    A bulk compliance import names addresses that may have no contact row and
    certainly does not touch the flags on the rows that do exist. The list is
    the durable half of an opt-out precisely because it outlives the row — so it
    has to be able to end an enrollment on its own.
    """
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(db_session, sample_contact)
    assert sample_contact.suppressed is not True
    db_session.add(
        SuppressionEntry(
            id=_uuid.uuid4(), email=sample_contact.email.lower(),
            reason="unsubscribe", source="import",
        )
    )
    await db_session.commit()

    dispatched = _stub_sweep(monkeypatch, db_session)
    await outreach_tasks.process_outreach_queue.run()

    assert dispatched == []
    await db_session.refresh(enrollment)
    assert enrollment.status == "unsubscribed"


async def test_a_second_sweep_has_nothing_left_to_do(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The point of ending it: the row leaves the queue and stays out.

    This is the assertion the old behaviour failed. It skipped correctly on the
    first pass and skipped identically on the ten-thousandth, because the
    enrollment it skipped was still ``active`` and still due.
    """
    import app.tasks.outreach as outreach_tasks

    await _enrollment_due_now(db_session, sample_contact)
    sample_contact.suppressed = True
    await db_session.commit()

    _stub_sweep(monkeypatch, db_session)
    first = await outreach_tasks.process_outreach_queue.run()
    second = await outreach_tasks.process_outreach_queue.run()

    assert first["stopped_suppressed"] == 1
    assert second["active_enrollments"] == 0
    assert second["stopped_suppressed"] == 0


async def test_a_clean_contact_is_untouched(
    db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
    monkeypatch,
):
    """The control. Ending enrollments is the destructive direction here."""
    import app.tasks.outreach as outreach_tasks

    _seq, enrollment = await _enrollment_due_now(db_session, sample_contact)
    dispatched = _stub_sweep(monkeypatch, db_session)

    result = await outreach_tasks.process_outreach_queue.run()

    assert dispatched == [str(enrollment.id)]
    assert result["stopped_suppressed"] == 0
    await db_session.refresh(enrollment)
    assert enrollment.status == "active"
