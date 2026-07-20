"""Pagination on previously-unbounded list endpoints (P2 audit fix).

These endpoints used to return ``.scalars().all()`` with no limit. Confirm the
new limit/offset (and page/page_size) params bound the result set.
"""

import uuid

import pytest

from app.models.company import Company
from app.models.company_group import CompanyGroup


async def _make_companies(db, n, prefix="Co"):
    companies = []
    for i in range(n):
        c = Company(id=uuid.uuid4(), name=f"{prefix}{i:02d}", country="US", source="test")
        db.add(c)
        companies.append(c)
    await db.flush()
    return companies


# --- groups ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_groups_list_respects_limit_offset(client, db_session):
    for i in range(5):
        db_session.add(CompanyGroup(id=uuid.uuid4(), name=f"Group {i:02d}"))
    await db_session.commit()

    resp = await client.get("/api/v1/groups", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    resp2 = await client.get("/api/v1/groups", params={"limit": 2, "offset": 4})
    assert len(resp2.json()) == 1  # 5 total, offset 4 → 1 left


@pytest.mark.asyncio
async def test_groups_list_default_bounded(client, db_session):
    db_session.add(CompanyGroup(id=uuid.uuid4(), name="Solo"))
    await db_session.commit()
    resp = await client.get("/api/v1/groups")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


# --- import/export CSV -------------------------------------------------------

@pytest.mark.asyncio
async def test_export_companies_csv_limit(client, db_session):
    await _make_companies(db_session, 6)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/import-export/export/companies/csv", params={"limit": 3}
    )
    assert resp.status_code == 200
    # header row + 3 data rows
    lines = [ln for ln in resp.text.strip().splitlines() if ln]
    assert len(lines) == 4


@pytest.mark.asyncio
async def test_export_companies_csv_default_exports_all(client, db_session):
    await _make_companies(db_session, 6)
    await db_session.commit()
    resp = await client.get("/api/v1/import-export/export/companies/csv")
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.strip().splitlines() if ln]
    assert len(lines) == 7  # header + 6


@pytest.mark.asyncio
async def test_export_contacts_csv_offset(client, db_session):
    from app.models.contact import Contact

    company = (await _make_companies(db_session, 1))[0]
    for i in range(4):
        db_session.add(Contact(
            id=uuid.uuid4(), company_id=company.id, email=f"c{i}@x.co",
            first_name=f"C{i}", source="test",
        ))
    await db_session.commit()

    resp = await client.get(
        "/api/v1/import-export/export/contacts/csv", params={"limit": 2, "offset": 2}
    )
    assert resp.status_code == 200
    lines = [ln for ln in resp.text.strip().splitlines() if ln]
    assert len(lines) == 3  # header + 2


# --- ab_tests (envelope + total) --------------------------------------------

@pytest.mark.asyncio
async def test_ab_tests_list_accepts_pagination(client):
    resp = await client.get("/api/v1/dialtone/ab-tests", params={"page": 1, "page_size": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body and "total" in body
    assert isinstance(body["items"], list)
