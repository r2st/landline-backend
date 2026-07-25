"""Out-of-office replies pause the sequence, and something brings it back.

Detection was already deterministic (``keyword_prefilter`` + ``extract_return_date``).
What these tests pin is the response: a real pause rather than a future date on
a still-"active" enrollment, a resume sweep that only ever touches pauses this
code created, and guards on the parsed date so a mis-read can't shelve a contact
until 2031.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence
from app.services import out_of_office
from app.tasks.replies import route_auto_action

NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


async def _contact(db: AsyncSession) -> Contact:
    company = Company(
        id=uuid.uuid4(), name="Acme", domain="acme.com", country="US", source="test"
    )
    db.add(company)
    await db.flush()
    contact = Contact(
        id=uuid.uuid4(), company_id=company.id,
        first_name="Dana", last_name="Reyes", email="dana@acme.com",
    )
    db.add(contact)
    await db.flush()
    return contact


async def _enrollment(
    db: AsyncSession, contact: Contact, *, status: str = "active",
    pause_reason: str | None = None, next_send_at: datetime | None = None,
) -> OutreachEnrollment:
    seq = OutreachSequence(id=uuid.uuid4(), name=f"seq-{uuid.uuid4().hex[:6]}")
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id,
        status=status, current_step=2,
        pause_reason=pause_reason, next_send_at=next_send_at,
    )
    db.add(enr)
    await db.flush()
    return enr


# --- resolve_resume_at --------------------------------------------------------


def test_parsed_return_date_is_used():
    resume = out_of_office.resolve_resume_at("2026-08-03", now=NOW)
    assert resume.date() == datetime(2026, 8, 3).date()


def test_no_date_falls_back_to_the_default_hold():
    resume = out_of_office.resolve_resume_at(None, now=NOW)
    assert resume == NOW + timedelta(days=out_of_office.OOO_DEFAULT_PAUSE_DAYS)


def test_unparseable_date_falls_back():
    assert out_of_office.resolve_resume_at("next Tuesdayish", now=NOW) == (
        NOW + timedelta(days=out_of_office.OOO_DEFAULT_PAUSE_DAYS)
    )


def test_past_date_never_resumes_immediately():
    """A stale autoresponder (or a date lifted out of a signature) must not
    make the next send fire right now."""
    resume = out_of_office.resolve_resume_at("2026-01-01", now=NOW)
    assert resume > NOW
    assert resume == NOW + timedelta(days=out_of_office.OOO_DEFAULT_PAUSE_DAYS)


def test_absurdly_distant_date_is_capped():
    """A mis-parsed copyright year would otherwise retire the contact for good."""
    resume = out_of_office.resolve_resume_at("2031-01-01", now=NOW)
    assert resume == NOW + timedelta(days=out_of_office.OOO_DEFAULT_PAUSE_DAYS)


def test_naive_date_is_treated_as_utc():
    resume = out_of_office.resolve_resume_at("2026-08-03T09:00:00", now=NOW)
    assert resume.tzinfo is not None


# --- pause --------------------------------------------------------------------


async def test_active_enrollments_are_paused_not_just_deferred(db_session: AsyncSession):
    contact = await _contact(db_session)
    enr = await _enrollment(db_session, contact)

    summary = await out_of_office.pause_for_ooo(
        db_session, contact.id, return_date="2026-08-03", now=NOW
    )
    await db_session.flush()
    await db_session.refresh(enr)

    assert summary["paused"] == 1
    # The old behaviour left this "active" with a future date, which every
    # dashboard read as a live sequence.
    assert enr.status == "paused"
    assert enr.pause_reason == "out_of_office"
    assert enr.next_send_at.date() == datetime(2026, 8, 3).date()


async def test_non_active_enrollments_are_left_alone(db_session: AsyncSession):
    """An autoresponder must not revive a completed or unsubscribed sequence."""
    contact = await _contact(db_session)
    done = await _enrollment(db_session, contact, status="completed")
    gone = await _enrollment(db_session, contact, status="unsubscribed")

    summary = await out_of_office.pause_for_ooo(db_session, contact.id, now=NOW)
    await db_session.flush()
    await db_session.refresh(done)
    await db_session.refresh(gone)

    assert summary["paused"] == 0
    assert done.status == "completed"
    assert gone.status == "unsubscribed"


async def test_pause_with_no_contact_is_a_no_op(db_session: AsyncSession):
    summary = await out_of_office.pause_for_ooo(db_session, None, now=NOW)
    assert summary["paused"] == 0


# --- resume -------------------------------------------------------------------


async def test_due_pause_is_resumed(db_session: AsyncSession):
    contact = await _contact(db_session)
    enr = await _enrollment(
        db_session, contact, status="paused", pause_reason="out_of_office",
        next_send_at=NOW - timedelta(hours=1),
    )

    summary = await out_of_office.resume_due(db_session, now=NOW)
    await db_session.flush()
    await db_session.refresh(enr)

    assert summary["resumed"] == 1
    assert enr.status == "active"
    # Cleared, so a later pause-for-another-reason can't be revived on a stale
    # marker.
    assert enr.pause_reason is None


async def test_pause_not_yet_due_is_left_paused(db_session: AsyncSession):
    contact = await _contact(db_session)
    enr = await _enrollment(
        db_session, contact, status="paused", pause_reason="out_of_office",
        next_send_at=NOW + timedelta(days=3),
    )

    assert (await out_of_office.resume_due(db_session, now=NOW))["resumed"] == 0
    await db_session.refresh(enr)
    assert enr.status == "paused"


async def test_pauses_we_did_not_create_are_never_resumed(db_session: AsyncSession):
    """The whole reason ``pause_reason`` exists.

    An enrollment paused for a missing email address, a dead domain, a consent
    block, or by an operator on purpose must not be revived by a sweep that only
    knows "paused, and the date has passed".
    """
    contact = await _contact(db_session)
    manual = await _enrollment(
        db_session, contact, status="paused", pause_reason=None,
        next_send_at=NOW - timedelta(days=30),
    )

    assert (await out_of_office.resume_due(db_session, now=NOW))["resumed"] == 0
    await db_session.refresh(manual)
    assert manual.status == "paused"


async def test_contact_who_replied_meanwhile_is_not_resumed(db_session: AsyncSession):
    """They came back early and answered — don't restart the cold sequence."""
    contact = await _contact(db_session)
    enr = await _enrollment(
        db_session, contact, status="replied", pause_reason="out_of_office",
        next_send_at=NOW - timedelta(days=1),
    )

    assert (await out_of_office.resume_due(db_session, now=NOW))["resumed"] == 0
    await db_session.refresh(enr)
    assert enr.status == "replied"


