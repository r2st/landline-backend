"""Tests for contextual knowledge surfacing:

- /knowledge/surface and /knowledge/surface/company/{id} (router).
- surface_for_industry / surface_for_company (service).

The router (async, client-backed) tests are defined first so the app/client
fixtures own the event loop and dependency overrides before the plain sync
service tests run (conftest's shared session-scoped loop is order-sensitive).
"""

import uuid

import pytest
from httpx import AsyncClient

from app.models.company import Company
from app.services.knowledge import surface_for_industry, surface_for_company


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_endpoint(client: AsyncClient):
    resp = await client.get(
        "/api/v1/knowledge/surface", params={"industry": "injection_molding"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["industry"]["key"] == "injection_molding"
    assert data["objection_handling"]
    assert data["profile"]["key_metrics"]


@pytest.mark.asyncio
async def test_surface_company_endpoint(client: AsyncClient, db_session):
    company = Company(
        id=uuid.uuid4(), name="Precision CNC Co",
        industry_segment="CNC Machining", country="US", source="test",
    )
    db_session.add(company)
    await db_session.commit()

    resp = await client.get(f"/api/v1/knowledge/surface/company/{company.id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["company"]["name"] == "Precision CNC Co"
    assert data["industry"]["key"] == "cnc_machining"


@pytest.mark.asyncio
async def test_surface_company_endpoint_404(client: AsyncClient):
    resp = await client.get(f"/api/v1/knowledge/surface/company/{uuid.uuid4()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# surface_for_industry (pure)
# ---------------------------------------------------------------------------


class TestSurfaceForIndustry:
    def test_matches_injection_molding_by_freetext(self):
        data = surface_for_industry("Plastic Injection Molding")
        assert data["industry"]["key"] == "injection_molding"
        assert data["industry"]["matched"] is True
        assert data["pain_points"]  # non-empty
        # Profile is populated for a known vertical.
        assert data["profile"]["key_certifications"]
        assert data["profile"]["emotional_triggers"].get("effective")

    def test_matches_by_machine_key(self):
        data = surface_for_industry("cnc_machining")
        assert data["industry"]["key"] == "cnc_machining"
        assert data["profile"]["technical_vocabulary"]

    def test_includes_objection_handling(self):
        data = surface_for_industry("injection_molding")
        assert isinstance(data["objection_handling"], list)
        assert data["objection_handling"]  # the cold-call script has rebuttals
        first = data["objection_handling"][0]
        assert "objection" in first and "rebuttal" in first

    def test_includes_compose_principles(self):
        data = surface_for_industry("metal_stamping")
        assert "reciprocity" in data["principles"]
        assert "social_proof" in data["principles"]

    def test_unknown_industry_falls_back(self):
        data = surface_for_industry("underwater basket weaving")
        # Falls back to the default vertical, flagged as not a real match.
        assert data["industry"]["matched"] is False
        assert data["industry"]["key"]  # still resolves to something
        assert data["pain_points"]

    def test_empty_industry_falls_back(self):
        data = surface_for_industry("")
        assert data["industry"]["matched"] is False


def test_surface_for_company_adds_company_block():
    company = Company(
        id=uuid.uuid4(), name="Acme Molding",
        industry_segment="Plastic Injection Molding", source="test",
    )
    data = surface_for_company(company)
    assert data["company"]["name"] == "Acme Molding"
    assert data["industry"]["key"] == "injection_molding"
    assert data["pain_points"]
