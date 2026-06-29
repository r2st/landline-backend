"""Tests for genuine/test contact grouping (migration 031 + API filters)."""

import uuid as _uuid

import pytest
from httpx import AsyncClient

from app.models.knowledge import IndustryContact
from app.services.contact_grouping import GENUINE, TEST, classify_group


# --- classify_group heuristic (pure) -------------------------------------------

@pytest.mark.parametrize(
    "email,source,name,expected",
    [
        # Real business contacts -> genuine.
        ("mtorres@precisionmetalworks.com", None, "Michael Torres", GENUINE),
        ("amra@amraplastic.ca", "quick-add", None, GENUINE),
        ("bao.nguyen@atmachining.us", "card_scan", "Bao Nguyen", GENUINE),
        # "test" only as a substring of a real domain must NOT flag (granite-STATE).
        ("rhart@granitestateprecision.com", None, "Rebecca Hart", GENUINE),
        # "test" as a delimited token in the email -> test.
        ("test@acme.com", None, "QA Bot", TEST),
        ("john+test@acme.com", "manual", "John Doe", TEST),
        # Developer's own / test addresses -> test.
        ("suman.onenet@gmail.com", None, "Raj Patel", TEST),
        ("sumaninster7@gmail.com", "quick-add", "Subhendu Das", TEST),
        ("suman.das@apprend.tech", "quick-add", "Suman Das", TEST),
        # Test markers on source / name / email.
        ("real@acme.com", "test-v6", "Sarah Williams", TEST),
        ("real@acme.com", "manual", "Alex TestClient", TEST),
        ("someone@example.com", None, None, TEST),
        ("fake.person@fakemail.com", None, None, TEST),
        # Empty / unknown -> genuine (default).
        (None, None, None, GENUINE),
        ("", "manual", "", GENUINE),
    ],
)
def test_classify_group(email, source, name, expected):
    assert classify_group(email=email, source=source, name=name) == expected


# --- Contacts API: auto-classification + filtering -----------------------------

async def _make_company(client: AsyncClient, name="Precision Metal Works") -> str:
    resp = await client.post(
        "/api/v1/companies/",
        json={"name": name, "country": "US", "source": "test"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def test_create_contact_auto_classifies_genuine(client: AsyncClient):
    company_id = await _make_company(client)
    resp = await client.post(
        "/api/v1/contacts/",
        json={
            "company_id": company_id,
            "first_name": "Michael",
            "last_name": "Torres",
            "email": "mtorres@precisionmetalworks.com",
            "source": "manual",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["contact_group"] == GENUINE


async def test_create_contact_auto_classifies_test(client: AsyncClient):
    company_id = await _make_company(client)
    resp = await client.post(
        "/api/v1/contacts/",
        json={
            "company_id": company_id,
            "first_name": "Raj",
            "last_name": "Patel",
            "email": "suman.onenet@gmail.com",
            "source": "test-v4",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["contact_group"] == TEST


async def test_create_contact_respects_explicit_group(client: AsyncClient):
    company_id = await _make_company(client)
    # A test-looking email but explicitly marked genuine must be honoured.
    resp = await client.post(
        "/api/v1/contacts/",
        json={
            "company_id": company_id,
            "email": "suman.onenet@gmail.com",
            "contact_group": "genuine",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["contact_group"] == GENUINE


async def test_quick_add_auto_classifies(client: AsyncClient):
    genuine = await client.post(
        "/api/v1/contacts/quick-add",
        json={
            "first_name": "Bao",
            "last_name": "Nguyen",
            "email": "bao.nguyen@atmachining.us",
            "company_name": "Advanced Tech Machining",
            "source": "card_scan",
        },
    )
    assert genuine.status_code == 201, genuine.text
    assert genuine.json()["contact"]["contact_group"] == GENUINE

    test = await client.post(
        "/api/v1/contacts/quick-add",
        json={
            "first_name": "Suman",
            "last_name": "Das",
            "email": "suman.das@apprend.tech",
            "company_name": "Apprend Technologies",
            "source": "quick-add",
        },
    )
    assert test.status_code == 201, test.text
    assert test.json()["contact"]["contact_group"] == TEST


async def test_list_contacts_filter_by_group(client: AsyncClient):
    company_id = await _make_company(client)
    await client.post("/api/v1/contacts/", json={
        "company_id": company_id, "email": "real@precisionmetalworks.com", "source": "manual",
    })
    await client.post("/api/v1/contacts/", json={
        "company_id": company_id, "email": "suman.onenet@gmail.com", "source": "test-v6",
    })

    genuine = await client.get("/api/v1/contacts/", params={"group": "genuine"})
    assert genuine.status_code == 200
    g_items = genuine.json()["items"]
    assert len(g_items) == 1
    assert g_items[0]["email"] == "real@precisionmetalworks.com"
    assert all(c["contact_group"] == "genuine" for c in g_items)

    test = await client.get("/api/v1/contacts/", params={"group": "test"})
    assert test.status_code == 200
    t_items = test.json()["items"]
    assert len(t_items) == 1
    assert t_items[0]["email"] == "suman.onenet@gmail.com"
    assert all(c["contact_group"] == "test" for c in t_items)


# --- Knowledge (industry) contacts: default genuine + filtering ----------------

async def test_create_industry_contact_defaults_genuine(client: AsyncClient):
    resp = await client.post("/api/v1/knowledge/contacts", json={
        "industry": "CNC Machining",
        "company_name": "Swiss-Tech LLC",
        "contact_email": "mike.ledestich@swisstechllc.com",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["contact_group"] == GENUINE


async def test_knowledge_contacts_filter_by_group(client: AsyncClient, db_session):
    db_session.add_all([
        IndustryContact(
            id=_uuid.uuid4(), industry="CNC Machining", company_name="Real Co",
            contact_email="a@realco.com", contact_group="genuine",
        ),
        IndustryContact(
            id=_uuid.uuid4(), industry="CNC Machining", company_name="Test Co",
            contact_email="b@example.com", contact_group="test",
        ),
    ])
    await db_session.commit()

    genuine = await client.get("/api/v1/knowledge/contacts", params={"group": "genuine"})
    assert genuine.status_code == 200
    g = genuine.json()
    assert len(g) == 1 and g[0]["company_name"] == "Real Co"

    test = await client.get("/api/v1/knowledge/contacts", params={"group": "test"})
    assert test.status_code == 200
    t = test.json()
    assert len(t) == 1 and t[0]["company_name"] == "Test Co"
