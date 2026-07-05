"""Tests for email template management, preview, and compose/send."""

import pytest

DEMO_LINK = "https://best-molding-demo.pages.dev"


@pytest.mark.asyncio
async def test_create_first_template_becomes_default(client):
    resp = await client.post("/api/v1/email-templates", json={
        "name": "Template A",
        "subject": "Hello {{first_name}}",
        "body": "<p>Hi {{first_name}} at {{company}}</p>",
    })
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["is_default"] is True  # first template auto-defaults


@pytest.mark.asyncio
async def test_set_default_moves_flag(client):
    a = (await client.post("/api/v1/email-templates", json={
        "name": "A", "subject": "s", "body": "b",
    })).json()
    b = (await client.post("/api/v1/email-templates", json={
        "name": "B", "subject": "s", "body": "b",
    })).json()
    assert a["is_default"] is True
    assert b["is_default"] is False

    resp = await client.post(f"/api/v1/email-templates/{b['id']}/set-default")
    assert resp.status_code == 200
    assert resp.json()["is_default"] is True

    listing = (await client.get("/api/v1/email-templates")).json()
    defaults = [t for t in listing if t["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == b["id"]


@pytest.mark.asyncio
async def test_cannot_delete_default(client):
    a = (await client.post("/api/v1/email-templates", json={
        "name": "A", "subject": "s", "body": "b",
    })).json()
    resp = await client.delete(f"/api/v1/email-templates/{a['id']}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_non_default(client):
    (await client.post("/api/v1/email-templates", json={
        "name": "A", "subject": "s", "body": "b",
    })).json()
    b = (await client.post("/api/v1/email-templates", json={
        "name": "B", "subject": "s", "body": "b",
    })).json()
    resp = await client.delete(f"/api/v1/email-templates/{b['id']}")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_update_template(client):
    a = (await client.post("/api/v1/email-templates", json={
        "name": "A", "subject": "s", "body": "b",
    })).json()
    resp = await client.put(f"/api/v1/email-templates/{a['id']}", json={
        "subject": "new subject {{company}}",
    })
    assert resp.status_code == 200
    assert resp.json()["subject"] == "new subject {{company}}"


@pytest.mark.asyncio
async def test_preview_with_contact(client, sample_contact):
    await client.post("/api/v1/email-templates", json={
        "name": "P",
        "subject": "Hi {{first_name}} — {{company}}",
        "body": f"<p>{{{{contact_title}}}} in {{{{city}}}} ({{{{industry_segment}}}}). Demo: {DEMO_LINK}</p>",
    })
    resp = await client.post("/api/v1/email-templates/preview", json={
        "contact_id": str(sample_contact.id),
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "John" in data["subject"]
    assert "Acme Stamping Co." in data["subject"]
    assert "VP Manufacturing" in data["body"]
    assert "Cleveland" in data["body"]
    assert "Metal Stamping" in data["body"]
    assert DEMO_LINK in data["body"]


@pytest.mark.asyncio
async def test_preview_with_sample_data(client):
    await client.post("/api/v1/email-templates", json={
        "name": "P", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    resp = await client.post("/api/v1/email-templates/preview", json={})
    assert resp.status_code == 200
    # Sample placeholder values render
    assert resp.json()["subject"].startswith("Hi ")


@pytest.mark.asyncio
async def test_compose_skips_contact_without_email(client, sample_company, db_session):
    from app.models.contact import Contact
    import uuid as _uuid
    c = Contact(id=_uuid.uuid4(), company_id=sample_company.id,
                first_name="No", last_name="Email", source="test")
    db_session.add(c)
    await db_session.commit()

    await client.post("/api/v1/email-templates", json={
        "name": "P", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(c.id)],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["skipped"] == 1
    assert data["results"][0]["status"] == "skipped"


@pytest.mark.asyncio
async def test_compose_without_ses_creds_records_send(client, sample_contact):
    """With no SES creds the sender returns 'skipped'; the send is still tracked."""
    await client.post("/api/v1/email-templates", json={
        "name": "P", "subject": "Hi {{first_name}}",
        "body": f"<p>Demo {DEMO_LINK}</p>",
    })
    resp = await client.post("/api/v1/email-compose", json={
        "contact_ids": [str(sample_contact.id)],
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["sent"] + data["skipped"] == 1

    sends = (await client.get("/api/v1/email-sends")).json()
    assert len(sends) >= 1
    assert sends[0]["to_email"] == sample_contact.email
