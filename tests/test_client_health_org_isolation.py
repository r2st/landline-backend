"""Client health scoring is one tenant's account data, reachable by company id.

``health.router`` was mounted in ``app/main.py`` without ``dependencies=_auth``.
That reads as an oversight about *authentication* and isn't one —
``ApiKeyMiddleware`` still rejected anyone without a valid token. What ``_auth``
supplies is :func:`app.dependencies.get_current_user`, and the only thing in the
request path that calls ``set_current_org_id``. With no org bound the global read
filter takes its documented no-op branch, so every query in these three
endpoints ran unfiltered — for any logged-in user of any tenant.

The write is the worse half. No org context also means ``org_id_for_insert()``
falls back to the first-party org, so ``POST /health/snapshot`` created a row
owned by Apprend pointing at a stranger's company, on a table the owning tenant
reads back as its own history.

The module name is most of why it sat there: it looks like a liveness probe and
serves ``/companies/{id}/health``. ``tests/test_route_org_binding.py`` is the
mechanical half — no future router can be mounted the same way. This is the
behavioural half, written against the API because that is the surface the leak
was reachable on.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client_health import ClientHealthSnapshot
from app.models.company import Company
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID, reset_current_org_id, set_current_org_id


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Health Beta", slug="health-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


async def _seed_company(db: AsyncSession, name: str, org_id) -> Company:
    token = set_current_org_id(org_id)
    try:
        company = Company(
            name=name, domain=f"{name.lower()}.example", source="test", org_id=org_id,
        )
        db.add(company)
        await db.commit()
    finally:
        reset_current_org_id(token)
    return company


@pytest.mark.asyncio
async def test_health_score_of_another_orgs_company_is_not_readable(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    theirs = await _seed_company(db_session, "BetaHealthCo", org_b.id)
    caller = await make_user("ha@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.get(
        f"/api/v1/companies/{theirs.id}/health", headers=auth_headers(caller)
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_health_history_of_another_orgs_company_is_not_readable(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    theirs = await _seed_company(db_session, "BetaHistoryCo", org_b.id)
    caller = await make_user("hb@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.get(
        f"/api/v1/companies/{theirs.id}/health/history", headers=auth_headers(caller)
    )
    assert resp.status_code in (200, 404), resp.text
    if resp.status_code == 200:
        assert resp.json() == [], "another tenant's snapshot series must not come back"


@pytest.mark.asyncio
async def test_snapshot_cannot_be_written_against_another_orgs_company(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The write half: no org bound also decided who *owned* the new row."""
    theirs = await _seed_company(db_session, "BetaSnapshotCo", org_b.id)
    caller = await make_user("hc@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.post(
        f"/api/v1/companies/{theirs.id}/health/snapshot",
        json={"satisfaction_score": 9},
        headers=auth_headers(caller),
    )
    assert resp.status_code == 404, resp.text

    written = (
        await db_session.execute(
            select(func.count(ClientHealthSnapshot.id))
            .where(ClientHealthSnapshot.company_id == theirs.id)
            .execution_options(skip_org_filter=True)
        )
    ).scalar()
    assert written == 0


@pytest.mark.asyncio
async def test_the_callers_own_company_still_works(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """The control. A 404 for everything would pass the three tests above."""
    mine = await _seed_company(db_session, "AlphaHealthCo", APPREND_ORG_ID)
    caller = await make_user("hd@alpha-corp.com", org_id=APPREND_ORG_ID)

    resp = await client.get(
        f"/api/v1/companies/{mine.id}/health", headers=auth_headers(caller)
    )
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        f"/api/v1/companies/{mine.id}/health/snapshot",
        json={"satisfaction_score": 8},
        headers=auth_headers(caller),
    )
    assert resp.status_code == 201, resp.text
