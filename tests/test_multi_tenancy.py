"""Multi-tenancy foundation tests (migration 078).

Covers the four pillars of Phase 1:
* org-scoped reads/writes are isolated (an org A user can't see org B data);
* the role hierarchy (``org_agent`` can't do ``org_admin`` actions);
* JWTs carry ``org_id``;
* migration/backfill invariants (the Apprend org id is a single source of truth,
  and new rows default to the caller's org).
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import OrgScope
from app.models.company import Company
from app.models.tenant import Organization
from app.models.user import User, UserRole, normalize_role
from app.org_context import (
    APPREND_ORG_ID,
    get_current_org_id,
    org_id_for_insert,
    reset_current_org_id,
    set_current_org_id,
)
from app.security import create_access_token, decode_token


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    """A second organization, distinct from the seeded Apprend org."""
    org = Organization(
        id=uuid.uuid4(), name="Beta Industries", slug="beta-industries",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


def _company(name: str, org_id) -> Company:
    return Company(name=name, domain=f"{name.lower()}.com", source="test", org_id=org_id)


# --- 1. Data isolation -------------------------------------------------------

@pytest.mark.asyncio
async def test_reads_are_isolated_by_org(db_session: AsyncSession, org_b: Organization):
    """With an org bound, a SELECT only returns that org's rows."""
    db_session.add(_company("AlphaCo", APPREND_ORG_ID))
    db_session.add(_company("BetaCo", org_b.id))
    await db_session.commit()

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        names = {c.name for c in (await db_session.execute(select(Company))).scalars()}
    finally:
        reset_current_org_id(token)
    assert names == {"AlphaCo"}

    token = set_current_org_id(org_b.id)
    try:
        names = {c.name for c in (await db_session.execute(select(Company))).scalars()}
    finally:
        reset_current_org_id(token)
    assert names == {"BetaCo"}


@pytest.mark.asyncio
async def test_no_org_context_sees_all(db_session: AsyncSession, org_b: Organization):
    """With no org bound (platform admin / background), the filter is a no-op."""
    db_session.add(_company("AlphaCo", APPREND_ORG_ID))
    db_session.add(_company("BetaCo", org_b.id))
    await db_session.commit()

    assert get_current_org_id() is None
    names = {c.name for c in (await db_session.execute(select(Company))).scalars()}
    assert {"AlphaCo", "BetaCo"} <= names


@pytest.mark.asyncio
async def test_writes_default_to_bound_org(db_session: AsyncSession, org_b: Organization):
    """A row created with no explicit org_id is stamped with the bound org."""
    token = set_current_org_id(org_b.id)
    try:
        c = Company(name="Ctx", domain="ctx.com", source="test")  # no org_id
        db_session.add(c)
        await db_session.flush()
    finally:
        reset_current_org_id(token)
    assert c.org_id == org_b.id


@pytest.mark.asyncio
async def test_write_without_context_falls_back_to_apprend(db_session: AsyncSession):
    """No org context → new rows belong to the first-party Apprend org."""
    assert get_current_org_id() is None
    c = Company(name="Fallback", domain="fb.com", source="test")
    db_session.add(c)
    await db_session.flush()
    assert c.org_id == APPREND_ORG_ID


@pytest.mark.asyncio
async def test_api_isolation_between_orgs(client: AsyncClient, db_session, make_user, auth_headers, org_b):
    """A user in org A cannot see org B's companies through the API."""
    user_a = await make_user("a@alpha-corp.com", org_id=APPREND_ORG_ID)
    user_b = await make_user("b@beta-corp.com", org_id=org_b.id)
    db_session.add(_company("AlphaVisible", APPREND_ORG_ID))
    db_session.add(_company("BetaVisible", org_b.id))
    await db_session.commit()

    resp_a = await client.get("/api/v1/companies/", headers=auth_headers(user_a))
    assert resp_a.status_code == 200, resp_a.text
    names_a = {c["name"] for c in resp_a.json()["items"]}
    assert "AlphaVisible" in names_a
    assert "BetaVisible" not in names_a

    resp_b = await client.get("/api/v1/companies/", headers=auth_headers(user_b))
    names_b = {c["name"] for c in resp_b.json()["items"]}
    assert "BetaVisible" in names_b
    assert "AlphaVisible" not in names_b


