"""Tests for follow-up reminders and stale-contact detection."""

import uuid as _uuid
from datetime import datetime, timezone, timedelta

import pytest


@pytest.mark.asyncio
async def test_create_follow_up_for_contact(client, sample_contact):
    due = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    resp = await client.post("/api/v1/follow-ups", json={
        "contact_id": str(sample_contact.id),
        "due_date": due,
        "reason": "Second touch",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "pending"
    assert data["contact_id"] == str(sample_contact.id)
    # Company is auto-filled from the contact.
    assert data["company_id"] == str(sample_contact.company_id)
    assert data["contact_name"] == "John Smith"
    assert data["is_overdue"] is False


@pytest.mark.asyncio
async def test_create_requires_target(client):
    due = datetime.now(timezone.utc).isoformat()
    resp = await client.post("/api/v1/follow-ups", json={"due_date": due})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_overdue_filter(client, sample_contact):
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
    await client.post("/api/v1/follow-ups", json={
        "contact_id": str(sample_contact.id), "due_date": past, "reason": "overdue one",
    })
    await client.post("/api/v1/follow-ups", json={
        "contact_id": str(sample_contact.id), "due_date": future, "reason": "future one",
    })

    all_items = (await client.get("/api/v1/follow-ups")).json()
    assert len(all_items) == 2

    overdue = (await client.get("/api/v1/follow-ups?overdue=true")).json()
    assert len(overdue) == 1
    assert overdue[0]["reason"] == "overdue one"
    assert overdue[0]["is_overdue"] is True


@pytest.mark.asyncio
async def test_complete_follow_up(client, sample_contact):
    due = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    created = (await client.post("/api/v1/follow-ups", json={
        "contact_id": str(sample_contact.id), "due_date": due,
    })).json()

    resp = await client.patch(f"/api/v1/follow-ups/{created['id']}", json={
        "status": "completed",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "completed"
    assert data["completed_at"] is not None
    # A completed follow-up is never "overdue".
    assert data["is_overdue"] is False


@pytest.mark.asyncio
async def test_delete_follow_up(client, sample_contact):
    due = datetime.now(timezone.utc).isoformat()
    created = (await client.post("/api/v1/follow-ups", json={
        "contact_id": str(sample_contact.id), "due_date": due,
    })).json()
    resp = await client.delete(f"/api/v1/follow-ups/{created['id']}")
    assert resp.status_code == 204
    remaining = (await client.get("/api/v1/follow-ups")).json()
    assert remaining == []


@pytest.mark.asyncio
async def test_stale_contacts_includes_never_emailed(client, sample_contact):
    """A contact with an email and no sends shows up as stale."""
    resp = await client.get("/api/v1/follow-ups/stale-contacts?days=30")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data) == 1
    assert data[0]["contact_id"] == str(sample_contact.id)
    assert data[0]["last_emailed_at"] is None
    assert data[0]["days_since_contact"] is None
    assert data[0]["company_name"] == "Acme Stamping Co."


@pytest.mark.asyncio
async def test_stale_contacts_excludes_recently_emailed(client, sample_contact, db_session):
    from app.models.email_template import EmailSend
    recent = EmailSend(
        id=_uuid.uuid4(),
        contact_id=sample_contact.id,
        to_email=sample_contact.email,
        subject="hi",
        status="sent",
        sent_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db_session.add(recent)
    await db_session.commit()

    # Within 30 days → not stale.
    fresh = (await client.get("/api/v1/follow-ups/stale-contacts?days=30")).json()
    assert all(c["contact_id"] != str(sample_contact.id) for c in fresh)

    # With a 1-day threshold the 2-day-old send is stale again.
    stale = (await client.get("/api/v1/follow-ups/stale-contacts?days=1")).json()
    match = [c for c in stale if c["contact_id"] == str(sample_contact.id)]
    assert len(match) == 1
    assert match[0]["days_since_contact"] >= 1
