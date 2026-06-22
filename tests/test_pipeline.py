"""Tests for kanban pipeline stage management."""

import pytest
from httpx import AsyncClient


async def _make_company(client: AsyncClient, name: str) -> str:
    resp = await client.post("/api/v1/companies/", json={
        "name": name, "country": "US", "source": "test",
    })
    assert resp.status_code == 201
    return resp.json()["id"]


@pytest.mark.asyncio
async def test_new_company_defaults_to_new_lead(client: AsyncClient):
    resp = await client.post("/api/v1/companies/", json={
        "name": "Pipeline Default Co", "country": "US", "source": "test",
    })
    assert resp.status_code == 201
    assert resp.json()["pipeline_stage"] == "new_lead"


@pytest.mark.asyncio
async def test_patch_pipeline_stage(client: AsyncClient):
    cid = await _make_company(client, "Pipeline Patch Co")
    resp = await client.patch(f"/api/v1/companies/{cid}", json={
        "pipeline_stage": "negotiating",
    })
    assert resp.status_code == 200
    assert resp.json()["pipeline_stage"] == "negotiating"


@pytest.mark.asyncio
async def test_patch_invalid_pipeline_stage_rejected(client: AsyncClient):
    cid = await _make_company(client, "Pipeline Bad Co")
    resp = await client.patch(f"/api/v1/companies/{cid}", json={
        "pipeline_stage": "not_a_stage",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_filter_by_pipeline_stage(client: AsyncClient):
    cid = await _make_company(client, "Pipeline Filter Co")
    await client.patch(f"/api/v1/companies/{cid}", json={"pipeline_stage": "client"})

    resp = await client.get("/api/v1/companies/", params={"pipeline_stage": "client"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    assert all(item["pipeline_stage"] == "client" for item in data["items"])


@pytest.mark.asyncio
async def test_bulk_update_stage(client: AsyncClient):
    ids = [await _make_company(client, f"Bulk Stage Co {i}") for i in range(3)]
    resp = await client.post("/api/v1/companies/bulk-update-stage", json={
        "company_ids": ids,
        "pipeline_stage": "engaged",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["matched"] == 3
    assert body["updated"] == 3

    for cid in ids:
        got = await client.get(f"/api/v1/companies/{cid}")
        assert got.json()["pipeline_stage"] == "engaged"


@pytest.mark.asyncio
async def test_bulk_update_stage_invalid_stage(client: AsyncClient):
    cid = await _make_company(client, "Bulk Bad Stage Co")
    resp = await client.post("/api/v1/companies/bulk-update-stage", json={
        "company_ids": [cid],
        "pipeline_stage": "bogus",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pipeline_summary(client: AsyncClient):
    c1 = await _make_company(client, "Summary Co A")
    c2 = await _make_company(client, "Summary Co B")
    await client.patch(f"/api/v1/companies/{c1}", json={"pipeline_stage": "outreach"})
    await client.patch(f"/api/v1/companies/{c2}", json={"pipeline_stage": "outreach"})

    resp = await client.get("/api/v1/pipeline/summary")
    assert resp.status_code == 200
    data = resp.json()
    # All seven stages are always present.
    stage_names = {s["stage"] for s in data["stages"]}
    assert stage_names == {
        "new_lead", "researching", "outreach", "engaged",
        "negotiating", "client", "churned",
    }
    by_stage = {s["stage"]: s["count"] for s in data["stages"]}
    assert by_stage["outreach"] >= 2
    assert data["total"] == sum(s["count"] for s in data["stages"])
