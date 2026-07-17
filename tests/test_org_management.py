"""Tests for organization self-service management (/api/v1/org/*, Phase 3).

Covers settings read/update, team member invite/role-change/removal, usage
aggregation, the org_admin write gate, and cross-org isolation. The org is
always resolved from the caller's JWT — never a path param — so the isolation
tests assert an org A admin cannot touch org B, and vice versa.
"""

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.tenant import Organization
from app.models.user import User, UserRole
from app.org_context import APPREND_ORG_ID
from app.services.org_service import get_org_service

SETTINGS = "/api/v1/org/settings"
MEMBERS = "/api/v1/org/members"
USAGE = "/api/v1/org/usage"


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    """A second organization distinct from the seeded Apprend (org A)."""
    org = Organization(
        id=uuid.uuid4(), name="Beta Industries", slug="beta-industries",
        is_active=True, billing_plan="starter", industry="metal_stamping",
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest_asyncio.fixture
async def org_admin(make_user) -> User:
    return await make_user(
        "owner@acme.io", role=UserRole.ORG_ADMIN.value, name="Org Owner",
        org_id=APPREND_ORG_ID,
    )


@pytest_asyncio.fixture
async def org_agent(make_user) -> User:
    return await make_user(
        "rep@acme.io", role=UserRole.ORG_AGENT.value, name="Rep One",
        org_id=APPREND_ORG_ID,
    )


@pytest_asyncio.fixture
async def org_b_admin(make_user, org_b) -> User:
    return await make_user(
        "owner@beta.io", role=UserRole.ORG_ADMIN.value, name="Beta Owner",
        org_id=org_b.id,
    )


# --- Settings ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_settings_returns_callers_org(client: AsyncClient, org_agent, auth_headers):
    resp = await client.get(SETTINGS, headers=auth_headers(org_agent))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == str(APPREND_ORG_ID)
    assert data["slug"] == "apprend-technologies"
    assert "effective_ai_call_limit" in data


@pytest.mark.asyncio
async def test_update_settings_as_admin(client: AsyncClient, org_admin, auth_headers):
    resp = await client.put(
        SETTINGS,
        headers=auth_headers(org_admin),
        json={"name": "Apprend Renamed", "industry": "cnc_machining",
              "branding": {"primary_color": "#0a7"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "Apprend Renamed"
    assert data["industry"] == "cnc_machining"
    assert data["branding"]["primary_color"] == "#0a7"


@pytest.mark.asyncio
async def test_update_settings_denied_for_agent(client: AsyncClient, org_agent, auth_headers):
    resp = await client.put(
        SETTINGS, headers=auth_headers(org_agent), json={"name": "Hacked"}
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_settings_400_for_platform_admin(client: AsyncClient, make_user, auth_headers):
    platform = await make_user(
        "root@landline.app", role=UserRole.PLATFORM_ADMIN.value, name="Root", org_id=None
    )
    resp = await client.get(SETTINGS, headers=auth_headers(platform))
    assert resp.status_code == 400, resp.text


# --- Members -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_members(client: AsyncClient, org_admin, org_agent, auth_headers):
    resp = await client.get(MEMBERS, headers=auth_headers(org_admin))
    assert resp.status_code == 200, resp.text
    emails = {m["email"] for m in resp.json()}
    assert {"owner@acme.io", "rep@acme.io"} <= emails


@pytest.mark.asyncio
async def test_agent_can_list_members(client: AsyncClient, org_admin, org_agent, auth_headers):
    """Reads are open to any member, including agents."""
    resp = await client.get(MEMBERS, headers=auth_headers(org_agent))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_invite_member(client: AsyncClient, org_admin, auth_headers):
    resp = await client.post(
        f"{MEMBERS}/invite",
        headers=auth_headers(org_admin),
        json={"email": "new@acme.io", "name": "New Hire", "role": "org_manager"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["email"] == "new@acme.io"
    assert data["role"] == "org_manager"
    assert data["org_id"] == str(APPREND_ORG_ID)
    assert data["must_change_password"] is True
    assert data["created_by"] == str(org_admin.id)
    # The temporary password is returned exactly once so the admin can relay it.
    assert len(data["temporary_password"]) >= 8


@pytest.mark.asyncio
async def test_invited_member_can_log_in(client: AsyncClient, org_admin, auth_headers):
    invite = await client.post(
        f"{MEMBERS}/invite",
        headers=auth_headers(org_admin),
        json={"email": "login@acme.io", "name": "Log In", "role": "org_agent"},
    )
    temp = invite.json()["temporary_password"]
    login = await client.post(
        "/api/v1/auth/login", json={"email": "login@acme.io", "password": temp}
    )
    assert login.status_code == 200, login.text
    assert login.json()["must_change_password"] is True


@pytest.mark.asyncio
async def test_invite_duplicate_email_conflict(client: AsyncClient, org_admin, org_agent, auth_headers):
    resp = await client.post(
        f"{MEMBERS}/invite",
        headers=auth_headers(org_admin),
        json={"email": "rep@acme.io", "name": "Dup", "role": "org_agent"},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_invite_platform_admin_role_rejected(client: AsyncClient, org_admin, auth_headers):
    resp = await client.post(
        f"{MEMBERS}/invite",
        headers=auth_headers(org_admin),
        json={"email": "x@acme.io", "name": "X", "role": "platform_admin"},
    )
    assert resp.status_code == 422, resp.text  # schema rejects the role


@pytest.mark.asyncio
async def test_invite_denied_for_agent(client: AsyncClient, org_agent, auth_headers):
    resp = await client.post(
        f"{MEMBERS}/invite",
        headers=auth_headers(org_agent),
        json={"email": "y@acme.io", "name": "Y", "role": "org_agent"},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_change_member_role(client: AsyncClient, org_admin, org_agent, auth_headers):
    resp = await client.put(
        f"{MEMBERS}/{org_agent.id}/role",
        headers=auth_headers(org_admin),
        json={"role": "org_manager"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["role"] == "org_manager"


@pytest.mark.asyncio
async def test_change_own_admin_role_blocked(client: AsyncClient, org_admin, auth_headers):
    resp = await client.put(
        f"{MEMBERS}/{org_admin.id}/role",
        headers=auth_headers(org_admin),
        json={"role": "org_agent"},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_remove_member(client: AsyncClient, org_admin, org_agent, auth_headers):
    resp = await client.delete(
        f"{MEMBERS}/{org_agent.id}", headers=auth_headers(org_admin)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["is_active"] is False


@pytest.mark.asyncio
async def test_remove_self_blocked(client: AsyncClient, org_admin, auth_headers):
    resp = await client.delete(
        f"{MEMBERS}/{org_admin.id}", headers=auth_headers(org_admin)
    )
    assert resp.status_code == 400, resp.text


# --- Cross-org isolation -----------------------------------------------------


@pytest.mark.asyncio
async def test_cannot_change_role_of_other_org_member(
    client: AsyncClient, org_b_admin, org_agent, auth_headers
):
    """An org B admin cannot touch an org A member — 404, not a leak."""
    resp = await client.put(
        f"{MEMBERS}/{org_agent.id}/role",
        headers=auth_headers(org_b_admin),
        json={"role": "org_manager"},
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_members_list_scoped_to_own_org(
    client: AsyncClient, org_b_admin, org_admin, org_agent, auth_headers
):
    resp = await client.get(MEMBERS, headers=auth_headers(org_b_admin))
    assert resp.status_code == 200, resp.text
    emails = {m["email"] for m in resp.json()}
    assert "owner@beta.io" in emails
    assert "rep@acme.io" not in emails  # org A member not visible


# --- Usage -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_counts(client: AsyncClient, org_admin, org_agent, auth_headers, db_session):
    # Two contacts + two sent emails for org A.
    company = Company(id=uuid.uuid4(), name="Acme Co", domain="acme.io",
                      source="test", org_id=APPREND_ORG_ID)
    db_session.add(company)
    await db_session.flush()
    db_session.add(Contact(id=uuid.uuid4(), company_id=company.id, first_name="A",
                           last_name="One", email="a1@x.io", source="test", org_id=APPREND_ORG_ID))
    db_session.add(Contact(id=uuid.uuid4(), company_id=company.id, first_name="B",
                           last_name="Two", email="b2@x.io", source="test", org_id=APPREND_ORG_ID))
    db_session.add(EmailSend(id=uuid.uuid4(), to_email="a1@x.io", status="sent",
                             org_id=APPREND_ORG_ID))
    db_session.add(EmailSend(id=uuid.uuid4(), to_email="b2@x.io", status="sent",
                             org_id=APPREND_ORG_ID))
    await db_session.commit()

    resp = await client.get(USAGE, headers=auth_headers(org_admin))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["org_id"] == str(APPREND_ORG_ID)
    assert data["contacts"] == 2
    assert data["emails_sent_total"] >= 2
    assert data["emails_sent_this_month"] >= 2
    assert data["members"] >= 2  # org_admin + org_agent
    assert "ai_call_limit" in data
    assert len(data["trend"]) == 6
    assert data["trend"][-1]["month"] == f"{datetime.now(timezone.utc):%Y-%m}"


# --- Service unit ------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_service_trend_shape(db_session):
    svc = get_org_service()
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    trend = await svc.monthly_trend(db_session, APPREND_ORG_ID, now, months=6)
    assert [p["month"] for p in trend] == [
        "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07",
    ]
    assert all(p["emails_sent"] == 0 and p["ai_calls"] == 0 for p in trend)
