"""Erasure is a do-not-contact decision, so it ends the cadence like the others.

Five paths take an address out of play and all of them close the subject's live
enrollments in ``opt_out.ACTIONABLE_STATUSES``: the unsubscribe link, a reply
saying "unsubscribe", a spam complaint, a hard bounce. The sixth — erasure, the
strongest of the six — set the contact flags, scrubbed every column, wrote the
suppression entry, and left the enrollments running.

**Suppressing the contact is what made that survivable rather than a leak.** The
queue sweep stops an ``active`` enrollment whose contact is suppressed, the next
time that enrollment comes due; ``send_sequence_step`` refuses at the gate
before that. So no mail went out. Two things did happen:

* an enrollment parked on ``pending_review`` is not covered by either, because
  the sweep selects ``status == "active"`` and never sees it. That status is not
  a resting state — ``expire_stale_review_drafts`` un-parks it back to
  ``active``, stamps it due now and dispatches ``send_sequence_step``. So an
  erased subject's parked enrollment was revived hours *after* the erasure
  completed and put back on the send path, to be refused.
* the ``active`` ones stayed listed as live outreach until their next due date,
  which for a step-4 cadence is days — a campaign reporting live outreach to
  someone who had exercised their right to be forgotten.

``unsubscribed``, never ``bounced``: a compliance stop is not a delivery failure
and must not move sender-reputation metrics. Same reasoning as
``complaint_handler`` and the queue sweep.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import gdpr

pytestmark = pytest.mark.asyncio

_EMAIL = "forgotten@target-shop.test"


async def _subject_with_enrollments(db, *, statuses, email=_EMAIL):
    """One contact carrying an enrollment in each of ``statuses``."""
    company = Company(id=uuid.uuid4(), name="Target Shop", country="US", source="test")
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id, first_name="Priya",
        last_name="Raghunathan", email=email, source="test",
    )
    sequence = OutreachSequence(id=uuid.uuid4(), name="Q3 cold", is_active=True)
    db.add_all([contact, sequence])
    await db.flush()

    enrollments = []
    for status in statuses:
        enr = OutreachEnrollment(
            id=uuid.uuid4(), contact_id=contact.id, sequence_id=sequence.id,
            status=status, current_step=3, enrolled_at=datetime.now(timezone.utc),
        )
        db.add(enr)
        enrollments.append(enr)
    await db.flush()
    await db.commit()
    return contact, enrollments


async def test_erasure_stops_every_actionable_enrollment(db_session):
    """The regression: they were left exactly as they were."""
    _contact, (active, paused, pending) = await _subject_with_enrollments(
        db_session, statuses=("active", "paused", "pending_review")
    )

    result = await gdpr.erase_subject(db_session, _EMAIL)

    assert result["scrubbed"]["enrollments_stopped"] == 3
    for enr in (active, paused, pending):
        await db_session.refresh(enr)
        assert enr.status == "unsubscribed"
        assert enr.completed_at is not None


async def test_the_parked_enrollment_is_the_one_that_mattered(db_session):
    """``pending_review`` is not a resting state — the review sweep revives it.

    The sweep only stops ``active`` enrollments, so this is the one no other
    gate would have closed before ``expire_stale_review_drafts`` un-parked it
    and dispatched a send for a subject who had been erased.
    """
    _contact, (pending,) = await _subject_with_enrollments(
        db_session, statuses=("pending_review",)
    )

    await gdpr.erase_subject(db_session, _EMAIL)

    await db_session.refresh(pending)
    assert pending.status != "pending_review", (
        "a pending_review enrollment left by an erasure is revived by "
        "expire_stale_review_drafts and dispatched"
    )


async def test_a_finished_enrollment_keeps_the_record_of_how_it_ended(db_session):
    """``ACTIONABLE_STATUSES`` excludes the terminal ones for a reason.

    Rewriting a ``bounced`` or ``completed`` enrollment to ``unsubscribed``
    would destroy the only record of why it stopped — and a bounce moves sender
    reputation, which a compliance stop must not.
    """
    _contact, (bounced, completed) = await _subject_with_enrollments(
        db_session, statuses=("bounced", "completed")
    )

    result = await gdpr.erase_subject(db_session, _EMAIL)

    assert result["scrubbed"]["enrollments_stopped"] == 0
    await db_session.refresh(bounced)
    await db_session.refresh(completed)
    assert bounced.status == "bounced"
    assert completed.status == "completed"


async def test_the_stop_stays_idempotent(db_session):
    """A re-run reports zero new stops rather than re-closing what it closed.

    Worth pinning for the reason the rest of ``erase_subject`` is: a count that
    grows on every re-run is how an operator concludes an erasure did not take.

    Note *why* the second pass is a no-op, because it bounds what this fix can
    do: erasure overwrites ``contact.email`` with the
    ``anonymized+<id>@redacted.invalid`` placeholder, so
    ``_contacts_for_email`` finds nothing the second time and the whole function
    is a no-op — not just the enrollment stop. Which is the argument for
    stopping the enrollments on the *first* pass and not leaving them to a
    later sweep: after erasure there is no address left to run erasure against.
    """
    _contact, (active,) = await _subject_with_enrollments(
        db_session, statuses=("active",)
    )

    first = await gdpr.erase_subject(db_session, _EMAIL)
    assert first["scrubbed"]["enrollments_stopped"] == 1
    assert first["anonymized_contacts"] == 1

    second = await gdpr.erase_subject(db_session, _EMAIL)
    assert second["anonymized_contacts"] == 0
    assert second["scrubbed"]["enrollments_stopped"] == 0

    await db_session.refresh(active)
    assert active.status == "unsubscribed"


async def test_the_stop_is_not_recorded_as_a_bounce(db_session):
    """A compliance stop must not dent sender-reputation metrics."""
    _contact, enrollments = await _subject_with_enrollments(
        db_session, statuses=("active", "paused")
    )

    await gdpr.erase_subject(db_session, _EMAIL)

    rows = (
        await db_session.execute(
            select(OutreachEnrollment.status).where(
                OutreachEnrollment.id.in_([e.id for e in enrollments])
            )
        )
    ).scalars().all()
    assert set(rows) == {"unsubscribed"}
    assert "bounced" not in rows
