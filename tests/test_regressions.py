"""Regression tests for issues found in the issue-hunt pass.

Covers: CSV formula-injection sanitization on export, the manual-compose
unsubscribed guard, and UUID validation on path/body params.
"""

import uuid

import pytest

from app.models.contact import Contact


@pytest.mark.asyncio
async def test_csv_export_sanitizes_formula_injection(client, db_session, sample_company):
    """A company name starting with '=' must be quoted in the CSV export."""
    from app.models.company import Company

    evil = Company(
        id=uuid.uuid4(),
        name="=HYPERLINK(\"http://evil\")",
        country="US",
        source="test",
    )
    db_session.add(evil)
    await db_session.commit()

    resp = await client.get("/api/v1/import-export/export/companies/csv")
    assert resp.status_code == 200
    body = resp.text
    # The dangerous cell is defused with a leading single quote...
    assert "'=HYPERLINK" in body
    # ...and the raw, unescaped formula never appears as a bare cell value.
    assert ",=HYPERLINK" not in body


@pytest.mark.asyncio
async def test_compose_skips_unsubscribed_contact(client, db_session, sample_company):
    """Manual compose must not email a contact who has unsubscribed."""
    c = Contact(
        id=uuid.uuid4(),
        company_id=sample_company.id,
        first_name="Opted",
        last_name="Out",
        email="opted.out@acmestamp.com",
        unsubscribed=True,
        source="test",
    )
    db_session.add(c)
    await db_session.commit()

    await client.post("/api/v1/email-templates", json={
        "name": "T", "subject": "Hi {{first_name}}", "body": "<p>{{company}}</p>",
    })
    resp = await client.post("/api/v1/email-compose", json={"contact_ids": [str(c.id)]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["skipped"] == 1
    assert data["sent"] == 0
    assert data["results"][0]["status"] == "skipped"
    assert data["results"][0]["detail"] == "unsubscribed"


@pytest.mark.asyncio
async def test_malformed_uuid_path_returns_422(client):
    """A non-UUID path param should be a 422, not a 500."""
    resp = await client.post("/api/v1/news/check/not-a-uuid")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_malformed_uuid_body_returns_400(client):
    """A non-UUID id in the request body should be a 400, not a 500."""
    resp = await client.post("/api/v1/ai/generate-email", json={"company_id": "not-a-uuid"})
    assert resp.status_code == 400
