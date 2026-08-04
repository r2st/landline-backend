"""The per-contact frequency cap is a gate on a recipient, not on a row.

``SendLimiter.can_send_to_contact`` holds two of the deliverability floors: at
most ``max_emails_per_contact_per_month`` touches in 30 days, and at least
``min_days_between_followups`` between any two. Both counted by joining
``OutreachEvent`` through to ``Contact`` and comparing ``Contact.email`` raw.

``contacts.email`` has no unique constraint and is not normalised on write, so
one human routinely holds several rows and the casing differs between whichever
sources produced them. A raw comparison split those rows into separate sending
histories: ``john@acme.com`` and ``John@Acme.com`` each counted only their own
sends. Enrol the same person under both spellings and they receive twice the
monthly cap with no minimum gap between the two streams — from the control whose
only job is preventing that — while the reason string handed back to the operator
reports a count that looks perfectly correct.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence
from app.services.send_limiter import get_send_limiter


async def _mk_contact(db: AsyncSession, company, email: str) -> Contact:
    contact = Contact(
        id=_uuid.uuid4(),
        company_id=company.id,
        first_name="Jo",
        last_name="Rivera",
        email=email,
    )
    db.add(contact)
    await db.flush()
    return contact


async def _mk_sends(db: AsyncSession, contact, *, count: int, days_ago: int = 1) -> None:
    """``count`` recorded sequence sends to ``contact``, ``days_ago`` days back."""
    seq = OutreachSequence(
        id=_uuid.uuid4(), name=f"seq-{_uuid.uuid4().hex[:6]}", status="active"
    )
    db.add(seq)
    await db.flush()
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        contact_id=contact.id,
        sequence_id=seq.id,
        status="active",
        current_step=1,
    )
    db.add(enr)
    await db.flush()
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    for i in range(count):
        db.add(
            OutreachEvent(
                enrollment_id=enr.id,
                step_number=i + 1,
                event_type="sent",
                occurred_at=when,
            )
        )
    await db.flush()


@pytest.mark.asyncio
async def test_monthly_cap_counts_case_variant_rows_as_one_recipient(
    db_session: AsyncSession, sample_company
):
    """Sends against ``John@Acme.com`` must count against ``john@acme.com``.

    The cap is asked about the address the send path is about to use. Before the
    fix the join found only rows spelled identically, so the duplicate's history
    was invisible and the gate returned "OK" on a recipient already at the cap.
    """
    limiter = get_send_limiter()
    cap = limiter.settings.max_emails_per_contact_per_month

    other_spelling = await _mk_contact(db_session, sample_company, "John@Acme.com")
    await _mk_sends(db_session, other_spelling, count=cap, days_ago=20)

    allowed, reason = await limiter.can_send_to_contact(db_session, "john@acme.com")

    assert allowed is False, "the same human has already had a month's worth"
    assert "Monthly limit" in reason


@pytest.mark.asyncio
async def test_minimum_gap_sees_a_send_recorded_under_another_spelling(
    db_session: AsyncSession, sample_company
):
    """The gap between touches is a gap for the person, not for the row."""
    limiter = get_send_limiter()

    other_spelling = await _mk_contact(db_session, sample_company, "Dana.Kim@Example.com")
    await _mk_sends(db_session, other_spelling, count=1, days_ago=0)

    allowed, reason = await limiter.can_send_to_contact(db_session, "dana.kim@example.com")

    assert allowed is False, "we emailed this person today under the other spelling"
    assert "Too soon" in reason


@pytest.mark.asyncio
async def test_the_lookup_address_is_normalised_too(
    db_session: AsyncSession, sample_company
):
    """Callers pass whatever the contact row holds, so both sides fold."""
    limiter = get_send_limiter()

    contact = await _mk_contact(db_session, sample_company, "lee@example.com")
    await _mk_sends(db_session, contact, count=1, days_ago=0)

    allowed, _ = await limiter.can_send_to_contact(db_session, "  Lee@Example.com  ")

    assert allowed is False


@pytest.mark.asyncio
async def test_an_untouched_recipient_is_still_allowed(
    db_session: AsyncSession, sample_company
):
    """The control: folding case must widen the count, not invent one."""
    limiter = get_send_limiter()

    noisy = await _mk_contact(db_session, sample_company, "someone@elsewhere.com")
    await _mk_sends(db_session, noisy, count=20, days_ago=1)
    await _mk_contact(db_session, sample_company, "fresh@example.com")

    allowed, reason = await limiter.can_send_to_contact(db_session, "fresh@example.com")

    assert allowed is True and reason == "OK"


@pytest.mark.asyncio
async def test_sends_older_than_thirty_days_still_fall_out_of_the_window(
    db_session: AsyncSession, sample_company
):
    """The monthly cap is a rolling window and stays one."""
    limiter = get_send_limiter()
    cap = limiter.settings.max_emails_per_contact_per_month

    old = await _mk_contact(db_session, sample_company, "Past@Example.com")
    await _mk_sends(db_session, old, count=cap + 5, days_ago=45)

    allowed, _ = await limiter.can_send_to_contact(db_session, "past@example.com")

    assert allowed is True, "last quarter's sends do not spend this month's budget"
