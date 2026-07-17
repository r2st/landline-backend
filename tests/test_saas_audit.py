"""Tests for the SaaS-readiness audit fixes.

Covers the multi-tenant gaps closed in the audit:

* public organization registration (POST /auth/register),
* organization-deactivation lockout (login, refresh, and every authenticated
  request),
* per-organization system settings (the kill switch is no longer global),
* the background-task org helpers (org_scope / active_org_ids) that back the
  per-org Celery sweeps.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.tenant import Organization
from app.models.company import Company
from app.models.user import User, UserRole
from app.org_context import org_scope
from app.security import create_access_token
from app.services import system_settings
from app.tasks.org_runner import active_org_ids


@pytest_asyncio.fixture
async def two_orgs(db_session):
    """Two active organizations, A and B."""
    a = Organization(id=uuid.uuid4(), name="Org A", slug="org-a", is_active=True)
    b = Organization(id=uuid.uuid4(), name="Org B", slug="org-b", is_active=True)
    db_session.add_all([a, b])
    await db_session.commit()
    return a, b


# --- Registration ----------------------------------------------------------


@pytest.mark.asyncio
async def test_register_creates_org_and_admin(unauth_client, db_session):
    resp = await unauth_client.post(
        "/api/v1/auth/register",
        json={
            "org_name": "Widgets Inc",
            "admin_email": "founder@widgets.example.com",
            "password": "supersecret1",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["access_token"]
    assert body["refresh_token"]
    assert body["role"] == UserRole.ORG_ADMIN.value
    assert body["org_id"] is not None
    assert body["org_name"] == "Widgets Inc"

    # The org and admin actually exist and are wired together.
    org = (
        await db_session.execute(select(Organization).where(Organization.slug == "widgets-inc"))
    ).scalar_one_or_none()
    assert org is not None and org.is_active
    user = (
        await db_session.execute(select(User).where(User.email == "founder@widgets.example.com"))
    ).scalar_one_or_none()
    assert user is not None
    assert user.role == UserRole.ORG_ADMIN.value
    assert user.org_id == org.id

    # The returned token authenticates.
    me = await unauth_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == "founder@widgets.example.com"


@pytest.mark.asyncio
async def test_register_duplicate_email_conflicts(unauth_client):
    payload = {
        "org_name": "First Co",
        "admin_email": "dup@x.example.com",
        "password": "supersecret1",
    }
    first = await unauth_client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201
    second = await unauth_client.post(
        "/api/v1/auth/register",
        json={**payload, "org_name": "Second Co"},
    )
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_register_slugs_are_unique(unauth_client, db_session):
    for email in ("a@same.example.com", "b@same.example.com"):
        resp = await unauth_client.post(
            "/api/v1/auth/register",
            json={"org_name": "Same Name", "admin_email": email, "password": "supersecret1"},
        )
        assert resp.status_code == 201, resp.text
    slugs = (
        await db_session.execute(
            select(Organization.slug).where(Organization.name == "Same Name")
        )
    ).scalars().all()
    assert len(slugs) == 2
    assert len(set(slugs)) == 2  # collision resolved


# --- Organization deactivation --------------------------------------------


@pytest.mark.asyncio
async def test_deactivated_org_blocks_login(unauth_client, db_session, make_user):
    org = Organization(id=uuid.uuid4(), name="Dead Co", slug="dead-co", is_active=False)
    db_session.add(org)
    await db_session.commit()
    await make_user("member@dead.example.com", password="password123", org_id=org.id,
                    role=UserRole.ORG_ADMIN.value)

    resp = await unauth_client.post(
        "/api/v1/auth/login",
        json={"email": "member@dead.example.com", "password": "password123"},
    )
    assert resp.status_code == 403
    assert "deactivated" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_deactivated_org_blocks_authenticated_requests(unauth_client, db_session, make_user):
    org = Organization(id=uuid.uuid4(), name="Frozen Co", slug="frozen-co", is_active=False)
    db_session.add(org)
    await db_session.commit()
    user = await make_user("u@frozen.example.com", org_id=org.id, role=UserRole.ORG_ADMIN.value)

    # A token minted before deactivation must stop working everywhere.
    token = create_access_token(user.id, user.role, user.org_id)
    resp = await unauth_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 403
    assert "deactivated" in resp.json()["detail"].lower()


# --- Per-org system settings ----------------------------------------------


@pytest.mark.asyncio
async def test_system_settings_are_per_org(db_session, two_orgs):
    a, b = two_orgs
    # Org A pauses sending; org B is untouched.
    async with org_scope(a.id):
        await system_settings.set_value(
            db_session, system_settings.SENDING_ENABLED_KEY, {"enabled": False}
        )
    await db_session.commit()

    async with org_scope(a.id):
        assert await system_settings.is_sending_enabled(db_session) is False
    async with org_scope(b.id):
        # B never configured it → falls back to the default, NOT A's row.
        assert await system_settings.is_sending_enabled(db_session) is True

    # Both orgs can hold their OWN row for the same key (composite unique).
    async with org_scope(b.id):
        await system_settings.set_value(
            db_session, system_settings.SENDING_ENABLED_KEY, {"enabled": True}
        )
    await db_session.commit()
    rows = (
        await db_session.execute(
            select(system_settings.SystemSetting)
            .where(system_settings.SystemSetting.key == system_settings.SENDING_ENABLED_KEY)
            .execution_options(skip_org_filter=True)
        )
    ).scalars().all()
    assert {r.org_id for r in rows} == {a.id, b.id}


# --- Background-task org helpers -------------------------------------------


@pytest.mark.asyncio
async def test_active_org_ids_excludes_inactive(db_session):
    active = Organization(id=uuid.uuid4(), name="On", slug="on", is_active=True)
    inactive = Organization(id=uuid.uuid4(), name="Off", slug="off", is_active=False)
    db_session.add_all([active, inactive])
    await db_session.commit()

    ids = await active_org_ids(db_session)
    assert active.id in ids
    assert inactive.id not in ids


@pytest.mark.asyncio
async def test_org_scope_filters_reads(db_session, two_orgs):
    a, b = two_orgs
    # org_id set explicitly (org-stamping happens at flush time, which here is
    # the commit below — outside the scope); this test targets the READ filter.
    db_session.add(Company(id=uuid.uuid4(), name="A-Corp", source="test", org_id=a.id))
    db_session.add(Company(id=uuid.uuid4(), name="B-Corp", source="test", org_id=b.id))
    await db_session.commit()

    async with org_scope(a.id):
        names = (await db_session.execute(select(Company.name))).scalars().all()
    assert "A-Corp" in names
    assert "B-Corp" not in names  # B's company is filtered out under A's scope
