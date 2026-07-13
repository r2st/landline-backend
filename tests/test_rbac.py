"""Role-based access control tests.

Two concerns:
  1. Dependency-level RBAC — require_role("admin") on the /admin routes.
  2. The global AuthMiddleware — API-key OR JWT, with backward compatibility.
"""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import AuthMiddleware
from app.security import create_access_token

USERS = "/api/v1/admin/users"


# ─── Dependency-level RBAC on the admin routes ───────────────────────────────

@pytest.mark.asyncio
async def test_admin_route_requires_token(unauth_client: AsyncClient):
    assert (await unauth_client.get(USERS)).status_code == 401


@pytest.mark.asyncio
async def test_agent_forbidden_from_admin_routes(client: AsyncClient, agent_user, auth_headers):
    h = auth_headers(agent_user)
    assert (await client.get(USERS, headers=h)).status_code == 403
    assert (
        await client.post(
            USERS, headers=h,
            json={"email": "z@landline.app", "name": "Z", "role": "agent", "password": "initialpw123"},
        )
    ).status_code == 403


@pytest.mark.asyncio
async def test_manager_forbidden_from_admin_routes(client: AsyncClient, manager_user, auth_headers):
    assert (await client.get(USERS, headers=auth_headers(manager_user))).status_code == 403


@pytest.mark.asyncio
async def test_admin_allowed(client: AsyncClient, admin_user, auth_headers):
    assert (await client.get(USERS, headers=auth_headers(admin_user))).status_code == 200


@pytest.mark.asyncio
async def test_deactivated_user_token_rejected(client: AsyncClient, make_user, auth_headers):
    """A token for a since-deactivated user must not authenticate."""
    user = await make_user("gone@landline.app", role="admin", is_active=False)
    resp = await client.get(USERS, headers=auth_headers(user))
    assert resp.status_code == 401


# ─── AuthMiddleware: API key OR JWT (backward compatibility) ──────────────────

class _FakeSettings:
    api_key = "secret-api-key"
    jwt_secret_key = "unit-test-jwt-secret"
    secret_key = ""
    jwt_algorithm = "HS256"
    access_token_expire_minutes = 30
    refresh_token_expire_days = 7


@pytest.fixture
def mw_transport(monkeypatch):
    fake = _FakeSettings()
    # Both the middleware and the token signer must see the same settings.
    monkeypatch.setattr("app.auth.get_settings", lambda: fake)
    monkeypatch.setattr("app.security.get_settings", lambda: fake)

    app = FastAPI()
    app.add_middleware(AuthMiddleware)

    @app.get("/api/v1/companies/")
    async def _protected():
        return {"ok": True}

    @app.get("/health")
    async def _health():
        return {"ok": True}

    return ASGITransport(app=app)


@pytest.mark.asyncio
async def test_middleware_blocks_unauthenticated(mw_transport):
    async with AsyncClient(transport=mw_transport, base_url="http://test") as ac:
        assert (await ac.get("/api/v1/companies/")).status_code == 401


@pytest.mark.asyncio
async def test_middleware_accepts_api_key(mw_transport):
    async with AsyncClient(transport=mw_transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/companies/", headers={"X-API-Key": "secret-api-key"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_middleware_rejects_wrong_api_key(mw_transport):
    async with AsyncClient(transport=mw_transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/companies/", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_middleware_accepts_jwt_bearer(mw_transport):
    import uuid
    token = create_access_token(uuid.uuid4(), "admin")
    async with AsyncClient(transport=mw_transport, base_url="http://test") as ac:
        resp = await ac.get("/api/v1/companies/", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_middleware_allows_public_health(mw_transport):
    async with AsyncClient(transport=mw_transport, base_url="http://test") as ac:
        assert (await ac.get("/health")).status_code == 200
