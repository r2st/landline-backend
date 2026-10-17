"""Tests for authentication endpoints and user management."""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.org_context import APPREND_ORG_ID
from app.security import hash_password, create_access_token, create_refresh_token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user."""
    user = User(
        id=uuid.uuid4(),
        email="admin@test.com",
        name="Test Admin",
        role="admin",
        password_hash=hash_password("admin123"),
        is_active=True,
        org_id=APPREND_ORG_ID,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def agent_user(db_session: AsyncSession) -> User:
    """Create an agent user."""
    user = User(
        id=uuid.uuid4(),
        email="agent@test.com",
        name="Test Agent",
        role="agent",
        password_hash=hash_password("agent123"),
        is_active=True,
        org_id=APPREND_ORG_ID,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def inactive_user(db_session: AsyncSession) -> User:
    """Create an inactive user."""
    user = User(
        id=uuid.uuid4(),
        email="inactive@test.com",
        name="Inactive User",
        role="agent",
        password_hash=hash_password("password"),
        is_active=False,
        org_id=APPREND_ORG_ID,
    )
    db_session.add(user)
    await db_session.flush()
    return user


def _auth_header(user: User) -> dict:
    """Build an Authorization header for a user."""
    token = create_access_token(user.id, user.role)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient, admin_user: User):
    resp = await client.post("/api/v1/auth/login", json={
        "email": "admin@test.com",
        "password": "admin123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["token_type"] == "bearer"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient, admin_user: User):
    resp = await client.post("/api/v1/auth/login", json={
        "email": "admin@test.com",
        "password": "wrong",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_nonexistent_user(client: AsyncClient):
    resp = await client.post("/api/v1/auth/login", json={
        "email": "nobody@test.com",
        "password": "password",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_inactive_user(client: AsyncClient, inactive_user: User):
    resp = await client.post("/api/v1/auth/login", json={
        "email": "inactive@test.com",
        "password": "password",
    })
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /auth/refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token(client: AsyncClient, admin_user: User):
    # Login first
    login_resp = await client.post("/api/v1/auth/login", json={
        "email": "admin@test.com",
        "password": "admin123",
    })
    refresh_token = login_resp.json()["refresh_token"]

    # Refresh
    resp = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": refresh_token,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert "refresh_token" in data


@pytest.mark.asyncio
async def test_refresh_with_invalid_token(client: AsyncClient):
    resp = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": "invalid.token.here",
    })
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_with_access_token_fails(client: AsyncClient, admin_user: User):
    """An access token should not work as a refresh token."""
    access = create_access_token(admin_user.id, admin_user.role)
    resp = await client.post("/api/v1/auth/refresh", json={
        "refresh_token": access,
    })
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_me_authenticated(client: AsyncClient, agent_user: User):
    resp = await client.get("/api/v1/auth/me", headers=_auth_header(agent_user))
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "agent@test.com"
    assert data["role"] == "agent"
    assert data["name"] == "Test Agent"


@pytest.mark.asyncio
async def test_me_returns_each_callers_own_email(
    client: AsyncClient, admin_user: User, agent_user: User
):
    """The Settings profile renders /auth/me's email; two users logged in
    against one deployment must each get their own address back."""
    for user in (admin_user, agent_user):
        resp = await client.get("/api/v1/auth/me", headers=_auth_header(user))
        assert resp.status_code == 200
        assert resp.json()["email"] == user.email
        assert resp.json()["id"] == str(user.id)


@pytest.mark.asyncio
async def test_me_no_token(unauth_client: AsyncClient):
    resp = await unauth_client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_invalid_token(client: AsyncClient):
    resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid.token"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/change-password
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_password(client: AsyncClient, agent_user: User):
    resp = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "agent123", "new_password": "newpass456"},
        headers=_auth_header(agent_user),
    )
    assert resp.status_code == 200

    # Login with new password
    login_resp = await client.post("/api/v1/auth/login", json={
        "email": "agent@test.com",
        "password": "newpass456",
    })
    assert login_resp.status_code == 200


@pytest.mark.asyncio
async def test_change_password_wrong_current(client: AsyncClient, agent_user: User):
    resp = await client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrongpass", "new_password": "newpass456"},
        headers=_auth_header(agent_user),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin: GET /admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_list_users(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.get("/api/v1/admin/users", headers=_auth_header(admin_user))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2
    emails = {u["email"] for u in data}
    assert "admin@test.com" in emails
    assert "agent@test.com" in emails


@pytest.mark.asyncio
async def test_admin_list_users_forbidden_for_agent(client: AsyncClient, agent_user: User):
    resp = await client.get("/api/v1/admin/users", headers=_auth_header(agent_user))
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Admin: POST /admin/users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_create_user(client: AsyncClient, admin_user: User):
    resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": "newagent@test.com",
            "name": "New Agent",
            "role": "org_agent",
            "password": "secret123",
        },
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == "newagent@test.com"
    assert data["role"] == "org_agent"
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_admin_create_duplicate_email(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.post(
        "/api/v1/admin/users",
        json={
            "email": "agent@test.com",
            "name": "Duplicate",
            "role": "agent",
            "password": "secret123",
        },
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Admin: PUT /admin/users/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_update_user(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{agent_user.id}",
        json={"name": "Updated Agent", "role": "org_manager"},
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Agent"
    assert data["role"] == "org_manager"


@pytest.mark.asyncio
async def test_admin_deactivate_user(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{agent_user.id}",
        json={"is_active": False},
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_admin_reset_password(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{agent_user.id}",
        json={"password": "resetpass123", "must_change_password": True},
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 200

    # Agent can now login with new password
    login_resp = await client.post("/api/v1/auth/login", json={
        "email": "agent@test.com",
        "password": "resetpass123",
    })
    assert login_resp.status_code == 200


# ---------------------------------------------------------------------------
# Admin: DELETE /admin/users/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_delete_user(client: AsyncClient, admin_user: User, agent_user: User):
    # DELETE is a soft-delete (deactivate) so attribution history is preserved.
    resp = await client.delete(
        f"/api/v1/admin/users/{agent_user.id}",
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_admin_cannot_delete_self(client: AsyncClient, admin_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{admin_user.id}",
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_admin_delete_nonexistent(client: AsyncClient, admin_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{uuid.uuid4()}",
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Admin: GET /admin/users/{id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_get_user(client: AsyncClient, admin_user: User, agent_user: User):
    resp = await client.get(
        f"/api/v1/admin/users/{agent_user.id}",
        headers=_auth_header(admin_user),
    )
    assert resp.status_code == 200
    assert resp.json()["email"] == "agent@test.com"


# ---------------------------------------------------------------------------
# User-scoped task filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tasks_scoped_by_user(
    unauth_client: AsyncClient, admin_user: User, agent_user: User, sample_company
):
    """Tasks created by different users should be scoped correctly.

    Uses ``unauth_client`` (no default token) so each call's auth is explicit
    and the final unauthenticated request exercises the fail-closed path.
    """
    client = unauth_client
    # Create task as agent
    resp1 = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": str(sample_company.id),
            "title": "Agent's task",
            "priority": "high",
        },
        headers=_auth_header(agent_user),
    )
    assert resp1.status_code == 201

    # Create task as admin
    resp2 = await client.post(
        "/api/v1/tasks",
        json={
            "company_id": str(sample_company.id),
            "title": "Admin's task",
            "priority": "low",
        },
        headers=_auth_header(admin_user),
    )
    assert resp2.status_code == 201

    # Agent should only see their own task
    agent_tasks = await client.get("/api/v1/tasks", headers=_auth_header(agent_user))
    assert agent_tasks.status_code == 200
    agent_data = agent_tasks.json()
    assert len(agent_data) == 1
    assert agent_data[0]["title"] == "Agent's task"

    # Admin should only see their own task
    admin_tasks = await client.get("/api/v1/tasks", headers=_auth_header(admin_user))
    assert admin_tasks.status_code == 200
    admin_data = admin_tasks.json()
    assert len(admin_data) == 1
    assert admin_data[0]["title"] == "Admin's task"

    # Fail closed: an unauthenticated request (no API key, no token) is rejected.
    all_tasks = await client.get("/api/v1/tasks")
    assert all_tasks.status_code == 401


# ---------------------------------------------------------------------------
# User-scoped follow-up filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_follow_ups_scoped_by_user(
    client: AsyncClient, admin_user: User, agent_user: User, sample_company
):
    """Follow-ups created by different users should be scoped correctly."""
    from datetime import timedelta

    due = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()

    # Create follow-up as agent
    resp1 = await client.post(
        "/api/v1/follow-ups",
        json={
            "company_id": str(sample_company.id),
            "due_date": due,
            "reason": "Agent follow-up",
        },
        headers=_auth_header(agent_user),
    )
    assert resp1.status_code == 201

    # Create follow-up as admin
    resp2 = await client.post(
        "/api/v1/follow-ups",
        json={
            "company_id": str(sample_company.id),
            "due_date": due,
            "reason": "Admin follow-up",
        },
        headers=_auth_header(admin_user),
    )
    assert resp2.status_code == 201

    # Agent should only see their own
    agent_fups = await client.get("/api/v1/follow-ups", headers=_auth_header(agent_user))
    assert agent_fups.status_code == 200
    agent_data = agent_fups.json()
    assert len(agent_data) == 1
    assert agent_data[0]["reason"] == "Agent follow-up"


# ---------------------------------------------------------------------------
# Security utility tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_password_hashing():
    from app.security import hash_password, verify_password
    hashed = hash_password("test123")
    assert verify_password("test123", hashed)
    assert not verify_password("wrong", hashed)


@pytest.mark.asyncio
async def test_jwt_token_creation_and_decode():
    from app.security import create_access_token, create_refresh_token, decode_token
    uid = uuid.uuid4()
    access = create_access_token(uid, "admin")
    refresh = create_refresh_token(uid)

    access_payload = decode_token(access)
    assert access_payload["sub"] == str(uid)
    assert access_payload["type"] == "access"
    assert access_payload["role"] == "admin"

    refresh_payload = decode_token(refresh)
    assert refresh_payload["sub"] == str(uid)
    assert refresh_payload["type"] == "refresh"
