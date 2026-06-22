"""Tests for deal CRUD and revenue summary."""

import uuid as _uuid

import pytest
from httpx import AsyncClient

from app.models.company import Company


async def _make_deal(client, company_id, **kw):
    payload = {"company_id": str(company_id), "title": "Annual contract"}
    payload.update(kw)
    resp = await client.post("/api/v1/deals", json=payload)
    return resp


@pytest.mark.asyncio
async def test_create_deal(client: AsyncClient, sample_company: Company):
    resp = await _make_deal(client, sample_company.id, value=50000, status="pending")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["value"] == 50000
    assert data["currency"] == "USD"
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_create_deal_unknown_company(client: AsyncClient):
    resp = await _make_deal(client, _uuid.uuid4(), value=100)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_deal_invalid_status(client: AsyncClient, sample_company: Company):
    resp = await _make_deal(client, sample_company.id, status="bogus")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_update_delete_deal(client: AsyncClient, sample_company: Company):
    created = (await _make_deal(client, sample_company.id, value=1000)).json()
    did = created["id"]

    got = await client.get(f"/api/v1/deals/{did}")
    assert got.status_code == 200

    upd = await client.patch(f"/api/v1/deals/{did}", json={"status": "won", "value": 1200})
    assert upd.status_code == 200
    assert upd.json()["status"] == "won"
    assert upd.json()["value"] == 1200

    delete = await client.delete(f"/api/v1/deals/{did}")
    assert delete.status_code == 204
    assert (await client.get(f"/api/v1/deals/{did}")).status_code == 404


@pytest.mark.asyncio
async def test_list_deals_filter(client: AsyncClient, sample_company: Company):
    await _make_deal(client, sample_company.id, value=100, status="won")
    await _make_deal(client, sample_company.id, value=200, status="pending")

    won = await client.get("/api/v1/deals", params={"status": "won"})
    assert won.status_code == 200
    assert all(d["status"] == "won" for d in won.json())

    by_company = await client.get("/api/v1/deals", params={"company_id": str(sample_company.id)})
    assert len(by_company.json()) >= 2


@pytest.mark.asyncio
async def test_deals_summary(client: AsyncClient, sample_company: Company):
    await _make_deal(client, sample_company.id, value=10000, status="won")
    await _make_deal(client, sample_company.id, value=5000, status="won")
    await _make_deal(client, sample_company.id, value=8000, status="lost")
    await _make_deal(client, sample_company.id, value=3000, status="pending")

    resp = await client.get("/api/v1/deals/summary")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total_deals"] == 4
    assert data["won_count"] == 2
    assert data["won_revenue"] == 15000
    assert data["lost_value"] == 8000
    assert data["pipeline_value"] == 3000
    # win rate = 2 won / 3 decided = 66.7%
    assert data["win_rate"] == pytest.approx(66.7, abs=0.1)
    statuses = {b["status"]: b for b in data["by_status"]}
    assert statuses["won"]["total_value"] == 15000
