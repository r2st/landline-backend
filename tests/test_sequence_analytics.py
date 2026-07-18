"""Tests for per-sequence analytics (Feature 4)."""

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.models.sequence import OutreachSequence, SequenceStep


async def _sequence_with_steps(db: AsyncSession) -> OutreachSequence:
    seq = OutreachSequence(id=uuid.uuid4(), name="Metal Stamping v2", status="active")
    db.add(seq)
    await db.flush()
    db.add_all([
        SequenceStep(id=uuid.uuid4(), sequence_id=seq.id, step_number=1, delay_days=0,
                     subject_template="Intro to Apprend", body_template="b", channel="email"),
        SequenceStep(id=uuid.uuid4(), sequence_id=seq.id, step_number=2, delay_days=3,
                     subject_template="Following up", body_template="b", channel="email"),
    ])
    await db.flush()
    return seq


async def _enroll(db, seq, contact, status="active"):
    e = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact.id, sequence_id=seq.id, status=status,
    )
    db.add(e)
    await db.flush()
    return e


def _event(enrollment, step, etype):
    return OutreachEvent(
        id=uuid.uuid4(), enrollment_id=enrollment.id, step_number=step,
        event_type=etype, occurred_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_analytics_404_for_unknown_sequence(client: AsyncClient):
    resp = await client.get(f"/api/v1/dialtone/sequences/{uuid.uuid4()}/analytics")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_analytics_per_step_rates_and_conversion(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company, sample_contact: Contact,
):
    seq = await _sequence_with_steps(db_session)

    # Two enrollments. e1 opens step 1 and replies; e2 just gets step 1 sent.
    e1 = await _enroll(db_session, seq, sample_contact, status="replied")
    contact2 = Contact(
        id=uuid.uuid4(), company_id=sample_company.id, first_name="Ann", last_name="Lee",
        email="ann@acmestamp.com", source="test",
    )
    db_session.add(contact2)
    await db_session.flush()
    e2 = await _enroll(db_session, seq, contact2, status="active")

    db_session.add_all([
        _event(e1, 1, "sent"), _event(e1, 1, "opened"), _event(e1, 1, "replied"),
        _event(e2, 1, "sent"),
    ])
    # A deal attributed to this sequence → conversion.
    db_session.add(Deal(
        id=uuid.uuid4(), company_id=sample_company.id, title="Won deal", value=40000,
        source_sequence_id=seq.id,
    ))
    await db_session.commit()

    resp = await client.get(f"/api/v1/dialtone/sequences/{seq.id}/analytics")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["name"] == "Metal Stamping v2"
    assert data["total_enrollments"] == 2
    assert data["replied_enrollments"] == 1
    assert data["active_enrollments"] == 1
    assert data["deals_attributed"] == 1
    assert data["conversion_rate"] == 0.5  # 1 deal / 2 enrollments

    steps = {s["step_number"]: s for s in data["steps"]}
    # Step 1: 2 sent, 1 opened, 1 replied.
    assert steps[1]["sent"] == 2
    assert steps[1]["opened"] == 1
    assert steps[1]["replied"] == 1
    assert steps[1]["open_rate"] == 0.5
    assert steps[1]["reply_rate"] == 0.5
    assert steps[1]["label"] == "Intro to Apprend"
    # Step 2: no activity → all zero, no divide-by-zero.
    assert steps[2]["sent"] == 0
    assert steps[2]["open_rate"] == 0.0
    assert steps[2]["delay_days"] == 3


@pytest.mark.asyncio
async def test_analytics_empty_sequence(client: AsyncClient, db_session: AsyncSession):
    seq = await _sequence_with_steps(db_session)
    await db_session.commit()
    resp = await client.get(f"/api/v1/dialtone/sequences/{seq.id}/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_enrollments"] == 0
    assert data["conversion_rate"] == 0.0
    assert len(data["steps"]) == 2
    assert all(s["sent"] == 0 for s in data["steps"])
