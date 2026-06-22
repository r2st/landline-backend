"""Tests for enhanced bulk operations: bulk-tag and bulk-export."""

import uuid as _uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore


async def _make_company(client: AsyncClient, name: str, tags=None) -> str:
    resp = await client.post("/api/v1/companies/", json={
        "name": name, "country": "US", "source": "test",
        "tags": tags or [],
    })
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_bulk_tag_add(client: AsyncClient):
    ids = [await _make_company(client, f"Tag Add {i}") for i in range(2)]
    resp = await client.post("/api/v1/companies/bulk-tag", json={
        "company_ids": ids,
        "add": ["priority", "midwest"],
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"matched": 2, "updated": 2}

    for cid in ids:
        got = (await client.get(f"/api/v1/companies/{cid}")).json()
        assert set(got["tags"]) == {"priority", "midwest"}


@pytest.mark.asyncio
async def test_bulk_tag_remove(client: AsyncClient):
    cid = await _make_company(client, "Tag Remove Co", tags=["a", "b", "c"])
    resp = await client.post("/api/v1/companies/bulk-tag", json={
        "company_ids": [cid],
        "remove": ["b"],
    })
    assert resp.status_code == 200
    got = (await client.get(f"/api/v1/companies/{cid}")).json()
    assert set(got["tags"]) == {"a", "c"}


@pytest.mark.asyncio
async def test_bulk_tag_no_op_not_counted_updated(client: AsyncClient):
    cid = await _make_company(client, "Tag NoOp Co", tags=["x"])
    # Adding a tag it already has changes nothing.
    resp = await client.post("/api/v1/companies/bulk-tag", json={
        "company_ids": [cid],
        "add": ["x"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] == 1
    assert body["updated"] == 0


@pytest.mark.asyncio
async def test_bulk_tag_requires_add_or_remove(client: AsyncClient):
    cid = await _make_company(client, "Tag Empty Co")
    resp = await client.post("/api/v1/companies/bulk-tag", json={
        "company_ids": [cid],
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_bulk_export_basic(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    db_session.add(LeadScore(
        id=_uuid.uuid4(), company_id=sample_company.id,
        score_type="rule_based", fit_score=77,
    ))
    await db_session.commit()

    resp = await client.post("/api/v1/companies/bulk-export", json={
        "company_ids": [str(sample_company.id)],
        "include_contacts": True,
        "include_scores": True,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["count"] == 1
    assert "exported_at" in data
    company = data["companies"][0]
    assert company["pipeline_stage"] == "new_lead"
    assert len(company["contacts"]) == 1
    assert company["contacts"][0]["email"] == sample_contact.email
    assert len(company["scores"]) == 1
    assert company["scores"][0]["fit_score"] == 77
    # Activity not requested.
    assert "activity" not in company


@pytest.mark.asyncio
async def test_bulk_export_with_activity(
    client: AsyncClient, db_session: AsyncSession,
    sample_company: Company, sample_contact: Contact,
):
    resp = await client.post("/api/v1/companies/bulk-export", json={
        "company_ids": [str(sample_company.id)],
        "include_activity": True,
    })
    assert resp.status_code == 200, resp.text
    company = resp.json()["companies"][0]
    assert "activity" in company
    # The sample contact's creation shows up as an activity item.
    assert any(a["type"] == "contact_added" for a in company["activity"])


@pytest.mark.asyncio
async def test_bulk_export_all_when_no_ids(
    client: AsyncClient, sample_company: Company,
):
    resp = await client.post("/api/v1/companies/bulk-export", json={
        "include_contacts": False,
        "include_scores": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert all("contacts" not in c for c in data["companies"])
