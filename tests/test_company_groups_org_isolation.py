"""Company groups are one tenant's filing system, not a shared one.

``company_groups`` was created by migration 008 and never picked up an ``org_id``.
Migration 078 gave 71 tables one; this table has no :class:`OrgScopedMixin`, so
autogenerate had nothing to notice and it was skipped. The consequence is not
subtle: every group every customer has ever created is in one global namespace,
readable, renameable and deletable by all of them, with the member companies
attached.

These tests pin the isolation, not the implementation. They are written against
the API because that is the surface the leak was reachable on.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_group import CompanyGroup, company_group_members
from app.models.tenant import Organization
from app.org_context import (
    APPREND_ORG_ID,
    reset_current_org_id,
    set_current_org_id,
)


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Groups Beta", slug="groups-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _seed_group(db: AsyncSession, name: str, org_id, company_name: str) -> CompanyGroup:
    """A group in ``org_id`` holding one company in the same org."""
    token = set_current_org_id(org_id)
    try:
        company = Company(
            name=company_name, domain=f"{company_name.lower()}.example",
            source="test", org_id=org_id,
        )
        db.add(company)
        group = CompanyGroup(name=name, description=f"{name} desc", org_id=org_id)
        db.add(group)
        await db.flush()
        await db.execute(
            company_group_members.insert().values(group_id=group.id, company_id=company.id)
        )
        await db.commit()
    finally:
        reset_current_org_id(token)
    return group


# --- reads -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_groups_shows_only_the_callers_org(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The list endpoint returned every tenant's groups."""
    await _seed_group(db_session, "AlphaFolder", APPREND_ORG_ID, "AlphaCo")
    await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    user_a = await make_user("ga@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.get("/api/v1/groups", headers=auth_headers(user_a))
    assert resp.status_code == 200, resp.text
    names = {g["name"] for g in resp.json()}
    assert "AlphaFolder" in names
    assert "BetaFolder" not in names


@pytest.mark.asyncio
async def test_group_members_never_name_another_orgs_companies(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """This half already held: the loader criteria the global filter injects
    propagate into the ``selectinload``, so a foreign group came back reading
    ``member_count: 0``. Pinned because it is what bounded the original leak to
    the group rows, and because the association table has no ``org_id`` of its
    own to fall back on if that propagation ever changes."""
    await _seed_group(db_session, "AlphaFolder", APPREND_ORG_ID, "AlphaCo")
    await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    user_a = await make_user("gm@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.get("/api/v1/groups", headers=auth_headers(user_a))
    assert resp.status_code == 200, resp.text
    seen = {c["name"] for g in resp.json() for c in g["companies"]}
    assert "BetaCo" not in seen


# --- writes ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cannot_rename_another_orgs_group(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    group_b = await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    user_a = await make_user("gr@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.patch(
        f"/api/v1/groups/{group_b.id}",
        json={"name": "Owned"},
        headers=auth_headers(user_a),
    )
    assert resp.status_code == 404, resp.text

    token = set_current_org_id(org_b.id)
    try:
        await db_session.refresh(group_b)
        assert group_b.name == "BetaFolder"
    finally:
        reset_current_org_id(token)


@pytest.mark.asyncio
async def test_cannot_delete_another_orgs_group(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    group_b = await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    user_a = await make_user("gd@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.delete(
        f"/api/v1/groups/{group_b.id}", headers=auth_headers(user_a)
    )
    assert resp.status_code == 404, resp.text

    still_there = (
        await db_session.execute(
            select(CompanyGroup.id)
            .where(CompanyGroup.id == group_b.id)
            .execution_options(skip_org_filter=True)
        )
    ).first()
    assert still_there is not None


@pytest.mark.asyncio
async def test_cannot_add_members_to_another_orgs_group(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    group_b = await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    user_a = await make_user("gam@alpha-corp.com", org_id=APPREND_ORG_ID)
    token = set_current_org_id(APPREND_ORG_ID)
    try:
        mine = Company(name="MineCo", domain="mine.example", source="test", org_id=APPREND_ORG_ID)
        db_session.add(mine)
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    resp = await client.post(
        f"/api/v1/groups/{group_b.id}/members",
        json={"company_ids": [str(mine.id)]},
        headers=auth_headers(user_a),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_cannot_remove_members_from_another_orgs_group(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The delete ran straight off the association table with no group lookup,
    so it never had to own the group it was editing."""
    group_b = await _seed_group(db_session, "BetaFolder", org_b.id, "BetaCo")
    member = (
        await db_session.execute(
            select(company_group_members.c.company_id).where(
                company_group_members.c.group_id == group_b.id
            )
        )
    ).scalar_one()
    user_a = await make_user("grm@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.request(
        "DELETE",
        f"/api/v1/groups/{group_b.id}/members",
        json={"company_ids": [str(member)]},
        headers=auth_headers(user_a),
    )
    assert resp.status_code == 404, resp.text

    rows = (
        await db_session.execute(
            select(company_group_members.c.company_id).where(
                company_group_members.c.group_id == group_b.id
            )
        )
    ).all()
    assert len(rows) == 1


# --- auto-grouping -----------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_group_does_not_join_another_orgs_group_by_name(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The "does a group with this name already exist?" lookup was global, so
    two tenants with an "Aerospace" segment ended up sharing one group row —
    each one's companies listed under the other's folder."""
    token = set_current_org_id(org_b.id)
    try:
        beta_group = CompanyGroup(name="Aerospace", org_id=org_b.id)
        db_session.add(beta_group)
        db_session.add(
            Company(
                name="BetaAero", domain="betaaero.example", source="test",
                org_id=org_b.id, industry_segment="Aerospace",
            )
        )
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        db_session.add(
            Company(
                name="AlphaAero", domain="alphaaero.example", source="test",
                org_id=APPREND_ORG_ID, industry_segment="Aerospace",
            )
        )
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    user_a = await make_user("gag@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(
        "/api/v1/groups/auto-group-by-industry", headers=auth_headers(user_a)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups_created"] == 1

    members = (
        await db_session.execute(
            select(company_group_members.c.company_id).where(
                company_group_members.c.group_id == beta_group.id
            )
        )
    ).all()
    assert members == [], "auto-grouping wrote into another org's group"


@pytest.mark.asyncio
async def test_created_group_is_stamped_with_the_callers_org(
    client: AsyncClient, make_user, auth_headers, db_session, org_b
):
    user_b = await make_user("gc@beta-corp.com", org_id=org_b.id)
    resp = await client.post(
        "/api/v1/groups", json={"name": "BetaMade"}, headers=auth_headers(user_b)
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(
            select(CompanyGroup.org_id)
            .where(CompanyGroup.id == uuid.UUID(resp.json()["id"]))
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one()
    assert row == org_b.id