async def test_pause_then_resume_round_trip(db_session: AsyncSession):
    contact = await _contact(db_session)
    enr = await _enrollment(db_session, contact)

    await out_of_office.pause_for_ooo(
        db_session, contact.id, return_date="2026-08-03", now=NOW
    )
    await db_session.flush()

    # Still away.
    assert (await out_of_office.resume_due(db_session, now=NOW))["resumed"] == 0
    # Back at their desk.
    after = datetime(2026, 8, 3, 13, 0, tzinfo=timezone.utc)
    assert (await out_of_office.resume_due(db_session, now=after))["resumed"] == 1

    await db_session.flush()
    await db_session.refresh(enr)
    assert enr.status == "active"


# --- reply routing ------------------------------------------------------------


async def test_ooo_reply_routes_into_a_pause(db_session: AsyncSession):
    contact = await _contact(db_session)
    enr = await _enrollment(db_session, contact)
    reply = EmailReply(
        id=uuid.uuid4(),
        contact_id=contact.id,
        from_email="dana@acme.com",
        subject="Automatic reply: out of the office",
        body_text="I am out of the office until August 3 with limited access to email.",
        received_at=NOW,
    )
    db_session.add(reply)
    await db_session.flush()

    action = await route_auto_action(
        db_session, reply, contact, None, "out_of_office",
        {"return_date": "2026-08-03"},
    )
    await db_session.flush()
    await db_session.refresh(enr)

    assert action == "requeued"
    assert enr.status == "paused"
    assert enr.pause_reason == "out_of_office"
    assert reply.classification_meta["ooo_paused"] == 1
    assert reply.classification_meta["requeued_until"].startswith("2026-08-03")
