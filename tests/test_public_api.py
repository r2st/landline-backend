"""Tests for the public REST API (``/api/v1/public``).

Exercises the CRUD surface (contacts, companies, deals, deal notes/activity,
sequence enrollments) end-to-end through the mounted sub-app, and verifies
org isolation — an API key only ever sees and touches its own org's data.
"""

import uuid

import pytest
import pytest_asyncio

from app.models.api_key import ALL_SCOPES, ApiKey
from app.models.company import Company
from app.models.sequence import OutreachSequence
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID
from app.services.api_key_auth import generate_api_key


async def _mint_key(db_session, org_id, scopes):
    raw, key_hash, prefix = generate_api_key()
    key = ApiKey(
        org_id=org_id, name="full", key_hash=key_hash, key_prefix=prefix,
        scopes=list(scopes), rate_limit=100000,
    )
    db_session.add(key)
    await db_session.commit()
    return raw


@pytest_asyncio.fixture
async def api_headers(db_session):
    """An X-API-Key header for a full-scope key in the Apprend org."""
    raw = await _mint_key(db_session, APPREND_ORG_ID, ALL_SCOPES)
    return {"X-API-Key": raw}


# --- docs -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_docs_and_openapi(client):
    """Docs + OpenAPI are reachable without a key, with the X-API-Key scheme."""
    docs = await client.get("/api/v1/public/docs")
    assert docs.status_code == 200
    schema = await client.get("/api/v1/public/openapi.json")
    assert schema.status_code == 200
    spec = schema.json()
    schemes = spec.get("components", {}).get("securitySchemes", {})
    # The X-API-Key apiKey scheme is advertised for the "Authorize" button.
    assert any(
        s.get("type") == "apiKey" and s.get("name") == "X-API-Key"
        for s in schemes.values()
    ), schemes


# --- contacts ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_contacts_crud(client, api_headers, sample_company):
    # Create
    created = await client.post(
        "/api/v1/public/contacts",
        headers=api_headers,
        json={
            "company_id": str(sample_company.id),
            "first_name": "Ada",
            "last_name": "Lovelace",
            "email": "ada@acmestamp.com",
        },
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]

    # Read
    got = await client.get(f"/api/v1/public/contacts/{cid}", headers=api_headers)
    assert got.status_code == 200
    assert got.json()["first_name"] == "Ada"

    # List (paginated envelope)
    lst = await client.get("/api/v1/public/contacts", headers=api_headers)
    assert lst.status_code == 200
    body = lst.json()
    assert body["total"] == 1 and body["page"] == 1
    assert body["items"][0]["id"] == cid

    # Update
    upd = await client.patch(
        f"/api/v1/public/contacts/{cid}", headers=api_headers, json={"title": "Analyst"}
    )
    assert upd.status_code == 200 and upd.json()["title"] == "Analyst"

    # Delete
    dele = await client.delete(f"/api/v1/public/contacts/{cid}", headers=api_headers)
    assert dele.status_code == 204
    assert (await client.get(f"/api/v1/public/contacts/{cid}", headers=api_headers)).status_code == 404


@pytest.mark.asyncio
async def test_create_contact_unknown_company_404(client, api_headers):
    resp = await client.post(
        "/api/v1/public/contacts",
        headers=api_headers,
        json={"company_id": str(uuid.uuid4()), "first_name": "X"},
    )
    assert resp.status_code == 404


# --- companies --------------------------------------------------------------


@pytest.mark.asyncio
async def test_companies_crud(client, api_headers):
    created = await client.post(
        "/api/v1/public/companies",
        headers=api_headers,
        json={"name": "Public Co", "source": "public_api", "country": "US"},
    )
    assert created.status_code == 201, created.text
    cid = created.json()["id"]

    got = await client.get(f"/api/v1/public/companies/{cid}", headers=api_headers)
    assert got.status_code == 200 and got.json()["name"] == "Public Co"

    upd = await client.patch(
        f"/api/v1/public/companies/{cid}", headers=api_headers, json={"city": "Detroit"}
    )
    assert upd.status_code == 200 and upd.json()["city"] == "Detroit"

    lst = await client.get("/api/v1/public/companies?q=Public", headers=api_headers)
    assert lst.json()["total"] == 1

    dele = await client.delete(f"/api/v1/public/companies/{cid}", headers=api_headers)
    assert dele.status_code == 204


# --- deals + notes + activity -----------------------------------------------


@pytest.mark.asyncio
async def test_deals_crud_notes_activity(client, api_headers, sample_company):
    created = await client.post(
        "/api/v1/public/deals",
        headers=api_headers,
        json={"company_id": str(sample_company.id), "title": "Big deal", "value": 5000},
    )
    assert created.status_code == 201, created.text
    did = created.json()["id"]

    # Update status → logs a status_changed activity in addition to 'created'.
    upd = await client.patch(
        f"/api/v1/public/deals/{did}", headers=api_headers, json={"status": "won"}
    )
    assert upd.status_code == 200 and upd.json()["status"] == "won"

    # Notes
    note = await client.post(
        f"/api/v1/public/deals/{did}/notes", headers=api_headers, json={"content": "called them"}
    )
    assert note.status_code == 201
    notes = await client.get(f"/api/v1/public/deals/{did}/notes", headers=api_headers)
    assert len(notes.json()) == 1 and notes.json()[0]["content"] == "called them"

    # Activity timeline records both created + status change.
    acts = await client.get(f"/api/v1/public/deals/{did}/activity", headers=api_headers)
    types = {a["activity_type"] for a in acts.json()}
    assert "created" in types and "status_changed" in types

    dele = await client.delete(f"/api/v1/public/deals/{did}", headers=api_headers)
    assert dele.status_code == 204


