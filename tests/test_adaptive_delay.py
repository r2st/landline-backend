"""Tests for engagement-aware adaptive step delays (Feature 3)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep
from app.services import adaptive_delay


# ─── Pure helpers ────────────────────────────────────────────────────────────


def test_effective_delay_days_scaling_and_clamp():
    assert adaptive_delay.effective_delay_days(2, 1.0) == 2       # unchanged
    assert adaptive_delay.effective_delay_days(2, 0.5) == 1       # halved
    assert adaptive_delay.effective_delay_days(1, 0.5) == 1       # floored at 1
    assert adaptive_delay.effective_delay_days(10, 1.5) == 14     # capped at 14
    assert adaptive_delay.effective_delay_days(0, 0.5) == 0       # immediate stays 0
    assert adaptive_delay.effective_delay_days(3, 0.0) == 3       # bad factor → 1.0


class _Enr:
    def __init__(self, factor=1.0):
        self.id = uuid.uuid4()
        self.adaptive_delay_factor = factor
        self.status = "active"


def test_shorten_halves_and_floors():
    e = _Enr(1.0)
    assert adaptive_delay.shorten(e) == 0.5
    assert adaptive_delay.shorten(e) == 0.25
    assert adaptive_delay.shorten(e) == 0.25  # floor at MIN_FACTOR


def test_extend_grows_and_caps():
    e = _Enr(1.0)
    assert adaptive_delay.extend(e) == 1.5
    assert adaptive_delay.extend(e) == 2.25
    assert adaptive_delay.extend(e) == 3.0  # cap at MAX_FACTOR


# ─── DB-backed reschedule ────────────────────────────────────────────────────


async def _seq_with_step(db, delay_days=4):
    seq = OutreachSequence(id=uuid.uuid4(), name="Seq", status="active")
    db.add(seq)
    await db.flush()
    db.add(SequenceStep(
        id=uuid.uuid4(), sequence_id=seq.id, step_number=2,
        delay_days=delay_days, subject_template="s", body_template="b",
    ))
    await db.flush()
    return seq


@pytest.mark.asyncio
async def test_register_engagement_pulls_in_pending_step(
    db_session: AsyncSession, sample_contact
):
    seq = await _seq_with_step(db_session, delay_days=4)
    now = datetime.now(timezone.utc)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="active",
        enrolled_at=now - timedelta(days=10),
        next_send_at=now + timedelta(days=4),
    )
    db_session.add(enrollment)
    await db_session.flush()
    # A prior send anchors the delay recompute.
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="sent", occurred_at=now,
    ))
    await db_session.commit()

    await adaptive_delay.register_engagement(db_session, enrollment)
    await db_session.commit()

    # Factor halved, and next_send_at pulled in from 4 days to 2 (4 * 0.5).
    assert enrollment.adaptive_delay_factor == 0.5
    delta_days = (enrollment.next_send_at - now).total_seconds() / 86400
    assert round(delta_days) == 2


@pytest.mark.asyncio
async def test_reschedule_noop_for_inactive_enrollment(
    db_session: AsyncSession, sample_contact
):
    seq = await _seq_with_step(db_session, delay_days=4)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="replied", adaptive_delay_factor=0.5,
    )
    db_session.add(enrollment)
    await db_session.commit()
    moved = await adaptive_delay.reschedule_pending_step(db_session, enrollment)
    assert moved is False


# ─── End-to-end through the tracking pixel ───────────────────────────────────


@pytest.mark.asyncio
async def test_open_pixel_shortens_delay(client, db_session: AsyncSession, sample_contact):
    """Hitting the open pixel for a sequence step shortens the enrollment cadence."""
    from app.routers.tracking import generate_sequence_pixel_url

    seq = await _seq_with_step(db_session, delay_days=4)
    now = datetime.now(timezone.utc)
    enrollment = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=sample_contact.id, sequence_id=seq.id,
        current_step=2, status="active", enrolled_at=now - timedelta(days=5),
        next_send_at=now + timedelta(days=4),
    )
    db_session.add(enrollment)
    await db_session.flush()
    db_session.add(OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=1,
        event_type="sent", occurred_at=now,
    ))
    await db_session.commit()

    # Build the signed open-pixel URL for step 1 and fetch it.
    pixel_url = generate_sequence_pixel_url(str(enrollment.id), 1)
    path = pixel_url.split("/api/v1", 1)[1]
    resp = await client.get(f"/api/v1{path}")
    assert resp.status_code == 200

    await db_session.refresh(enrollment)
    assert enrollment.adaptive_delay_factor == 0.5
