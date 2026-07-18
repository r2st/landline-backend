"""Platform Admin API tests (SaaS multi-tenancy, Phase 2).

Covers the cross-org "god view" restricted to ``platform_admin``:
* role gating — org_admin / org_agent are denied every route;
* organization CRUD (create, list-with-counts, detail, update, soft-delete);
* platform-wide stats roll up across all orgs;
* impersonation mints a token that actually scopes to the target user's org.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.email_template import EmailSend
from app.models.tenant import Organization
from app.models.user import UserRole
from app.org_context import APPREND_ORG_ID
from app.security import decode_token


@pytest_asyncio.fixture
async def platform_admin(make_user):
    """A platform admin — org_id NULL, exempt from org isolation."""
    return await make_user(
        "root@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None
    )


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Beta Industries", slug="beta-industries",
        is_active=True, billing_plan="growth",
    )
    db_session.add(org)
    await db_session.commit()
    return org


# --- Role gating -------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_admin_cannot_access_platform_routes(client, make_user, auth_headers):
    """An org_admin is below platform_admin → 403 on every platform route."""
    admin = await make_user("orgadmin@alpha.com", role=UserRole.ORG_ADMIN.value)
    h = auth_headers(admin)
    assert (await client.get("/api/v1/platform/stats", headers=h)).status_code == 403
    assert (await client.get("/api/v1/platform/organizations", headers=h)).status_code == 403
    assert (
        await client.post(
            "/api/v1/platform/organizations", json={"name": "Nope"}, headers=h
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/platform/impersonate/{uuid.uuid4()}", headers=h)
    ).status_code == 403


@pytest.mark.asyncio
async def test_agent_cannot_access_platform_routes(client, make_user, auth_headers):
    agent = await make_user("agent@alpha.com", role=UserRole.ORG_AGENT.value)
    resp = await client.get("/api/v1/platform/organizations", headers=auth_headers(agent))
    assert resp.status_code == 403


# --- Organization CRUD -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_organization(client, platform_admin, auth_headers):
    resp = await client.post(
        "/api/v1/platform/organizations",
        json={"name": "Gamma Corp", "billing_plan": "pro"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Gamma Corp"
    assert body["slug"] == "gamma-corp"  # derived from name
    assert body["billing_plan"] == "pro"
    assert body["is_active"] is True
    assert body["usage"]["user_count"] == 0
    assert body["users"] == []


@pytest.mark.asyncio
async def test_create_organization_rejects_duplicate_slug(client, platform_admin, auth_headers, org_b):
    resp = await client.post(
        "/api/v1/platform/organizations",
        json={"name": "Beta Again", "slug": "beta-industries"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_organization_rejects_bad_plan(client, platform_admin, auth_headers):
    resp = await client.post(
        "/api/v1/platform/organizations",
        json={"name": "Bad Plan Co", "billing_plan": "platinum"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_organizations_with_counts(
    client, db_session, platform_admin, make_user, auth_headers, org_b
):
    """List spans all orgs and reports per-org user + email counts."""
    # Two users in org_b (plus the platform admin, who has no org).
    await make_user("u1@beta.com", org_id=org_b.id)
    await make_user("u2@beta.com", org_id=org_b.id, role=UserRole.ORG_ADMIN.value)
    # One sent email attributed to org_b.
    db_session.add(EmailSend(to_email="x@beta.com", status="sent", org_id=org_b.id))
    await db_session.commit()

    resp = await client.get("/api/v1/platform/organizations", headers=auth_headers(platform_admin))
    assert resp.status_code == 200, resp.text
    by_slug = {o["slug"]: o for o in resp.json()}
    assert "apprend-technologies" in by_slug  # seeded org is visible
    beta = by_slug["beta-industries"]
    assert beta["usage"]["user_count"] == 2
    assert beta["usage"]["emails_sent"] == 1
    # The org_admin is the preferred impersonation target.
    assert beta["impersonatable_user_id"] is not None


@pytest.mark.asyncio
async def test_get_organization_detail(client, platform_admin, make_user, auth_headers, org_b):
    member = await make_user("member@beta.com", org_id=org_b.id)
    resp = await client.get(
        f"/api/v1/platform/organizations/{org_b.id}", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == str(org_b.id)
    emails = {u["email"] for u in body["users"]}
    assert "member@beta.com" in emails


@pytest.mark.asyncio
async def test_get_organization_404(client, platform_admin, auth_headers):
    resp = await client.get(
        f"/api/v1/platform/organizations/{uuid.uuid4()}", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_organization(client, platform_admin, auth_headers, org_b):
    resp = await client.put(
        f"/api/v1/platform/organizations/{org_b.id}",
        json={"name": "Beta Renamed", "billing_plan": "enterprise", "is_active": False},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Beta Renamed"
    assert body["billing_plan"] == "enterprise"
    assert body["is_active"] is False


@pytest.mark.asyncio
async def test_update_organization_slug_conflict(
    client, db_session, platform_admin, auth_headers, org_b
):
    other = Organization(id=uuid.uuid4(), name="Delta", slug="delta-co", is_active=True, billing_plan="free")
    db_session.add(other)
    await db_session.commit()
    resp = await client.put(
        f"/api/v1/platform/organizations/{org_b.id}",
        json={"slug": "delta-co"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_organization(client, platform_admin, auth_headers, org_b):
    resp = await client.delete(
        f"/api/v1/platform/organizations/{org_b.id}", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 200, resp.text
    # Verify it now reads back inactive.
    detail = await client.get(
        f"/api/v1/platform/organizations/{org_b.id}", headers=auth_headers(platform_admin)
    )
    assert detail.json()["is_active"] is False


# --- Platform stats ----------------------------------------------------------


@pytest.mark.asyncio
async def test_platform_stats(client, db_session, platform_admin, make_user, auth_headers, org_b):
    await make_user("s1@beta.com", org_id=org_b.id)
    db_session.add(EmailSend(to_email="a@beta.com", status="sent", org_id=org_b.id))
    db_session.add(EmailSend(to_email="b@apprend.com", status="sent", org_id=APPREND_ORG_ID))
    db_session.add(EmailSend(to_email="c@beta.com", status="failed", org_id=org_b.id))  # not counted
    await db_session.commit()

    resp = await client.get("/api/v1/platform/stats", headers=auth_headers(platform_admin))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Apprend (seeded) + Beta = at least 2 orgs.
    assert body["total_organizations"] >= 2
    assert body["active_organizations"] >= 2
    assert body["total_emails_sent"] == 2  # two 'sent', the 'failed' excluded
    assert body["total_users"] >= 2  # platform admin + s1 + default admin


# --- Impersonation -----------------------------------------------------------


@pytest.mark.asyncio
async def test_impersonate_user_returns_scoped_token(
    client, platform_admin, make_user, auth_headers, org_b
):
    target = await make_user("target@beta.com", org_id=org_b.id, role=UserRole.ORG_AGENT.value)
    resp = await client.post(
        f"/api/v1/platform/impersonate/{target.id}", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_id"] == str(org_b.id)
    assert body["org_name"] == "Beta Industries"
    assert body["email"] == "target@beta.com"
    payload = decode_token(body["access_token"])
    assert payload["sub"] == str(target.id)
    assert payload["org_id"] == str(org_b.id)


@pytest.mark.asyncio
async def test_impersonation_token_scopes_reads(
    client, db_session, platform_admin, make_user, auth_headers, org_b
):
    """Using the impersonation token, the caller sees only the target org's data."""
    target = await make_user("t2@beta.com", org_id=org_b.id)
    db_session.add(Company(name="BetaOnly", domain="betaonly.com", source="test", org_id=org_b.id))
    db_session.add(Company(name="ApprendOnly", domain="apprendonly.com", source="test", org_id=APPREND_ORG_ID))
    await db_session.commit()

    imp = await client.post(
        f"/api/v1/platform/impersonate/{target.id}", headers=auth_headers(platform_admin)
    )
    token = imp.json()["access_token"]
    resp = await client.get(
        "/api/v1/companies/", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text
    names = {c["name"] for c in resp.json()["items"]}
    assert "BetaOnly" in names
    assert "ApprendOnly" not in names


@pytest.mark.asyncio
async def test_impersonate_404_for_unknown_user(client, platform_admin, auth_headers):
    resp = await client.post(
        f"/api/v1/platform/impersonate/{uuid.uuid4()}", headers=auth_headers(platform_admin)
    )
    assert resp.status_code == 404


# --- Platform admin promote / demote ----------------------------------------


@pytest.mark.asyncio
async def test_list_platform_admins(client, platform_admin, make_user, auth_headers):
    """The admins roster returns every platform_admin, not org users."""
    await make_user("orgadmin2@alpha.com", role=UserRole.ORG_ADMIN.value)
    resp = await client.get("/api/v1/platform/admins", headers=auth_headers(platform_admin))
    assert resp.status_code == 200, resp.text
    emails = {u["email"] for u in resp.json()}
    assert "root@landline.app" in emails
    assert "orgadmin2@alpha.com" not in emails
    assert all(u["role"] == "platform_admin" for u in resp.json())


@pytest.mark.asyncio
async def test_promote_admin_success(client, db_session, platform_admin, make_user, auth_headers, org_b):
    """An org user promoted by email becomes platform_admin with org_id cleared."""
    user = await make_user("promote.me@beta.com", role=UserRole.ORG_ADMIN.value, org_id=org_b.id)
    resp = await client.post(
        "/api/v1/platform/promote-admin",
        json={"email": "promote.me@beta.com"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "platform_admin"
    assert body["email"] == "promote.me@beta.com"

    await db_session.refresh(user)
    assert user.role == UserRole.PLATFORM_ADMIN.value
    assert user.org_id is None


@pytest.mark.asyncio
async def test_promote_admin_is_case_insensitive_on_email(client, platform_admin, make_user, auth_headers):
    await make_user("mixed.case@alpha.com", role=UserRole.ORG_AGENT.value)
    resp = await client.post(
        "/api/v1/platform/promote-admin",
        json={"email": "Mixed.Case@Alpha.com"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "platform_admin"


@pytest.mark.asyncio
async def test_promote_admin_user_not_found(client, platform_admin, auth_headers):
    resp = await client.post(
        "/api/v1/platform/promote-admin",
        json={"email": "ghost@nowhere.com"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_promote_admin_already_platform_admin(client, platform_admin, auth_headers):
    resp = await client.post(
        "/api/v1/platform/promote-admin",
        json={"email": "root@landline.app"},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_promote_admin_requires_platform_admin(client, make_user, auth_headers):
    """An org_admin cannot promote anyone — 403."""
    admin = await make_user("orgadmin@alpha.com", role=UserRole.ORG_ADMIN.value)
    await make_user("victim@alpha.com", role=UserRole.ORG_AGENT.value)
    resp = await client.post(
        "/api/v1/platform/promote-admin",
        json={"email": "victim@alpha.com"},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_demote_admin_success(client, db_session, platform_admin, make_user, auth_headers, org_b):
    """Demoting a platform_admin sets org_admin + the chosen org."""
    target = await make_user("demote.me@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None)
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(target.id), "org_id": str(org_b.id)},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "org_admin"

    await db_session.refresh(target)
    assert target.role == UserRole.ORG_ADMIN.value
    assert target.org_id == org_b.id


@pytest.mark.asyncio
async def test_demote_admin_cannot_demote_self(client, platform_admin, auth_headers, org_b):
    """The caller can't demote themselves — always leaves one platform_admin."""
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(platform_admin.id), "org_id": str(org_b.id)},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_demote_admin_not_platform_admin(client, platform_admin, make_user, auth_headers, org_b):
    """Target must currently be a platform_admin — 400 otherwise."""
    target = await make_user("plain@beta.com", role=UserRole.ORG_ADMIN.value, org_id=org_b.id)
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(target.id), "org_id": str(org_b.id)},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_demote_admin_user_not_found(client, platform_admin, auth_headers, org_b):
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(uuid.uuid4()), "org_id": str(org_b.id)},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_demote_admin_org_not_found(client, platform_admin, make_user, auth_headers):
    target = await make_user("demote2@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None)
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(target.id), "org_id": str(uuid.uuid4())},
        headers=auth_headers(platform_admin),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_demote_admin_requires_platform_admin(client, make_user, auth_headers, org_b):
    admin = await make_user("orgadmin3@alpha.com", role=UserRole.ORG_ADMIN.value)
    target = await make_user("demote3@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None)
    resp = await client.post(
        "/api/v1/platform/demote-admin",
        json={"user_id": str(target.id), "org_id": str(org_b.id)},
        headers=auth_headers(admin),
    )
    assert resp.status_code == 403
