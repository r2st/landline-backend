"""Tests for recipient email validation (syntax + MX pre-check)."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.services import email_validation


@pytest.fixture(autouse=True)
def _clear_mx_cache():
    email_validation.clear_cache()
    yield
    email_validation.clear_cache()


def test_syntax_rejects_garbage():
    assert email_validation.is_valid_syntax("not-an-email") is False
    assert email_validation.is_valid_syntax("a@b") is False  # no dot in domain
    assert email_validation.is_valid_syntax("john@example.com") is True


def test_validate_email_invalid_syntax_short_circuits(monkeypatch):
    # MX check must not even run on bad syntax.
    called = {"n": 0}
    monkeypatch.setattr(email_validation, "check_mx", lambda d: called.__setitem__("n", called["n"] + 1) or "valid")
    assert email_validation.validate_email("bogus") == "invalid"
    assert called["n"] == 0


def test_validate_email_uses_mx(monkeypatch):
    monkeypatch.setattr(email_validation, "check_mx", lambda d: "valid")
    assert email_validation.validate_email("a@good.com") == "valid"
    monkeypatch.setattr(email_validation, "check_mx", lambda d: "invalid")
    assert email_validation.validate_email("a@nomx.com") == "invalid"


def test_check_mx_caches(monkeypatch):
    calls = {"n": 0}

    def fake_resolver():  # not used; we test cache short-circuit directly
        calls["n"] += 1

    # Seed the cache and confirm a second call doesn't recompute.
    email_validation._MX_CACHE["cached.com"] = "valid"
    assert email_validation.check_mx("cached.com") == "valid"


@pytest.mark.asyncio
async def test_validate_contact_persists_status(db_session: AsyncSession, sample_contact: Contact, monkeypatch):
    monkeypatch.setattr(email_validation, "check_mx", lambda d: "invalid")
    status = await email_validation.validate_contact(db_session, sample_contact)
    assert status == "invalid"
    assert sample_contact.email_status == "invalid"
    assert sample_contact.email_checked_at is not None


@pytest.mark.asyncio
async def test_validate_endpoint(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(email_validation, "check_mx", lambda d: "valid")
    resp = await client.post("/api/v1/email/validate", json={"email": "ceo@acme.com"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "valid"

    resp = await client.post("/api/v1/email/validate", json={"email": "garbage"})
    assert resp.json()["status"] == "invalid"


@pytest.mark.asyncio
async def test_validate_contacts_bulk_marks(client: AsyncClient, db_session: AsyncSession, sample_company, monkeypatch):
    monkeypatch.setattr(email_validation, "check_mx", lambda d: "invalid" if "dead" in d else "valid")
    good = Contact(id=uuid.uuid4(), company_id=sample_company.id, email="a@live.com", source="test")
    bad = Contact(id=uuid.uuid4(), company_id=sample_company.id, email="b@dead.example", source="test")
    db_session.add_all([good, bad])
    await db_session.commit()

    resp = await client.post("/api/v1/email/validate-contacts?limit=50")
    assert resp.status_code == 200
    body = resp.json()
    assert body["checked"] >= 2
    assert body["invalid"] >= 1
    assert body["valid"] >= 1
