"""Tests for the auth router: login, refresh, change-password, and /me."""

import pytest
from httpx import AsyncClient

from app.security import create_access_token, create_refresh_token

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
CHANGE_PW = "/api/v1/auth/change-password"
ME = "/api/v1/auth/me"


# ─── Login ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_success_returns_token_pair(client: AsyncClient, agent_user):
    resp = await client.post(LOGIN, json={"email": "agent@landline.app", "password": "agentpass123"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["token_type"] == "bearer"
    assert data["must_change_password"] is False


@pytest.mark.asyncio
async def test_login_email_is_case_insensitive(client: AsyncClient, agent_user):
    resp = await client.post(LOGIN, json={"email": "AGENT@Landline.APP", "password": "agentpass123"})
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_login_wrong_password_401(client: AsyncClient, agent_user):
    resp = await client.post(LOGIN, json={"email": "agent@landline.app", "password": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_unknown_user_401(client: AsyncClient):
    resp = await client.post(LOGIN, json={"email": "ghost@landline.app", "password": "whatever"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_inactive_user_403(client: AsyncClient, make_user):
    await make_user("off@landline.app", password="secret123", is_active=False)
    resp = await client.post(LOGIN, json={"email": "off@landline.app", "password": "secret123"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_login_flags_forced_password_change(client: AsyncClient, make_user):
    await make_user("new@landline.app", password="temp1234", must_change_password=True)
    resp = await client.post(LOGIN, json={"email": "new@landline.app", "password": "temp1234"})
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is True


# ─── /me ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_returns_profile(client: AsyncClient, agent_user, auth_headers):
    resp = await client.get(ME, headers=auth_headers(agent_user))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["email"] == "agent@landline.app"
    assert data["role"] == "agent"
    assert "password_hash" not in data


@pytest.mark.asyncio
async def test_me_without_token_401(client: AsyncClient):
    assert (await client.get(ME)).status_code == 401


@pytest.mark.asyncio
async def test_me_with_garbage_token_401(client: AsyncClient):
    resp = await client.get(ME, headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_token_rejected_as_access_on_me(client: AsyncClient, agent_user):
    # A refresh token must not authenticate a normal (access-token) request.
    refresh = create_refresh_token(agent_user.id, agent_user.role)
    resp = await client.get(ME, headers={"Authorization": f"Bearer {refresh}"})
    assert resp.status_code == 401


# ─── Refresh ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refresh_success(client: AsyncClient, agent_user):
    login = await client.post(LOGIN, json={"email": "agent@landline.app", "password": "agentpass123"})
    refresh_token = login.json()["refresh_token"]

    resp = await client.post(REFRESH, json={"refresh_token": refresh_token})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["access_token"]
    assert data["refresh_token"]


@pytest.mark.asyncio
async def test_refresh_rejects_access_token(client: AsyncClient, agent_user):
    access = create_access_token(agent_user.id, agent_user.role)
    resp = await client.post(REFRESH, json={"refresh_token": access})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_rejects_garbage(client: AsyncClient):
    resp = await client.post(REFRESH, json={"refresh_token": "garbage"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_new_access_token_works(client: AsyncClient, agent_user):
    login = await client.post(LOGIN, json={"email": "agent@landline.app", "password": "agentpass123"})
    refreshed = await client.post(REFRESH, json={"refresh_token": login.json()["refresh_token"]})
    new_access = refreshed.json()["access_token"]
    me = await client.get(ME, headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200


# ─── Change password ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_change_password_success_and_clears_flag(client: AsyncClient, make_user, auth_headers):
    user = await make_user("first@landline.app", password="oldpass123", must_change_password=True)
    resp = await client.post(
        CHANGE_PW,
        headers=auth_headers(user),
        json={"current_password": "oldpass123", "new_password": "brandnew456"},
    )
    assert resp.status_code == 200, resp.text

    # Old password no longer works; new one does; the forced-change flag cleared.
    assert (await client.post(LOGIN, json={"email": "first@landline.app", "password": "oldpass123"})).status_code == 401
    ok = await client.post(LOGIN, json={"email": "first@landline.app", "password": "brandnew456"})
    assert ok.status_code == 200
    assert ok.json()["must_change_password"] is False


@pytest.mark.asyncio
async def test_change_password_wrong_current_400(client: AsyncClient, agent_user, auth_headers):
    resp = await client.post(
        CHANGE_PW,
        headers=auth_headers(agent_user),
        json={"current_password": "WRONG", "new_password": "brandnew456"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_change_password_same_as_current_400(client: AsyncClient, agent_user, auth_headers):
    resp = await client.post(
        CHANGE_PW,
        headers=auth_headers(agent_user),
        json={"current_password": "agentpass123", "new_password": "agentpass123"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_change_password_too_short_422(client: AsyncClient, agent_user, auth_headers):
    resp = await client.post(
        CHANGE_PW,
        headers=auth_headers(agent_user),
        json={"current_password": "agentpass123", "new_password": "short"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_change_password_requires_auth_401(client: AsyncClient):
    resp = await client.post(
        CHANGE_PW,
        json={"current_password": "x", "new_password": "brandnew456"},
    )
    assert resp.status_code == 401
