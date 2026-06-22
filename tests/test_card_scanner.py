"""End-to-end tests for the business-card scanner save/retrieve flow.

Covers the three reported bugs:
  1. The scanned card image is stored and retrievable.
  2. Multiple phone numbers (office/mobile/fax/extra) are all saved.
  3. The full address and every other extracted field is persisted + returned.
"""

import base64

import pytest
from httpx import AsyncClient

# 1x1 transparent PNG
_PNG = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
).decode()
CARD_IMAGE = f"data:image/png;base64,{_PNG}"


def _full_payload():
    return {
        "first_name": "Jane",
        "last_name": "Doe",
        "title": "VP of Sales",
        "department": "Sales",
        "company_name": "Acme Robotics",
        "email": "jane.doe@acme.com",
        "phone": "(555) 100-2000",
        "mobile": "555-100-3000",
        "fax": "555-100-4000",
        "additional_phones": [
            {"label": "Office", "number": "(555) 100-2000"},  # dup of phone
            {"label": "Mobile", "number": "555-100-3000"},    # dup of mobile
            {"label": "Direct", "number": "+1 555 100 5000"},
            {"label": "Toll-free", "number": "1-800-555-9999"},
        ],
        "website": "https://acme.com",
        "linkedin_url": "https://linkedin.com/in/janedoe",
        "address": "123 Main St, Suite 400",
        "city": "Austin",
        "state": "TX",
        "postal_code": "78701",
        "country": "USA",
        "notes": "Met at trade show",
        "card_image": CARD_IMAGE,
        "source": "card_scan",
    }


@pytest.mark.asyncio
async def test_quick_add_persists_all_card_fields(client: AsyncClient):
    resp = await client.post("/api/v1/contacts/quick-add", json=_full_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    c = body["contact"]

    # Issue 3: all detail fields persisted on the contact.
    assert c["first_name"] == "Jane"
    assert c["department"] == "Sales"
    assert c["title"] == "VP of Sales"
    assert c["email"] == "jane.doe@acme.com"
    assert c["website"] == "https://acme.com"
    assert c["linkedin_url"] == "https://linkedin.com/in/janedoe"
    assert c["address"] == "123 Main St, Suite 400"
    assert c["city"] == "Austin"
    assert c["state"] == "TX"
    assert c["postal_code"] == "78701"
    assert c["country"] == "USA"
    assert c["notes"] == "Met at trade show"
    assert c["source"] == "card_scan"

    # Issue 1: image was saved (flag set; blob excluded from this response).
    assert c["has_card_image"] is True
    assert "card_image" not in c  # heavy blob not in list/quick-add payload

    # Issue 2: every distinct phone number kept (deduped to 4), dedicated slots set.
    assert c["phone"] == "(555) 100-2000"
    assert c["mobile"] == "555-100-3000"
    assert c["fax"] == "555-100-4000"
    numbers = [p["number"] for p in c["additional_phones"]]
    assert numbers == ["(555) 100-2000", "555-100-3000", "555-100-4000", "+1 555 100 5000", "1-800-555-9999"]


@pytest.mark.asyncio
async def test_detail_endpoint_returns_card_image_and_fields(client: AsyncClient):
    created = (await client.post("/api/v1/contacts/quick-add", json=_full_payload())).json()
    cid = created["contact"]["id"]

    resp = await client.get(f"/api/v1/contacts/{cid}")
    assert resp.status_code == 200, resp.text
    detail = resp.json()

    # Issue 1: the detail view exposes the stored image as a data URL.
    assert detail["card_image"] == CARD_IMAGE
    assert detail["has_card_image"] is True
    # Full address available for display.
    assert detail["address"] == "123 Main St, Suite 400"
    assert detail["postal_code"] == "78701"
    assert len(detail["additional_phones"]) == 5


@pytest.mark.asyncio
async def test_card_image_raw_endpoint_returns_decoded_bytes(client: AsyncClient):
    created = (await client.post("/api/v1/contacts/quick-add", json=_full_payload())).json()
    cid = created["contact"]["id"]

    resp = await client.get(f"/api/v1/contacts/{cid}/card-image")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == base64.b64decode(_PNG)


@pytest.mark.asyncio
async def test_card_image_404_when_absent(client: AsyncClient):
    payload = _full_payload()
    payload.pop("card_image")
    created = (await client.post("/api/v1/contacts/quick-add", json=payload)).json()
    cid = created["contact"]["id"]

    resp = await client.get(f"/api/v1/contacts/{cid}/card-image")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_excludes_heavy_image_blob(client: AsyncClient):
    await client.post("/api/v1/contacts/quick-add", json=_full_payload())

    resp = await client.get("/api/v1/contacts/")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 1
    for it in items:
        # Deferred blob is never shipped in bulk; only the flag is.
        assert "card_image" not in it
        assert "has_card_image" in it


@pytest.mark.asyncio
async def test_bad_email_does_not_block_save(client: AsyncClient):
    """An imperfect OCR email must not 422 the whole save (issue: partial reads)."""
    payload = _full_payload()
    payload["email"] = "not-an-email"
    resp = await client.post("/api/v1/contacts/quick-add", json=payload)
    assert resp.status_code == 201
    c = resp.json()["contact"]
    assert c["email"] is None
    # Other fields still saved.
    assert c["address"] == "123 Main St, Suite 400"
