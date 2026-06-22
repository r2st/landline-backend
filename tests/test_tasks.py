"""Tests for task CRUD and the upcoming-tasks feed."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient

from app.models.company import Company
from app.models.contact import Contact


async def _make_task(client, company_id, **kw):
    payload = {"company_id": str(company_id), "title": "Call client"}
    payload.update(kw)
    return await client.post("/api/v1/tasks", json=payload)


@pytest.mark.asyncio
async def test_create_task(client: AsyncClient, sample_company: Company, sample_contact: Contact):
    resp = await _make_task(
        client, sample_company.id,
        contact_id=str(sample_contact.id),
        priority="high", task_type="call", assigned_to="rep@apprend.io",
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["priority"] == "high"
    assert data["task_type"] == "call"
    assert data["status"] == "pending"
    assert data["assigned_to"] == "rep@apprend.io"


@pytest.mark.asyncio
async def test_create_task_unknown_company(client: AsyncClient):
    resp = await _make_task(client, _uuid.uuid4())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_task_invalid_enum(client: AsyncClient, sample_company: Company):
    assert (await _make_task(client, sample_company.id, priority="huge")).status_code == 422
    assert (await _make_task(client, sample_company.id, task_type="dance")).status_code == 422


@pytest.mark.asyncio
async def test_task_lifecycle(client: AsyncClient, sample_company: Company):
    created = (await _make_task(client, sample_company.id)).json()
    tid = created["id"]

    assert (await client.get(f"/api/v1/tasks/{tid}")).status_code == 200

    upd = await client.patch(f"/api/v1/tasks/{tid}", json={"status": "completed"})
    assert upd.status_code == 200
    assert upd.json()["status"] == "completed"

    assert (await client.delete(f"/api/v1/tasks/{tid}")).status_code == 204
    assert (await client.get(f"/api/v1/tasks/{tid}")).status_code == 404


@pytest.mark.asyncio
async def test_list_tasks_filters(client: AsyncClient, sample_company: Company):
    await _make_task(client, sample_company.id, priority="urgent", status="pending")
    await _make_task(client, sample_company.id, priority="low", status="completed")

    urgent = await client.get("/api/v1/tasks", params={"priority": "urgent"})
    assert all(t["priority"] == "urgent" for t in urgent.json())

    completed = await client.get("/api/v1/tasks", params={"status": "completed"})
    assert all(t["status"] == "completed" for t in completed.json())


@pytest.mark.asyncio
async def test_upcoming_tasks(client: AsyncClient, sample_company: Company):
    now = datetime.now(timezone.utc)
    # Due in 2 days (in window), in 30 days (out of 7-day window), overdue, completed.
    await _make_task(client, sample_company.id, title="Soon",
                     due_date=(now + timedelta(days=2)).isoformat())
    await _make_task(client, sample_company.id, title="Later",
                     due_date=(now + timedelta(days=30)).isoformat())
    await _make_task(client, sample_company.id, title="Overdue",
                     due_date=(now - timedelta(days=1)).isoformat())
    await _make_task(client, sample_company.id, title="Done", status="completed",
                     due_date=(now + timedelta(days=1)).isoformat())

    resp = await client.get("/api/v1/tasks/upcoming", params={"days": 7})
    assert resp.status_code == 200, resp.text
    titles = [t["title"] for t in resp.json()]
    assert "Soon" in titles
    assert "Overdue" in titles  # include_overdue defaults True
    assert "Later" not in titles
    assert "Done" not in titles  # completed tasks excluded
    # Soonest-due first: overdue precedes soon.
    assert titles.index("Overdue") < titles.index("Soon")


@pytest.mark.asyncio
async def test_upcoming_excludes_overdue_when_disabled(
    client: AsyncClient, sample_company: Company
):
    now = datetime.now(timezone.utc)
    await _make_task(client, sample_company.id, title="OldOne",
                     due_date=(now - timedelta(days=3)).isoformat())
    resp = await client.get(
        "/api/v1/tasks/upcoming", params={"days": 7, "include_overdue": False}
    )
    assert "OldOne" not in [t["title"] for t in resp.json()]
