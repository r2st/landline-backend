"""Tests for self-service profile updates and the admin password-reset endpoint."""

import uuid

import pytest
from httpx import AsyncClient

ME = "/api/v1/auth/me"
USERS = "/api/v1/admin/users"


# ─── Self-service profile update: PATCH /auth/me ─────────────────────────────

@pytest.mark.asyncio
async def test_update_own_name(client: AsyncClient, agent_user, auth_headers):
    resp = await client.patch(ME, json={"name": "Renamed Agent"}, headers=auth_headers(agent_user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed Agent"
    # /me reflects the change.
    me = await client.get(ME, headers=auth_headers(agent_user))
    assert me.json()["name"] == "Renamed Agent"


@pytest.mark.asyncio
async def test_update_own_email(client: AsyncClient, agent_user, auth_headers):
    resp = await client.patch(
        ME, json={"email": "renamed@landline.app"}, headers=auth_headers(agent_user)
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "renamed@landline.app"
    # Can log in with the new email.
    login = await client.post(
        "/api/v1/auth/login", json={"email": "renamed@landline.app", "password": "agentpass123"}
    )
    assert login.status_code == 200


@pytest.mark.asyncio
async def test_update_email_conflict(client: AsyncClient, agent_user, admin_user, auth_headers):
    resp = await client.patch(
        ME, json={"email": admin_user.email}, headers=auth_headers(agent_user)
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_profile_update_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.patch(ME, json={"name": "Nobody"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_profile_update_cannot_change_role(client: AsyncClient, agent_user, auth_headers):
    """Role isn't part of the profile schema, so it is silently ignored."""
    resp = await client.patch(
        ME, json={"name": "Still Agent", "role": "admin"}, headers=auth_headers(agent_user)
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "org_agent"


# ─── Admin password reset: POST /admin/users/{id}/reset-password ─────────────

@pytest.mark.asyncio
async def test_admin_reset_password_endpoint(client: AsyncClient, admin_user, agent_user, auth_headers):
    resp = await client.post(
        f"{USERS}/{agent_user.id}/reset-password",
        json={"new_password": "brandnew123"},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_password"] is True

    # Agent logs in with the new password; response signals forced change.
    login = await client.post(
        "/api/v1/auth/login", json={"email": agent_user.email, "password": "brandnew123"}
    )
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True


@pytest.mark.asyncio
async def test_admin_reset_password_no_force(client: AsyncClient, admin_user, agent_user, auth_headers):
    resp = await client.post(
        f"{USERS}/{agent_user.id}/reset-password",
        json={"new_password": "brandnew123", "must_change_password": False},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["must_change_password"] is False


@pytest.mark.asyncio
async def test_reset_password_forbidden_for_agent(client: AsyncClient, agent_user, manager_user, auth_headers):
    resp = await client.post(
        f"{USERS}/{manager_user.id}/reset-password",
        json={"new_password": "hacked123456"},
        headers=auth_headers(agent_user),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reset_password_unknown_user_404(client: AsyncClient, admin_user, auth_headers):
    resp = await client.post(
        f"{USERS}/{uuid.uuid4()}/reset-password",
        json={"new_password": "whatever123"},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reset_password_too_short_422(client: AsyncClient, admin_user, agent_user, auth_headers):
    resp = await client.post(
        f"{USERS}/{agent_user.id}/reset-password",
        json={"new_password": "short"},
        headers=auth_headers(admin_user),
    )
    assert resp.status_code == 422
