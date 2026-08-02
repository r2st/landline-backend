"""Auto-grouping must survive two groups sharing a name.

The three ``/groups/auto-group-by-*`` endpoints ask "is there already a group
called X?" with ``scalar_one_or_none()``. Nothing in the schema makes that a safe
question: ``company_groups.name`` has no unique constraint and ``POST /groups``
accepts any name, so a tenant that created "Aerospace" twice — by hand, or by a
double-submit — had a database that made every auto-group endpoint raise
``MultipleResultsFound`` and return a 500. No way back except finding and
deleting one of the groups, which is not a thing the UI explains.

This is the same failure the unsubscribe handler had on duplicate contact rows
(see CLAUDE.md): ``scalar_one_or_none`` reads as the careful choice and is an
assertion of uniqueness that the schema never made.

The fix takes the first match, ordered by ``created_at``, so repeat runs keep
filing into the same group rather than alternating between the duplicates.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.company_group import CompanyGroup, company_group_members
from app.org_context import APPREND_ORG_ID, reset_current_org_id, set_current_org_id


ENDPOINTS = {
    "industry": "/api/v1/groups/auto-group-by-industry",
    "country": "/api/v1/groups/auto-group-by-country",
    "stage": "/api/v1/groups/auto-group-by-stage",
}


@pytest_asyncio.fixture
async def duplicate_named_groups(db_session: AsyncSession):
    """Two groups sharing each name auto-grouping is about to look up.

    ``Aerospace`` is the industry, ``Canada`` the country label, and
    ``New Prospects`` the label for stage ``new`` — one duplicate pair per
    endpoint, all reachable from the single seeded company below.
    """
    token = set_current_org_id(APPREND_ORG_ID)
    try:
        company = Company(
            name="DupCo", domain="dupco.example", source="test",
            industry_segment="Aerospace", country="CA", stage="new",
            org_id=APPREND_ORG_ID,
        )
        db_session.add(company)
        for name in ("Aerospace", "Canada", "New Prospects"):
            for _ in range(2):
                db_session.add(
                    CompanyGroup(name=name, description="dup", org_id=APPREND_ORG_ID)
                )
        await db_session.commit()
    finally:
        reset_current_org_id(token)
    return company


@pytest.mark.parametrize("kind", sorted(ENDPOINTS))
@pytest.mark.asyncio
async def test_auto_group_does_not_500_on_duplicate_names(
    kind, client: AsyncClient, make_user, auth_headers, duplicate_named_groups
):
    user = await make_user(f"ag-{kind}@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(ENDPOINTS[kind], headers=auth_headers(user))
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_it_reuses_a_duplicate_rather_than_creating_a_third(
    client: AsyncClient, db_session: AsyncSession, make_user, auth_headers,
    duplicate_named_groups,
):
    user = await make_user("ag-reuse@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(ENDPOINTS["industry"], headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups_created"] == 0, "an existing name was matched"

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        count = (
            await db_session.execute(
                select(func.count(CompanyGroup.id)).where(
                    CompanyGroup.name == "Aerospace"
                )
            )
        ).scalar_one()
    finally:
        reset_current_org_id(token)
    assert count == 2, "auto-grouping added another duplicate instead of reusing one"


@pytest.mark.asyncio
async def test_repeat_runs_file_into_the_same_duplicate(
    client: AsyncClient, db_session: AsyncSession, make_user, auth_headers,
    duplicate_named_groups,
):
    """Deterministic ordering, so membership doesn't ping-pong between the twins.

    Without an ``order_by`` the "first" match is whatever the database returns,
    which can differ between runs — companies would land in one duplicate today
    and the other tomorrow, and neither group would ever hold the full set.
    """
    user = await make_user("ag-stable@alpha-corp.com", org_id=APPREND_ORG_ID)

    for _ in range(2):
        resp = await client.post(ENDPOINTS["industry"], headers=auth_headers(user))
        assert resp.status_code == 200, resp.text

    token = set_current_org_id(APPREND_ORG_ID)
    try:
        rows = (
            await db_session.execute(
                select(
                    company_group_members.c.group_id,
                    func.count(company_group_members.c.company_id),
                ).group_by(company_group_members.c.group_id)
            )
        ).all()
    finally:
        reset_current_org_id(token)

    holding = [group_id for group_id, n in rows if n]
    assert len(holding) == 1, (
        f"the company was filed into {len(holding)} different groups across two "
        "identical runs — the find-or-create lookup is not deterministic"
    )


@pytest.mark.asyncio
async def test_a_unique_name_is_still_created(
    client: AsyncClient, db_session: AsyncSession, make_user, auth_headers
):
    """The ordinary path: no existing group, so one is made."""
    token = set_current_org_id(APPREND_ORG_ID)
    try:
        db_session.add(
            Company(
                name="SoloCo", domain="soloco.example", source="test",
                industry_segment=f"Widgets-{uuid.uuid4().hex[:8]}",
                org_id=APPREND_ORG_ID,
            )
        )
        await db_session.commit()
    finally:
        reset_current_org_id(token)

    user = await make_user("ag-new@alpha-corp.com", org_id=APPREND_ORG_ID)
    resp = await client.post(ENDPOINTS["industry"], headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups_created"] >= 1