# --- 2. Role hierarchy -------------------------------------------------------

def _scope(role: str, org_id=APPREND_ORG_ID) -> OrgScope:
    user = User(id=uuid.uuid4(), email="x@x.com", name="X", role=role, password_hash="x", org_id=org_id)
    return OrgScope(user, None)


def test_role_hierarchy_ranks():
    assert UserRole.rank("org_agent") < UserRole.rank("org_manager")
    assert UserRole.rank("org_manager") < UserRole.rank("org_admin")
    assert UserRole.rank("org_admin") < UserRole.rank("platform_admin")


def test_check_permission_denies_lower_role():
    scope = _scope(UserRole.ORG_AGENT.value)
    with pytest.raises(Exception) as exc:
        scope.check_permission(UserRole.ORG_ADMIN.value)
    assert getattr(exc.value, "status_code", None) == 403


def test_check_permission_allows_equal_or_higher():
    _scope(UserRole.ORG_ADMIN.value).check_permission(UserRole.ORG_MANAGER.value)  # no raise
    _scope(UserRole.PLATFORM_ADMIN.value).check_permission(UserRole.ORG_ADMIN.value)  # no raise


def test_legacy_role_normalization():
    assert normalize_role("admin") == "org_admin"
    assert normalize_role("manager") == "org_manager"
    assert normalize_role("agent") == "org_agent"
    assert normalize_role("platform_admin") == "platform_admin"


@pytest.mark.asyncio
async def test_agent_cannot_create_users(client: AsyncClient, make_user, auth_headers):
    """An org_agent is below the org_admin bar for user management → 403."""
    agent = await make_user("agent2@alpha-corp.com", role=UserRole.ORG_AGENT.value)
    resp = await client.post(
        "/api/v1/admin/users",
        json={"email": "new@alpha-corp.com", "name": "New", "role": "org_agent", "password": "pw123456"},
        headers=auth_headers(agent),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_create_users_in_own_org(client: AsyncClient, make_user, auth_headers):
    admin = await make_user("admin2@alpha-corp.com", role=UserRole.ORG_ADMIN.value)
    resp = await client.post(
        "/api/v1/admin/users",
        json={"email": "created@alpha-corp.com", "name": "Created", "role": "org_agent", "password": "pw123456"},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["org_id"] == str(admin.org_id)


# --- 3. JWT carries org_id ---------------------------------------------------

def test_jwt_includes_org_id():
    uid = uuid.uuid4()
    org = uuid.uuid4()
    payload = decode_token(create_access_token(uid, "org_admin", org))
    assert payload["sub"] == str(uid)
    assert payload["role"] == "org_admin"
    assert payload["org_id"] == str(org)


def test_jwt_platform_admin_has_null_org():
    payload = decode_token(create_access_token(uuid.uuid4(), "platform_admin", None))
    assert payload["org_id"] is None


@pytest.mark.asyncio
async def test_login_returns_org_info(client: AsyncClient, make_user):
    await make_user("login@alpha-corp.com", password="secret123", org_id=APPREND_ORG_ID)
    resp = await client.post("/api/v1/auth/login", json={"email": "login@alpha-corp.com", "password": "secret123"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == str(APPREND_ORG_ID)
    assert body["org_name"] == "Apprend Technologies"
    assert body["role"] == UserRole.ORG_AGENT.value


# --- 4. Migration / backfill invariants --------------------------------------

def test_migration_org_id_matches_app_constant():
    """The migration's hard-coded Apprend id must match the app constant, or the
    server_default and the seeded row would disagree."""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions" / "078_multi_tenancy.py"
    spec = importlib.util.spec_from_file_location("mig078", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.APPREND_ORG_ID == str(APPREND_ORG_ID)
    assert mod.APPREND_ORG_SLUG == "apprend-technologies"
    # 67 tables get a new org_id column; the migration must not silently shrink.
    assert len(mod.NEW_ORG_TABLES) == 67


def test_org_id_for_insert_helper():
    assert org_id_for_insert() == APPREND_ORG_ID  # no context
    token = set_current_org_id(uuid.uuid4())
    try:
        bound = get_current_org_id()
        assert org_id_for_insert() == bound
    finally:
        reset_current_org_id(token)
