"""Tests for the knowledge base API and seed loaders."""

import uuid as _uuid

import pytest
from httpx import AsyncClient

from app.models.knowledge import IndustryContact, KnowledgeDocument
from app.services import knowledge_data


# --- Seed loaders (pure, no DB) ------------------------------------------------

def test_load_industry_contacts_parses_csv():
    rows = knowledge_data.load_industry_contacts()
    assert len(rows) > 100  # 105 contact rows in the seed CSV
    industries = {r["industry"] for r in rows}
    assert industries == {
        "Plastic Injection Molding",
        "CNC Machining",
        "Metal Stamping & Forming",
        "Food & Beverage Manufacturing",
    }
    # Every row has a company, and emails look like emails when present.
    assert all(r["company_name"] for r in rows)
    first = rows[0]
    assert first["company_name"] == "Midwest Precision Molding (MPM)"
    assert first["contact_email"] == "laustin@mpmplastics.com"


def test_load_documents_reads_markdown():
    docs = knowledge_data.load_documents()
    keys = {d["key"] for d in docs}
    assert keys == {"sales-psychology-guidelines", "contact-approach-strategy"}
    for d in docs:
        assert d["format"] == "markdown"
        assert len(d["content"]) > 500


# --- Helpers -------------------------------------------------------------------

