"""Tests for top-level sequence listing and batch enrollment."""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.sequence import OutreachSequence


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """Stop the enroll endpoint from hitting a real broker during tests."""
    import app.tasks.outreach as outreach_tasks
    monkeypatch.setattr(outreach_tasks.send_sequence_step, "delay", lambda *a, **k: None)


async def _make_sequence(db_session: AsyncSession, name="Test Seq", active=True) -> OutreachSequence:
    seq = OutreachSequence(id=_uuid.uuid4(), name=name, is_active=active)
    db_session.add(seq)
    await db_session.commit()
    return seq


@pytest.mark.asyncio
async def test_list_sequences_with_counts(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _make_sequence(db_session)

    # Enroll one contact via the API.
    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 200, resp.text

    listing = await client.get("/api/v1/sequences")
    assert listing.status_code == 200
    rows = listing.json()
    row = next(r for r in rows if r["id"] == str(seq.id))
    assert row["enrollment_count"] == 1
    assert row["active_enrollment_count"] == 1
    assert row["step_count"] == 0


@pytest.mark.asyncio
async def test_enroll_batch(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    seq = await _make_sequence(db_session)
    # Two valid contacts.
    c1 = Contact(id=_uuid.uuid4(), company_id=sample_company.id, first_name="A",
                 email="a@x.com", source="test")
    c2 = Contact(id=_uuid.uuid4(), company_id=sample_company.id, first_name="B",
                 email="b@x.com", source="test")
    db_session.add_all([c1, c2])
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(c1.id), str(c2.id)]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrolled_count"] == 2
    assert body["skipped_count"] == 0
    assert set(body["enrolled"]) == {str(c1.id), str(c2.id)}


@pytest.mark.asyncio
async def test_enroll_skips_invalid_contacts(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    seq = await _make_sequence(db_session)
    no_email = Contact(id=_uuid.uuid4(), company_id=sample_company.id,
                       first_name="NoEmail", source="test")
    unsub = Contact(id=_uuid.uuid4(), company_id=sample_company.id, first_name="Unsub",
                    email="u@x.com", unsubscribed=True, source="test")
    db_session.add_all([no_email, unsub])
    await db_session.commit()

    missing_id = str(_uuid.uuid4())
    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(no_email.id), str(unsub.id), missing_id]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrolled_count"] == 0
    assert body["skipped_count"] == 3
    reasons = {s["contact_id"]: s["reason"] for s in body["skipped"]}
    assert "no email" in reasons[str(no_email.id)].lower()
    assert "unsubscribed" in reasons[str(unsub.id)].lower()
    assert "not found" in reasons[missing_id].lower()


@pytest.mark.asyncio
async def test_enroll_skips_already_enrolled(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _make_sequence(db_session)
    first = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert first.json()["enrolled_count"] == 1

    second = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    body = second.json()
    assert body["enrolled_count"] == 0
    assert body["skipped_count"] == 1
    assert "already enrolled" in body["skipped"][0]["reason"].lower()


@pytest.mark.asyncio
async def test_enroll_inactive_sequence_rejected(
    client: AsyncClient, db_session: AsyncSession, sample_contact: Contact
):
    seq = await _make_sequence(db_session, name="Inactive", active=False)
    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_enroll_unknown_sequence_404(client: AsyncClient, sample_contact: Contact):
    resp = await client.post(
        f"/api/v1/sequences/{_uuid.uuid4()}/enroll",
        json={"contact_ids": [str(sample_contact.id)]},
    )
    assert resp.status_code == 404
