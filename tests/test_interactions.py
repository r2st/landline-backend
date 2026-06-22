"""Tests for interaction CRUD."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest
from httpx import AsyncClient

from app.models.company import Company
from app.models.contact import Contact


async def _make(client, company_id, **kw):
    payload = {"company_id": str(company_id), "interaction_type": "meeting"}
    payload.update(kw)
    return await client.post("/api/v1/interactions", json=payload)


@pytest.mark.asyncio
async def test_create_interaction(client: AsyncClient, sample_company: Company, sample_contact: Contact):
    now = datetime.now(timezone.utc)
    resp = await _make(
        client, sample_company.id,
        contact_id=str(sample_contact.id),
        interaction_type="call",
        subject="Quarterly check-in",
        content="Discussed renewal and expansion.",
        follow_up_date=(now + timedelta(days=14)).isoformat(),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["interaction_type"] == "call"
    assert data["content"] == "Discussed renewal and expansion."
    assert data["follow_up_date"] is not None
    assert data["occurred_at"] is not None  # defaulted to now


@pytest.mark.asyncio
async def test_create_invalid_type(client: AsyncClient, sample_company: Company):
    assert (await _make(client, sample_company.id, interaction_type="carrier_pigeon")).status_code == 422


@pytest.mark.asyncio
async def test_create_unknown_company(client: AsyncClient):
    assert (await _make(client, _uuid.uuid4())).status_code == 404


@pytest.mark.asyncio
async def test_interaction_lifecycle(client: AsyncClient, sample_company: Company):
    created = (await _make(client, sample_company.id, subject="Initial")).json()
    iid = created["id"]

    assert (await client.get(f"/api/v1/interactions/{iid}")).status_code == 200

    upd = await client.patch(f"/api/v1/interactions/{iid}", json={
        "subject": "Updated", "content": "new content",
    })
    assert upd.status_code == 200
    assert upd.json()["subject"] == "Updated"
    assert upd.json()["content"] == "new content"

    assert (await client.delete(f"/api/v1/interactions/{iid}")).status_code == 204
    assert (await client.get(f"/api/v1/interactions/{iid}")).status_code == 404


@pytest.mark.asyncio
async def test_list_interactions_filters(
    client: AsyncClient, sample_company: Company, sample_contact: Contact
):
    await _make(client, sample_company.id, interaction_type="meeting")
    await _make(client, sample_company.id, interaction_type="email",
                contact_id=str(sample_contact.id))

    by_company = await client.get("/api/v1/interactions",
                                  params={"company_id": str(sample_company.id)})
    assert by_company.status_code == 200
    assert len(by_company.json()) >= 2

    emails = await client.get("/api/v1/interactions", params={"interaction_type": "email"})
    assert all(i["interaction_type"] == "email" for i in emails.json())

    by_contact = await client.get("/api/v1/interactions",
                                  params={"contact_id": str(sample_contact.id)})
    assert all(i["contact_id"] == str(sample_contact.id) for i in by_contact.json())


@pytest.mark.asyncio
async def test_logged_interaction_shows_in_activity(
    client: AsyncClient, sample_company: Company
):
    await _make(client, sample_company.id, interaction_type="note", subject="A note")
    feed = await client.get(f"/api/v1/companies/{sample_company.id}/activity")
    assert feed.status_code == 200
    assert any(i["type"] == "note" for i in feed.json()["items"])