async def _seed_contacts(db_session):
    rows = [
        IndustryContact(
            id=_uuid.uuid4(),
            industry="CNC Machining",
            company_name="Swiss-Tech LLC",
            website="https://swisstechllc.com",
            location="Delavan, Wisconsin, USA",
            employee_count="64",
            contact_name="Mike Ledestich",
            contact_title="President",
            contact_email="mike.ledestich@swisstechllc.com",
            notes="Triple-certified.",
        ),
        IndustryContact(
            id=_uuid.uuid4(),
            industry="CNC Machining",
            company_name="Swiss-Tech LLC",
            location="Delavan, Wisconsin, USA",
            contact_name="Paul Gilson",
            contact_title="Director of Quality",
            contact_email="paul.gilson@swisstechllc.com",
        ),
        IndustryContact(
            id=_uuid.uuid4(),
            industry="Food & Beverage Manufacturing",
            company_name="Beaverton Foods",
            contact_name="Domonic Biggi",
            contact_title="CEO & Chairman",
            contact_email="dbiggi@beavertonfoods.com",
        ),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    return rows


# --- Industry contacts CRUD ----------------------------------------------------

@pytest.mark.asyncio
async def test_list_contacts_and_filter(client: AsyncClient, db_session):
    await _seed_contacts(db_session)

    resp = await client.get("/api/v1/knowledge/contacts")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 3

    resp = await client.get("/api/v1/knowledge/contacts", params={"industry": "CNC Machining"})
    assert {c["company_name"] for c in resp.json()} == {"Swiss-Tech LLC"}
    assert len(resp.json()) == 2

    resp = await client.get("/api/v1/knowledge/contacts", params={"search": "Gilson"})
    data = resp.json()
    assert len(data) == 1
    assert data[0]["contact_name"] == "Paul Gilson"

    resp = await client.get("/api/v1/knowledge/contacts", params={"company": "beaverton"})
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_contacts_industries_counts(client: AsyncClient, db_session):
    await _seed_contacts(db_session)
    resp = await client.get("/api/v1/knowledge/contacts/industries")
    assert resp.status_code == 200, resp.text
    by_industry = {r["industry"]: r for r in resp.json()}
    assert by_industry["CNC Machining"]["contact_count"] == 2
    assert by_industry["CNC Machining"]["company_count"] == 1
    assert by_industry["Food & Beverage Manufacturing"]["contact_count"] == 1


@pytest.mark.asyncio
async def test_get_contact_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/knowledge/contacts/{_uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_update_delete_contact(client: AsyncClient):
    payload = {
        "industry": "Metal Stamping & Forming",
        "company_name": "Keats Manufacturing Co.",
        "contact_name": "Matt Eggemeyer",
        "contact_title": "President & CEO",
        "contact_email": "meggemeyer@keatsmfg.com",
    }
    resp = await client.post("/api/v1/knowledge/contacts", json=payload)
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]

    resp = await client.get(f"/api/v1/knowledge/contacts/{cid}")
    assert resp.json()["contact_name"] == "Matt Eggemeyer"

    resp = await client.put(
        f"/api/v1/knowledge/contacts/{cid}",
        json={"contact_title": "CEO", "notes": "Updated"},
    )
    assert resp.status_code == 200
    assert resp.json()["contact_title"] == "CEO"
    assert resp.json()["notes"] == "Updated"
    assert resp.json()["company_name"] == "Keats Manufacturing Co."  # unchanged

    resp = await client.delete(f"/api/v1/knowledge/contacts/{cid}")
    assert resp.status_code == 204
    resp = await client.get(f"/api/v1/knowledge/contacts/{cid}")
    assert resp.status_code == 404


# --- Knowledge documents CRUD --------------------------------------------------

async def _seed_doc(db_session, key="sales-psychology-guidelines", category="guidelines"):
    doc = KnowledgeDocument(
        id=_uuid.uuid4(),
        key=key,
        title="Sales Psychology",
        category=category,
        format="markdown",
        content="# Heading\n\nSome guidance.",
    )
    db_session.add(doc)
    await db_session.flush()
    return doc


@pytest.mark.asyncio
async def test_documents_list_and_get(client: AsyncClient, db_session):
    await _seed_doc(db_session)
    await _seed_doc(db_session, key="contact-approach-strategy", category="strategy")

    resp = await client.get("/api/v1/knowledge/documents")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    # Summary omits content body.
    assert "content" not in resp.json()[0]

    resp = await client.get("/api/v1/knowledge/documents", params={"category": "strategy"})
    assert len(resp.json()) == 1

    resp = await client.get("/api/v1/knowledge/documents/sales-psychology-guidelines")
    assert resp.status_code == 200
    assert resp.json()["content"].startswith("# Heading")


@pytest.mark.asyncio
async def test_guidelines_and_strategy_shortcuts(client: AsyncClient, db_session):
    await _seed_doc(db_session)
    await _seed_doc(db_session, key="contact-approach-strategy", category="strategy")

    resp = await client.get("/api/v1/knowledge/guidelines")
    assert resp.status_code == 200
    assert resp.json()["key"] == "sales-psychology-guidelines"

    resp = await client.get("/api/v1/knowledge/strategy")
    assert resp.status_code == 200
    assert resp.json()["key"] == "contact-approach-strategy"


@pytest.mark.asyncio
async def test_document_create_conflict_and_edit(client: AsyncClient):
    payload = {"key": "new-playbook", "title": "Playbook", "category": "guidelines", "content": "x"}
    resp = await client.post("/api/v1/knowledge/documents", json=payload)
    assert resp.status_code == 201, resp.text

    # Duplicate key conflicts.
    resp = await client.post("/api/v1/knowledge/documents", json=payload)
    assert resp.status_code == 409

    resp = await client.put(
        "/api/v1/knowledge/documents/new-playbook", json={"content": "## Edited"}
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "## Edited"

    resp = await client.delete("/api/v1/knowledge/documents/new-playbook")
    assert resp.status_code == 204
    resp = await client.get("/api/v1/knowledge/documents/new-playbook")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_document_invalid_key_rejected(client: AsyncClient):
    resp = await client.post(
        "/api/v1/knowledge/documents",
        json={"key": "Bad Key!", "title": "x", "content": ""},
    )
    assert resp.status_code == 422


# --- Industry verticals + AI guidelines (read-only) ----------------------------

@pytest.mark.asyncio
async def test_list_industries(client: AsyncClient):
    resp = await client.get("/api/v1/knowledge/industries")
    assert resp.status_code == 200, resp.text
    verticals = resp.json()
    assert len(verticals) == 4
    keys = {v["key"] for v in verticals}
    assert "cnc_machining" in keys
    cnc = next(v for v in verticals if v["key"] == "cnc_machining")
    assert cnc["demo_link"].startswith("https://")
    assert len(cnc["steps"]) == 4
    assert cnc["pain_points"]


@pytest.mark.asyncio
async def test_get_industry_by_key_and_404(client: AsyncClient):
    resp = await client.get("/api/v1/knowledge/industries/cnc_machining")
    assert resp.status_code == 200
    assert resp.json()["name"] == "CNC Machining"

    resp = await client.get("/api/v1/knowledge/industries/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ai_guidelines(client: AsyncClient):
    resp = await client.get("/api/v1/knowledge/ai-guidelines")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    for section in (
        "outreach_principles",
        "email_structure",
        "subject_line_formulas",
        "follow_up_cadence",
        "anti_spam_rules",
        "industry_profiles",
    ):
        assert section in data and data[section]
