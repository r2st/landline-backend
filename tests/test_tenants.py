"""Tests for the multi-tenant white-label platform (feature 4.1).

Covers the Tenant/TenantUsage models, tenant config resolution, usage-based
billing aggregation, and the admin tenant API + current-tenant endpoints.
"""

import uuid

import pytest

from app.models.tenant import PLAN_AI_CALL_LIMITS, Tenant
from app.models.user import User, UserRole
from app.security import create_access_token, hash_password
from app.services.tenant_service import estimate_cost, get_tenant_service


# NOTE ON TEST ORDER: the shared conftest uses a session-scoped event loop, and
# running plain sync tests *before* async-DB tests in the same file destabilizes
# the per-test DB fixtures (see reference: landline test-suite ordering). So all
# async DB/API tests come first and the pure sync unit tests live at the bottom.


# --- Model / service async tests ------------------------------------------


@pytest.mark.asyncio
async def test_record_usage_and_summary(db_session):
    tenant = Tenant(id=uuid.uuid4(), name="Acme", slug="acme", billing_plan="starter")
    db_session.add(tenant)
    await db_session.flush()

    svc = get_tenant_service()
    await svc.record_ai_usage(
        db_session, tenant.id, "email_generation", "claude-sonnet-4-6",
        input_tokens=1000, output_tokens=500,
    )
    await svc.record_ai_usage(
        db_session, tenant.id, "workflow", "claude-sonnet-4-6",
        input_tokens=2000, output_tokens=0,
    )
    await db_session.flush()

    summary = await svc.usage_summary(db_session, tenant)
    assert summary["ai_calls"] == 2
    assert summary["input_tokens"] == 3000
    assert summary["output_tokens"] == 500
    assert summary["ai_call_limit"] == PLAN_AI_CALL_LIMITS["starter"]
    assert summary["over_limit"] is False
    assert "email_generation" in summary["by_feature"]
    assert summary["by_feature"]["workflow"]["calls"] == 1


@pytest.mark.asyncio
async def test_record_usage_noop_for_default_tenant(db_session):
    svc = get_tenant_service()
    result = await svc.record_ai_usage(
        db_session, None, "email_generation", "claude-sonnet-4-6", 100, 100
    )
    assert result is None


@pytest.mark.asyncio
async def test_within_ai_limit(db_session):
    tenant = Tenant(id=uuid.uuid4(), name="Tiny", slug="tiny", billing_plan="free")
    db_session.add(tenant)
    await db_session.flush()
    svc = get_tenant_service()
    assert await svc.within_ai_limit(db_session, tenant) is True
    # None tenant (default) is always within limit.
    assert await svc.within_ai_limit(db_session, None) is True


# --- API tests -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_get_tenant(client):
    resp = await client.post(
        "/api/v1/tenants",
        json={
            "name": "Acme Corp",
            "slug": "acme-corp",
            "billing_plan": "growth",
            "branding": {"primary_color": "#ff0000"},
            "config": {"enabled_verticals": ["cnc_machining"]},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["slug"] == "acme-corp"
    assert data["effective_ai_call_limit"] == PLAN_AI_CALL_LIMITS["growth"]
    tenant_id = data["id"]

    got = await client.get(f"/api/v1/tenants/{tenant_id}")
    assert got.status_code == 200
    assert got.json()["branding"]["primary_color"] == "#ff0000"


@pytest.mark.asyncio
async def test_create_tenant_rejects_bad_slug_and_dup(client):
    bad = await client.post("/api/v1/tenants", json={"name": "X", "slug": "Bad Slug!"})
    assert bad.status_code == 400

    await client.post("/api/v1/tenants", json={"name": "One", "slug": "dup"})
    dup = await client.post("/api/v1/tenants", json={"name": "Two", "slug": "dup"})
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_update_tenant_config_and_plan(client):
    created = await client.post("/api/v1/tenants", json={"name": "Acme", "slug": "acme2"})
    tid = created.json()["id"]

    resp = await client.patch(
        f"/api/v1/tenants/{tid}",
        json={"billing_plan": "enterprise", "config": {"feature_flags": {"workflows": True}}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["billing_plan"] == "enterprise"
    assert body["effective_ai_call_limit"] is None
    assert body["config"]["feature_flags"]["workflows"] is True


@pytest.mark.asyncio
async def test_deactivate_tenant(client):
    created = await client.post("/api/v1/tenants", json={"name": "Acme", "slug": "acme3"})
    tid = created.json()["id"]
    resp = await client.delete(f"/api/v1/tenants/{tid}")
    assert resp.status_code == 200
    got = await client.get(f"/api/v1/tenants/{tid}")
    assert got.json()["is_active"] is False


@pytest.mark.asyncio
async def test_non_admin_cannot_manage_tenants(client, db_engine):
    # Seed an agent user and call with their token.
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        agent = User(
            id=uuid.uuid4(), email="agent@acme.test", name="Agent",
            role=UserRole.AGENT.value, password_hash=hash_password("x"), is_active=True,
        )
        s.add(agent)
        await s.commit()
        token = create_access_token(agent.id, agent.role)

    resp = await client.get("/api/v1/tenants", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_current_config_default_for_user_without_org(client):
    resp = await client.get("/api/v1/tenants/current/config")
    assert resp.status_code == 200
    assert resp.json()["default"] is True


@pytest.mark.asyncio
async def test_current_config_resolves_tenant(client, db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        tenant = Tenant(
            id=uuid.uuid4(), name="Branded", slug="branded",
            branding={"company_name": "Branded Inc"},
            config={"feature_flags": {"workflows": True}},
        )
        s.add(tenant)
        await s.flush()
        user = User(
            id=uuid.uuid4(), email="u@branded.test", name="U",
            role=UserRole.MANAGER.value, password_hash=hash_password("x"),
            is_active=True, org_id=tenant.id,
        )
        s.add(user)
        await s.commit()
        token = create_access_token(user.id, user.role)

    resp = await client.get(
        "/api/v1/tenants/current/config", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["default"] is False
    assert body["branding"]["company_name"] == "Branded Inc"
    assert body["feature_flags"]["workflows"] is True


# --- Pure sync unit tests (kept last — see TEST ORDER note above) ----------


def test_estimate_cost_prices_known_and_unknown_models():
    # Sonnet: $3/Mtok in, $15/Mtok out.
    assert estimate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000) == pytest.approx(18.0)
    # Unknown model falls back to the sonnet-tier default.
    assert estimate_cost("mystery-model", 1_000_000, 0) == pytest.approx(3.0)


def test_effective_ai_limit_uses_plan_default():
    t = Tenant(name="Acme", slug="acme", billing_plan="starter")
    assert t.effective_ai_call_limit == PLAN_AI_CALL_LIMITS["starter"]
    t.ai_monthly_call_limit = 42
    assert t.effective_ai_call_limit == 42
    ent = Tenant(name="Ent", slug="ent", billing_plan="enterprise")
    assert ent.effective_ai_call_limit is None


def test_tenant_config_helpers():
    t = Tenant(
        name="Acme",
        slug="acme",
        config={
            "enabled_verticals": ["cnc_machining"],
            "ai_prompts": {"email_generation": "Always mention our ISO cert."},
            "feature_flags": {"workflows": False},
        },
    )
    assert t.enabled_verticals() == ["cnc_machining"]
    assert t.ai_prompt_override("email_generation").startswith("Always mention")
    assert t.ai_prompt_override("missing") is None
