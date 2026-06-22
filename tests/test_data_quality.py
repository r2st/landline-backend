"""Tests for data-quality features: domain normalization, contact dedup on
enrichment, and the company-merge endpoint."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.models.company import Company
from app.models.contact import Contact
from app.models.lead_score import LeadScore
from app.services.normalize import normalize_domain


def test_normalize_domain_variants():
    assert normalize_domain("https://www.Acme-Corp.com/about?x=1") == "acme-corp.com"
    assert normalize_domain("HTTP://Acme.com") == "acme.com"
    assert normalize_domain("acme.com:8080") == "acme.com"
    assert normalize_domain("user@acme.com") == "acme.com"
    assert normalize_domain("  ") is None
    assert normalize_domain("not a domain") is None
    assert normalize_domain(None) is None


@pytest.mark.asyncio
async def test_company_create_normalizes_domain(client: AsyncClient):
    resp = await client.post("/api/v1/companies/", json={
        "name": "Normalize Co", "domain": "https://www.NormalizeCo.com/path",
        "source": "test",
    })
    assert resp.status_code == 201, resp.text
    assert resp.json()["domain"] == "normalizeco.com"


@pytest.mark.asyncio
async def test_enrichment_find_contacts_dedups(db_session, sample_company, sample_contact, monkeypatch):
    """find_contacts must skip people whose email already exists on the company."""
    from app.services.enrichment import ApolloEnrichmentService

    service = ApolloEnrichmentService()
    service.api_key = "fake"  # bypass the no-key short-circuit

    async def fake_post(*args, **kwargs):
        class R:
            def raise_for_status(self): ...
            def json(self):
                return {"people": [
                    # Duplicate of the existing sample_contact email.
                    {"first_name": "John", "last_name": "Smith",
                     "email": "john.smith@acmestamp.com", "title": "VP"},
                    # A genuinely new person.
                    {"first_name": "Amy", "last_name": "New",
                     "email": "amy.new@acmestamp.com", "title": "Director"},
                ]}
        return R()

    import httpx

    class FakeClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): ...
        async def post(self, *a, **k): return await fake_post(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    created = await service.find_contacts(db_session, sample_company)
    # Only the new person should be created; the duplicate is skipped.
    assert len(created) == 1
    assert created[0]["email"] == "amy.new@acmestamp.com"

    result = await db_session.execute(
        select(Contact).where(Contact.company_id == sample_company.id)
    )
    emails = sorted(c.email for c in result.scalars().all())
    assert emails == ["amy.new@acmestamp.com", "john.smith@acmestamp.com"]


@pytest.mark.asyncio
async def test_company_merge(client: AsyncClient, db_session):
    # Two duplicate companies, each with a contact and a score.
    survivor = Company(name="Acme Inc", country="US", source="test")
    source = Company(name="Acme Incorporated", country="US", source="test",
                     domain="acme.com", employee_count=200)
    db_session.add_all([survivor, source])
    await db_session.flush()

    db_session.add(Contact(company_id=source.id, first_name="Dup", email="dup@acme.com", source="test"))
    db_session.add(LeadScore(company_id=source.id, score_type="rule_based", fit_score=60))
    await db_session.flush()
    survivor_id, source_id = survivor.id, source.id
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/companies/{survivor_id}/merge",
        json={"source_id": str(source_id)},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["contacts_moved"] == 1
    assert body["scores_moved"] == 1

    # Source is gone; survivor inherited the source's domain + employee_count.
    gone = await client.get(f"/api/v1/companies/{source_id}")
    assert gone.status_code == 404
    surv = await client.get(f"/api/v1/companies/{survivor_id}")
    assert surv.json()["domain"] == "acme.com"
    assert surv.json()["employee_count"] == 200
    assert surv.json()["contact_count"] == 1


@pytest.mark.asyncio
async def test_company_merge_into_self_rejected(client: AsyncClient, sample_company):
    resp = await client.post(
        f"/api/v1/companies/{sample_company.id}/merge",
        json={"source_id": str(sample_company.id)},
    )
    assert resp.status_code == 400
