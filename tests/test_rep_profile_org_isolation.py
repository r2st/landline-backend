"""Rep profiles are scoped to the caller's org — the target and the join.

:class:`~app.models.user.User` is one of three models deliberately outside
:class:`~app.models.mixins.OrgScopedMixin`, so the ``do_orm_execute`` filter that
silently scopes every other SELECT does not scope a ``select(User)``.
``/routing/reps`` read users in three places and scoped none of them.

``PUT /routing/reps/{user_id}`` is the one that mattered, and it fails in three
directions at once:

* the 404-or-not answer is an existence oracle for any user id on the platform;
* ``GET /routing/reps`` joins ``User`` to render ``user_name``, so a profile
  written against a foreign id hands the caller that stranger's *name*;
* ``rep_profiles.user_id`` is ``unique=True`` **platform-wide**, not per-org. So
  the write also consumes the only row that user's own tenant could ever insert
  — and because their org's read of ``rep_profiles`` is filtered, they see no
  profile, try to create one, and get a unique violation with nothing in their
  tenant to explain it.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rep_profile import RepProfile
from app.models.tenant import Organization
from app.models.user import UserRole
from app.org_context import (
    APPREND_ORG_ID,
    reset_current_org_id,
    set_current_org_id,
)


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="Routing Beta", slug="routing-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest.mark.asyncio
async def test_upsert_rejects_a_user_in_another_org(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    caller = await make_user(
        "rp-mgr@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user("rp-stranger@beta-corp.com", org_id=org_b.id)

    resp = await client.put(
        f"/api/v1/routing/reps/{stranger.id}",
        json={"industries": ["aerospace"], "max_capacity": 5},
        headers=auth_headers(caller),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_a_refused_upsert_writes_no_row(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The 404 is the visible half; the row it did not write is the durable one.

    ``rep_profiles.user_id`` is unique platform-wide, so a row written here is
    not merely a stray record in the caller's org — it is the slot the stranger's
    own tenant needs and can no longer take.
    """
    caller = await make_user(
        "rp-mgr2@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user("rp-stranger2@beta-corp.com", org_id=org_b.id)

    await client.put(
        f"/api/v1/routing/reps/{stranger.id}",
        json={"max_capacity": 5},
        headers=auth_headers(caller),
    )

    token = set_current_org_id(None)  # look at the table with no filter at all
    try:
        rows = (
            await db_session.execute(
                select(RepProfile).where(RepProfile.user_id == stranger.id)
            )
        ).scalars().all()
    finally:
        reset_current_org_id(token)
    assert rows == [], "a profile was written against another org's user"


@pytest.mark.asyncio
async def test_the_stranger_can_still_create_their_own_profile(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The end state the unique constraint made unreachable."""
    caller = await make_user(
        "rp-mgr3@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user(
        "rp-stranger3@beta-corp.com", role=UserRole.ORG_MANAGER.value, org_id=org_b.id
    )

    # Org A pokes at the id first — this used to take the slot.
    await client.put(
        f"/api/v1/routing/reps/{stranger.id}",
        json={"max_capacity": 5},
        headers=auth_headers(caller),
    )

    resp = await client.put(
        f"/api/v1/routing/reps/{stranger.id}",
        json={"max_capacity": 12},
        headers=auth_headers(stranger),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_capacity"] == 12


@pytest.mark.asyncio
async def test_the_guard_does_not_cost_a_manager_their_own_team(
    client: AsyncClient, db_session, make_user, auth_headers
):
    caller = await make_user(
        "rp-mgr4@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    teammate = await make_user(
        "rp-mate@alpha-corp.com", name="Alpha Mate", org_id=APPREND_ORG_ID
    )

    resp = await client.put(
        f"/api/v1/routing/reps/{teammate.id}",
        json={"industries": ["plastics"], "max_capacity": 9},
        headers=auth_headers(caller),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["max_capacity"] == 9

    listed = await client.get("/api/v1/routing/reps", headers=auth_headers(caller))
    assert listed.status_code == 200, listed.text
    assert [r["user_id"] for r in listed.json()] == [str(teammate.id)]


@pytest.mark.asyncio
async def test_the_list_never_renders_another_orgs_name(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The join to ``User`` needs its own predicate.

    A profile row that predates this fix is still in the table, and the list
    resolved its ``user_name`` through an unfiltered join.
    """
    caller = await make_user(
        "rp-mgr5@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user(
        "rp-stranger5@beta-corp.com", name="Beta Stranger", org_id=org_b.id
    )

    # Plant the row the old endpoint would have written, stamped to org A.
    db_session.add(RepProfile(user_id=stranger.id, org_id=APPREND_ORG_ID))
    await db_session.commit()

    resp = await client.get("/api/v1/routing/reps", headers=auth_headers(caller))
    assert resp.status_code == 200, resp.text
    assert "Beta Stranger" not in {r.get("user_name") for r in resp.json()}
