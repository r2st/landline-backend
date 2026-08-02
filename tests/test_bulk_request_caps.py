"""Bulk id lists are bounded, and enrolling a batch is one flush not N.

Most bulk request bodies in this codebase already cap their id list — contacts
at 1000, lead scoring at 500, thread ops at 200. Three did not: the two sequence
enroll bodies and the two company bulk bodies. These tests pin the caps, and
pin the reason the enroll one mattered most: that endpoint does per-id work
inline in the request, so an uncapped list is an uncapped amount of work.
"""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.sequence import OutreachSequence
from app.schemas.company import MAX_BULK_COMPANIES
from app.schemas.outreach import MAX_ENROLL_BATCH


@pytest.fixture(autouse=True)
def _no_celery_dispatch(monkeypatch):
    """Keep the enroll endpoint off a real broker."""
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(outreach_tasks.send_sequence_step, "delay", lambda *a, **k: None)


async def _make_sequence(db_session: AsyncSession, name="Cap Seq") -> OutreachSequence:
    seq = OutreachSequence(id=_uuid.uuid4(), name=name, is_active=True)
    db_session.add(seq)
    await db_session.commit()
    return seq


def _ids(n: int) -> list[str]:
    return [str(_uuid.uuid4()) for _ in range(n)]


# --- Enrollment --------------------------------------------------------------


@pytest.mark.asyncio
async def test_enroll_rejects_a_batch_over_the_cap(
    client: AsyncClient, db_session: AsyncSession
):
    """422 from validation, before a single id is looked up or inserted."""
    seq = await _make_sequence(db_session)
    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": _ids(MAX_ENROLL_BATCH + 1)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_enroll_by_industry_rejects_a_batch_over_the_cap(client: AsyncClient):
    """The second door into the same helper needs the same cap.

    Asserted without a real industry key on purpose: validation of the body has
    to happen before the handler resolves anything, so an over-cap list is a 422
    rather than the 404 an unknown industry would earn.
    """
    resp = await client.post(
        "/api/v1/sequences/enroll-by-industry",
        json={"industry": "cnc_machining", "contact_ids": _ids(MAX_ENROLL_BATCH + 1)},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_enroll_still_accepts_a_normal_batch(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company
):
    """The cap must not disturb the batch sizes anyone actually sends."""
    seq = await _make_sequence(db_session, name="Normal Batch Seq")
    contacts = [
        Contact(
            id=_uuid.uuid4(),
            company_id=sample_company.id,
            first_name=f"C{i}",
            email=f"c{i}@capped.test",
            source="test",
        )
        for i in range(5)
    ]
    db_session.add_all(contacts)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(c.id) for c in contacts]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrolled_count"] == 5


@pytest.mark.asyncio
async def test_enroll_batch_flushes_once_not_once_per_contact(
    client: AsyncClient, db_session: AsyncSession, sample_company: Company, monkeypatch
):
    """The loop used to flush per contact purely to read back a generated id.

    That made an N-contact batch N database round-trips. The ids are now
    generated up front, so the whole batch is one flush — and the enrollments
    still come back with real, distinct ids, which is what the flush was
    protecting.
    """
    seq = await _make_sequence(db_session, name="Flush Count Seq")
    contacts = [
        Contact(
            id=_uuid.uuid4(),
            company_id=sample_company.id,
            first_name=f"F{i}",
            email=f"f{i}@capped.test",
            source="test",
        )
        for i in range(6)
    ]
    db_session.add_all(contacts)
    await db_session.commit()

    dispatched: list[str] = []
    import app.tasks.outreach as outreach_tasks

    monkeypatch.setattr(
        outreach_tasks.send_sequence_step, "delay", lambda eid: dispatched.append(eid)
    )

    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    flushes = 0
    original_flush = _AsyncSession.flush

    async def counting_flush(self, *args, **kwargs):
        nonlocal flushes
        flushes += 1
        return await original_flush(self, *args, **kwargs)

    monkeypatch.setattr(_AsyncSession, "flush", counting_flush)

    resp = await client.post(
        f"/api/v1/sequences/{seq.id}/enroll",
        json={"contact_ids": [str(c.id) for c in contacts]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enrolled_count"] == 6

    # Six contacts, and the enroll path flushes far fewer than six times. The
    # bound is deliberately loose: the commit at the end of the request and any
    # autoflush are not this test's business — the per-contact flush is.
    assert flushes < 6, f"expected a batched flush, saw {flushes}"

    # Every enrollment still got its own id.
    assert len(dispatched) == 6
    assert len(set(dispatched)) == 6


# --- Company bulk operations -------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_update_stage_rejects_over_the_cap(client: AsyncClient):
    resp = await client.post(
        "/api/v1/companies/bulk-update-stage",
        json={"company_ids": _ids(MAX_BULK_COMPANIES + 1), "pipeline_stage": "contacted"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_tag_rejects_over_the_cap(client: AsyncClient):
    resp = await client.post(
        "/api/v1/companies/bulk-tag",
        json={"company_ids": _ids(MAX_BULK_COMPANIES + 1), "add": ["x"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_company_bulk_ops_still_work_at_normal_size(
    client: AsyncClient, db_session: AsyncSession
):
    company = Company(id=_uuid.uuid4(), name="Cap Check Co", country="US", source="test")
    db_session.add(company)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/companies/bulk-tag",
        json={"company_ids": [str(company.id)], "add": ["priority"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1


@pytest.mark.asyncio
async def test_bulk_tag_caps_the_tag_lists_too(client: AsyncClient, db_session: AsyncSession):
    """The contact equivalent already capped add/remove at 50; this one didn't.

    Tags are unioned into a JSON column, so an unbounded add list writes an
    unbounded row.
    """
    company = Company(id=_uuid.uuid4(), name="Tag Flood Co", country="US", source="test")
    db_session.add(company)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/companies/bulk-tag",
        json={"company_ids": [str(company.id)], "add": [f"t{i}" for i in range(51)]},
    )
    assert resp.status_code == 422
