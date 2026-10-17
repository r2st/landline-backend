"""Tests for company API endpoints."""

import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_check(client: AsyncClient):
    """Test the health endpoint."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["app"] == "DoAide Prospect"


@pytest.mark.asyncio
async def test_create_company(client: AsyncClient):
    """Test creating a company via the API."""
    payload = {
        "name": "Test Manufacturing Inc.",
        "domain": "testmfg.com",
        "city": "Detroit",
        "state_province": "MI",
        "country": "US",
        "employee_count": 200,
        "naics_code": "3321",
        "source": "api_test",
    }
    response = await client.post("/api/v1/companies/", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Test Manufacturing Inc."
    assert data["domain"] == "testmfg.com"
    assert data["stage"] == "new"
    assert "id" in data


@pytest.mark.asyncio
async def test_list_companies(client: AsyncClient):
    """Test listing companies with pagination."""
    # Create two companies
    for i in range(2):
        await client.post("/api/v1/companies/", json={
            "name": f"Company {i}",
            "country": "US",
            "source": "test",
        })

    response = await client.get("/api/v1/companies/")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2


@pytest.mark.asyncio
async def test_search_companies(client: AsyncClient):
    """Test searching companies by name."""
    await client.post("/api/v1/companies/", json={
        "name": "Acme Widgets Corp",
        "country": "US",
        "source": "test",
    })
    await client.post("/api/v1/companies/", json={
        "name": "Beta Industries",
        "country": "CA",
        "source": "test",
    })

    response = await client.get("/api/v1/companies/", params={"q": "Acme"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert any("Acme" in item["name"] for item in data["items"])


@pytest.mark.asyncio
async def test_filter_by_country(client: AsyncClient):
    """Test filtering companies by country."""
    await client.post("/api/v1/companies/", json={
        "name": "US Company",
        "country": "US",
        "source": "test",
    })
    await client.post("/api/v1/companies/", json={
        "name": "CA Company",
        "country": "CA",
        "source": "test",
    })

    response = await client.get("/api/v1/companies/", params={"country": "CA"})
    assert response.status_code == 200
    data = response.json()
    assert all(item["country"] == "CA" for item in data["items"])


@pytest.mark.asyncio
async def test_get_company(client: AsyncClient):
    """Test getting a single company by ID."""
    create_resp = await client.post("/api/v1/companies/", json={
        "name": "Get Test Co",
        "country": "US",
        "source": "test",
    })
    company_id = create_resp.json()["id"]

    response = await client.get(f"/api/v1/companies/{company_id}")
    assert response.status_code == 200
    assert response.json()["name"] == "Get Test Co"


@pytest.mark.asyncio
async def test_get_company_not_found(client: AsyncClient):
    """Test 404 for non-existent company."""
    import uuid
    response = await client.get(f"/api/v1/companies/{uuid.uuid4()}")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_update_company(client: AsyncClient):
    """Test updating a company."""
    create_resp = await client.post("/api/v1/companies/", json={
        "name": "Update Test Co",
        "country": "US",
        "source": "test",
    })
    company_id = create_resp.json()["id"]

    response = await client.patch(f"/api/v1/companies/{company_id}", json={
        "employee_count": 500,
        "city": "Chicago",
    })
    assert response.status_code == 200
    data = response.json()
    assert data["employee_count"] == 500
    assert data["city"] == "Chicago"


@pytest.mark.asyncio
async def test_update_company_stage(client: AsyncClient):
    """Test transitioning a company's pipeline stage."""
    create_resp = await client.post("/api/v1/companies/", json={
        "name": "Stage Test Co",
        "country": "US",
        "source": "test",
    })
    company_id = create_resp.json()["id"]
    assert create_resp.json()["stage"] == "new"

    response = await client.patch(
        f"/api/v1/companies/{company_id}/stage",
        json={"stage": "researched"},
    )
    assert response.status_code == 200
    assert response.json()["stage"] == "researched"


@pytest.mark.asyncio
async def test_delete_company(client: AsyncClient):
    """Test deleting a company."""
    create_resp = await client.post("/api/v1/companies/", json={
        "name": "Delete Test Co",
        "country": "US",
        "source": "test",
    })
    company_id = create_resp.json()["id"]

    response = await client.delete(f"/api/v1/companies/{company_id}")
    assert response.status_code == 204

    # Verify it's gone
    response = await client.get(f"/api/v1/companies/{company_id}")
    assert response.status_code == 404
