"""Tests for sequence attribution — linking won deals back to the outreach
sequence that produced them (deals.source_sequence_id + /deals/attribution)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.outreach import OutreachEnrollment
from app.models.sequence import OutreachSequence


async def _seq(db: AsyncSession, name: str) -> OutreachSequence:
    seq = OutreachSequence(id=uuid.uuid4(), name=name, status="active")
    db.add(seq)
    await db.flush()
    return seq


async def _enroll(db: AsyncSession, contact_id, sequence_id) -> OutreachEnrollment:
    enr = OutreachEnrollment(
        id=uuid.uuid4(), contact_id=contact_id, sequence_id=sequence_id, status="active"
    )
    db.add(enr)
    await db.flush()
    return enr


async def _make_deal(client, company_id, **kw):
    payload = {"company_id": str(company_id), "title": "Annual contract"}
    payload.update(kw)
    return await client.post("/api/v1/deals", json=payload)


@pytest.mark.asyncio
async def test_deal_auto_attributes_to_earliest_enrollment(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    seq = await _seq(db_session, "Injection Molding v1")
    await _enroll(db_session, sample_contact.id, seq.id)
    await db_session.commit()

    resp = await _make_deal(client, sample_company.id, value=50000, status="won")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["source_sequence_id"] == str(seq.id)
    assert data["source_enrollment_id"] is not None


@pytest.mark.asyncio
async def test_deal_without_enrollment_is_unattributed(
    client: AsyncClient, sample_company: Company
):
    resp = await _make_deal(client, sample_company.id, value=1000, status="won")
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_sequence_id"] is None


@pytest.mark.asyncio
async def test_explicit_sequence_attribution_is_respected(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company, sample_contact: Contact
):
    # A different sequence is enrolled, but the caller pins another one.
    enrolled_seq = await _seq(db_session, "Enrolled seq")
    pinned_seq = await _seq(db_session, "Pinned seq")
    await _enroll(db_session, sample_contact.id, enrolled_seq.id)
    await db_session.commit()

    resp = await _make_deal(
        client, sample_company.id, value=2000, source_sequence_id=str(pinned_seq.id)
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["source_sequence_id"] == str(pinned_seq.id)


@pytest.mark.asyncio
async def test_attribution_summary_rolls_up_revenue(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_company: Company,
    sample_contact: Contact,
):
    seq = await _seq(db_session, "Revenue seq")
    await _enroll(db_session, sample_contact.id, seq.id)
    await db_session.commit()

    await _make_deal(client, sample_company.id, value=10000, status="won")
    await _make_deal(client, sample_company.id, value=5000, status="won")
    await _make_deal(client, sample_company.id, value=3000, status="pending")
    await _make_deal(client, sample_company.id, value=1000, status="lost")

    resp = await client.get("/api/v1/deals/attribution")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    rows = {r["sequence_id"]: r for r in data["by_sequence"]}
    assert str(seq.id) in rows
    row = rows[str(seq.id)]
    assert row["sequence_name"] == "Revenue seq"
    assert row["won_deals"] == 2
    assert row["won_revenue"] == 15000
    assert row["pending_deals"] == 1
    assert row["pipeline_value"] == 3000
    assert row["lost_deals"] == 1
    assert row["enrollments"] == 1

    assert data["total_attributed_won_revenue"] == 15000
    assert data["attributed_won_deals"] == 2
    assert data["unattributed_won_deals"] == 0


@pytest.mark.asyncio
async def test_attribution_summary_has_unattributed_bucket(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    # A company that was never enrolled → deal lands in the unattributed bucket.
    await _make_deal(client, sample_company.id, value=7000, status="won")

    resp = await client.get("/api/v1/deals/attribution")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    unattributed = [r for r in data["by_sequence"] if r["sequence_id"] is None]
    assert len(unattributed) == 1
    assert unattributed[0]["won_revenue"] == 7000
    assert unattributed[0]["enrollments"] is None
    assert data["unattributed_won_deals"] == 1
    # Unattributed revenue counts toward total but not attributed total.
    assert data["total_won_revenue"] == 7000
    assert data["total_attributed_won_revenue"] == 0
