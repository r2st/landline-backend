"""``?view_as_user_id=`` may only name a user in the caller's own org.

``User`` is one of three models deliberately left outside
:class:`~app.models.mixins.OrgScopedMixin` — membership *is* the ``org_id``
column, and login reads a user before any org context exists — so the global
read filter, which covers every other lookup in the application, does not cover
this one. It has to be written out, and it was not.

The blast radius was narrower than it sounds, because everything a scoped
request goes on to read is org-filtered anyway. What an org_admin could actually
do was probe whether an arbitrary user id existed anywhere on the platform
(404 vs 200), and have anything created during that request attributed to a user
in someone else's tenant. Both are the kind of small wrongness that becomes a
real one the first time a new model or a report trusts ``target_user_id`` on its
own.

A ``platform_admin`` carries no org and is supposed to reach across; that is
pinned here too, so a later tightening does not take the cross-org views away
from the role that exists for them.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_scope
from app.models.tenant import Organization
from app.models.user import UserRole
from app.org_context import APPREND_ORG_ID


@pytest_asyncio.fixture
async def org_b(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=uuid.uuid4(), name="ViewAs Beta", slug="viewas-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


@pytest.mark.asyncio
async def test_admin_cannot_view_as_a_user_in_another_org(
    db_session: AsyncSession, make_user, org_b
):
    admin_a = await make_user(
        "va-admin@alpha-corp.com", role=UserRole.ORG_ADMIN.value, org_id=APPREND_ORG_ID
    )
    stranger = await make_user("va-stranger@beta-corp.com", org_id=org_b.id)

    with pytest.raises(HTTPException) as exc:
        await get_scope(
            view_as_user_id=str(stranger.id), current_user=admin_a, db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_can_still_view_as_a_user_in_their_own_org(
    db_session: AsyncSession, make_user
):
    admin_a = await make_user(
        "va-admin2@alpha-corp.com", role=UserRole.ORG_ADMIN.value, org_id=APPREND_ORG_ID
    )
    teammate = await make_user("va-teammate@alpha-corp.com", org_id=APPREND_ORG_ID)

    scope = await get_scope(
        view_as_user_id=str(teammate.id), current_user=admin_a, db=db_session
    )
    assert scope.target_user_id == teammate.id
    assert scope.see_all is False
    assert scope.owner_id == teammate.id


@pytest.mark.asyncio
async def test_platform_admin_may_view_as_any_org_s_user(
    db_session: AsyncSession, make_user, org_b
):
    """The role that has no org is the one allowed to cross them."""
    superadmin = await make_user(
        "va-super@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None
    )
    stranger = await make_user("va-stranger2@beta-corp.com", org_id=org_b.id)

    scope = await get_scope(
        view_as_user_id=str(stranger.id), current_user=superadmin, db=db_session
    )
    assert scope.target_user_id == stranger.id


@pytest.mark.asyncio
async def test_a_nonexistent_id_is_still_a_404(db_session: AsyncSession, make_user):
    admin_a = await make_user(
        "va-admin3@alpha-corp.com", role=UserRole.ORG_ADMIN.value, org_id=APPREND_ORG_ID
    )
    with pytest.raises(HTTPException) as exc:
        await get_scope(
            view_as_user_id=str(uuid.uuid4()), current_user=admin_a, db=db_session
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_view_as_all_is_unaffected(db_session: AsyncSession, make_user):
    """``all`` never named a user, so it never went through the lookup."""
    admin_a = await make_user(
        "va-admin4@alpha-corp.com", role=UserRole.ORG_ADMIN.value, org_id=APPREND_ORG_ID
    )
    scope = await get_scope(view_as_user_id="all", current_user=admin_a, db=db_session)
    assert scope.see_all is True


@pytest.mark.asyncio
async def test_an_unprivileged_user_is_scoped_to_themselves_regardless(
    db_session: AsyncSession, make_user, org_b
):
    """The override is ignored for an agent, so the org check is not its only guard."""
    agent = await make_user("va-agent@alpha-corp.com", org_id=APPREND_ORG_ID)
    stranger = await make_user("va-stranger3@beta-corp.com", org_id=org_b.id)

    scope = await get_scope(
        view_as_user_id=str(stranger.id), current_user=agent, db=db_session
    )
    assert scope.target_user_id == agent.id
