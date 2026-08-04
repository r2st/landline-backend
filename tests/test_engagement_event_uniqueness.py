"""One open and one click per sequence step, enforced by the database.

`_record_outreach_engagement` deduplicates `opened`/`clicked` per
`(enrollment, step)` because mail clients re-fetch the tracking pixel on every
re-render and the send-time optimizer reads these as counts. It did it with a
SELECT followed by an INSERT, which is not a constraint — and the pixel endpoint
is public, unauthenticated and served concurrently. Two fetches landing together
both read no row and both wrote one.

The count is the visible half. The other half is that
`adaptive_delay.register_engagement` runs once per *recorded* event, so a
doubled open shortened the enrollment's cadence twice.

Migration 117 adds the partial unique index. These tests cover the constraint
itself, the losing side of the race, and that a lost race does not double the
adaptive-delay signal or break the pixel response.
"""

import uuid as _uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.org_context import APPREND_ORG_ID
from app.routers import tracking


@pytest_asyncio.fixture
async def enrollment(db_session, sample_contact):
    seq = OutreachSequence(
        id=_uuid.uuid4(), name="Uniqueness", org_id=APPREND_ORG_ID, is_active=True
    )
    db_session.add(seq)
    await db_session.flush()
    db_session.add(
        SequenceStep(
            id=_uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=3,
            subject_template="s", body_template="b", org_id=APPREND_ORG_ID,
        )
    )
    enr = OutreachEnrollment(
        id=_uuid.uuid4(),
        sequence_id=seq.id,
        contact_id=sample_contact.id,
        status="active",
        current_step=1,
        org_id=APPREND_ORG_ID,
        next_send_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    db_session.add(enr)
    await db_session.commit()
    return enr


async def _count(db_session, enrollment_id, event_type):
    return (
        await db_session.execute(
            select(func.count(OutreachEvent.id)).where(
                OutreachEvent.enrollment_id == enrollment_id,
                OutreachEvent.event_type == event_type,
            )
        )
    ).scalar()


# --- The constraint ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_database_refuses_a_second_open_for_the_same_step(
    db_session, enrollment
):
    """Written straight to the table, bypassing every application check."""
    for _ in range(2):
        db_session.add(
            OutreachEvent(
                enrollment_id=enrollment.id, step_number=1, event_type="opened",
                occurred_at=datetime.now(timezone.utc), org_id=APPREND_ORG_ID,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_open_and_click_on_the_same_step_coexist(db_session, enrollment):
    """The index keys on event_type — a click is not a duplicate of an open."""
    for kind in ("opened", "clicked"):
        db_session.add(
            OutreachEvent(
                enrollment_id=enrollment.id, step_number=1, event_type=kind,
                occurred_at=datetime.now(timezone.utc), org_id=APPREND_ORG_ID,
            )
        )
    await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_the_same_type_on_a_different_step_is_not_a_duplicate(
    db_session, enrollment
):
    for step in (1, 2):
        db_session.add(
            OutreachEvent(
                enrollment_id=enrollment.id, step_number=step, event_type="opened",
                occurred_at=datetime.now(timezone.utc), org_id=APPREND_ORG_ID,
            )
        )
    await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_repeatable_event_types_are_untouched(db_session, enrollment):
    """The index is partial on purpose.

    `sent` is written per attempt — `send_claim.reconcile` writes a second one
    for an attempt whose bookkeeping was lost — and `replied` is genuinely
    repeatable. Making those unique would break the send path, not tidy it.
    """
    for kind in ("sent", "sent", "replied", "replied"):
        db_session.add(
            OutreachEvent(
                enrollment_id=enrollment.id, step_number=1, event_type=kind,
                occurred_at=datetime.now(timezone.utc), org_id=APPREND_ORG_ID,
            )
        )
    await db_session.flush()
    assert await _count(db_session, enrollment.id, "sent") == 2
    await db_session.rollback()


# --- The application path ---------------------------------------------------


@pytest.mark.asyncio
async def test_the_ordinary_repeat_is_still_dropped_by_the_read(
    db_session, enrollment
):
    """The control: the cheap pre-check still handles the common case."""
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is True
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is False
    assert await _count(db_session, enrollment.id, "opened") == 1


@pytest.mark.asyncio
async def test_losing_the_race_returns_false_instead_of_raising(
    db_session, enrollment, monkeypatch
):
    """The path a concurrent pixel fetch takes.

    The pre-check is forced to miss, so the INSERT meets the constraint — which
    is exactly what the second of two simultaneous requests sees. It must come
    back "already recorded", not raise: the caller is a tracking pixel, and an
    exception there is a broken image in a prospect's inbox.
    """
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is True

    async def _blind(*a, **k):
        return False

    monkeypatch.setattr(tracking, "_engagement_exists", _blind)
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is False
    assert await _count(db_session, enrollment.id, "opened") == 1


@pytest.mark.asyncio
async def test_a_lost_race_does_not_register_the_engagement_twice(
    db_session, enrollment, monkeypatch
):
    """The reason the return has to come *before* the adaptive-delay call.

    `register_engagement` shortens the enrollment's cadence per recorded signal.
    A doubled open that still reached it would pull the next step in twice for
    one person opening one email once.
    """
    calls = []

    async def _spy(db, enr):
        calls.append(enr.id)

    from app.services import adaptive_delay

    monkeypatch.setattr(adaptive_delay, "register_engagement", _spy)

    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is True
    assert len(calls) == 1

    async def _blind(*a, **k):
        return False

    monkeypatch.setattr(tracking, "_engagement_exists", _blind)
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is False
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_session_survives_a_lost_race(db_session, enrollment, monkeypatch):
    """The savepoint's job: a failed INSERT must not poison the transaction.

    Without it the IntegrityError leaves the session unusable, so the *click*
    half of a click handler — which records the implied open first — would fail
    after the open collided.
    """
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is True

    original = tracking._engagement_exists
    state = {"blind": True}

    async def _blind_once(*a, **k):
        if state["blind"]:
            state["blind"] = False
            return False
        return await original(*a, **k)

    monkeypatch.setattr(tracking, "_engagement_exists", _blind_once)

    # Collides...
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "opened"
    ) is False
    # ...and the next write on the same session still lands.
    assert await tracking._record_outreach_engagement(
        db_session, str(enrollment.id), 1, "clicked"
    ) is True
    assert await _count(db_session, enrollment.id, "clicked") == 1