# --- sequence enrollments ----------------------------------------------------


@pytest.mark.asyncio
async def test_sequences_and_enrollments(client, api_headers, db_session, sample_contact):
    seq = OutreachSequence(id=uuid.uuid4(), name="Public Seq", status="active", org_id=APPREND_ORG_ID)
    db_session.add(seq)
    await db_session.commit()

    seqs = await client.get("/api/v1/public/sequences", headers=api_headers)
    assert seqs.status_code == 200 and any(s["id"] == str(seq.id) for s in seqs.json())

    enr = await client.post(
        "/api/v1/public/enrollments",
        headers=api_headers,
        json={"contact_id": str(sample_contact.id), "sequence_id": str(seq.id)},
    )
    assert enr.status_code == 201, enr.text
    eid = enr.json()["id"]
    assert enr.json()["status"] == "active"

    # Duplicate active enrollment → 409.
    dup = await client.post(
        "/api/v1/public/enrollments",
        headers=api_headers,
        json={"contact_id": str(sample_contact.id), "sequence_id": str(seq.id)},
    )
    assert dup.status_code == 409

    lst = await client.get("/api/v1/public/enrollments", headers=api_headers)
    assert any(e["id"] == eid for e in lst.json())

    cancel = await client.delete(f"/api/v1/public/enrollments/{eid}", headers=api_headers)
    assert cancel.status_code == 200 and cancel.json()["status"] == "paused"


@pytest.mark.asyncio
async def test_enroll_unknown_contact_404(client, api_headers, db_session):
    seq = OutreachSequence(id=uuid.uuid4(), name="S", status="active", org_id=APPREND_ORG_ID)
    db_session.add(seq)
    await db_session.commit()
    resp = await client.post(
        "/api/v1/public/enrollments",
        headers=api_headers,
        json={"contact_id": str(uuid.uuid4()), "sequence_id": str(seq.id)},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_enrolling_into_a_paused_sequence_is_refused(
    client, api_headers, db_session, sample_contact
):
    """The same refusal ``POST /outreach/enrollments`` gives.

    Pausing a campaign is an operator decision to stop sending it. A second
    enrollment endpoint that doesn't know that is how the decision gets worked
    around by an integration script nobody remembers writing — the contact is
    added, the queue holds it, and the campaign silently resumes with a backlog
    the moment anyone un-pauses it.
    """
    seq = OutreachSequence(
        id=uuid.uuid4(), name="Shelved Seq", status="paused",
        is_active=False, org_id=APPREND_ORG_ID,
    )
    db_session.add(seq)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/public/enrollments",
        headers=api_headers,
        json={"contact_id": str(sample_contact.id), "sequence_id": str(seq.id)},
    )
    assert resp.status_code == 400, resp.text
    assert "not active" in resp.json()["detail"]


# --- cross-org isolation -----------------------------------------------------


@pytest_asyncio.fixture
async def other_org_with_data(db_session):
    """A second org with one company, plus a full-scope key for it."""
    org = Organization(id=uuid.uuid4(), name="Rival", slug="rival-inc", is_active=True)
    db_session.add(org)
    company = Company(
        id=uuid.uuid4(), name="Rival Manufacturing", source="seed",
        country="US", org_id=org.id,
    )
    db_session.add(company)
    await db_session.commit()
    raw = await _mint_key(db_session, org.id, ALL_SCOPES)
    return org, company, {"X-API-Key": raw}


@pytest.mark.asyncio
async def test_key_only_sees_own_org(client, api_headers, sample_company, other_org_with_data):
    """The Apprend key sees Apprend's company, never the rival's, and vice-versa."""
    _, rival_company, rival_headers = other_org_with_data

    # Apprend key: its list has the sample company, not the rival's.
    apprend_list = (await client.get("/api/v1/public/companies", headers=api_headers)).json()
    ids = {c["id"] for c in apprend_list["items"]}
    assert str(sample_company.id) in ids
    assert str(rival_company.id) not in ids

    # Apprend key cannot fetch the rival's company by id (404, not a leak).
    leak = await client.get(f"/api/v1/public/companies/{rival_company.id}", headers=api_headers)
    assert leak.status_code == 404

    # Rival key: mirror check.
    rival_list = (await client.get("/api/v1/public/companies", headers=rival_headers)).json()
    rival_ids = {c["id"] for c in rival_list["items"]}
    assert str(rival_company.id) in rival_ids
    assert str(sample_company.id) not in rival_ids


@pytest.mark.asyncio
async def test_cross_org_write_isolation(client, db_session, other_org_with_data, sample_company):
    """A rival key cannot create a contact under an Apprend company (404)."""
    _, _, rival_headers = other_org_with_data

    # sample_company belongs to the Apprend org; the rival key must not reach it.
    resp = await client.post(
        "/api/v1/public/contacts",
        headers=rival_headers,
        json={"company_id": str(sample_company.id), "first_name": "Spy"},
    )
    assert resp.status_code == 404
