"""Coaching is scoped to the caller's org — the roster, the target, the benchmark.

:class:`~app.models.user.User` is one of three models deliberately outside
:class:`~app.models.mixins.OrgScopedMixin`, so the ``do_orm_execute`` filter that
silently scopes every other SELECT in the application does not scope a
``select(User)``. Coaching read users in four places and scoped none of them:

* ``GET /coaching/team`` enumerated every active user on the platform — and then
  ran ``_get_or_generate`` over them, so it did not merely *read* a foreign
  roster, it wrote a RepPerformance row per stranger, stamped by the insert hook
  with the caller's org;
* ``POST /coaching/generate?target_user_id=`` accepted any user id on the
  platform and returned a snapshot labelled with that person's name;
* the Celery task ``generate_weekly_coaching`` runs inside ``for_each_org`` —
  which binds each org precisely so scoped reads come out right — and then read
  users unscoped, so every org's pass generated snapshots for every user on the
  platform, each stamped with whichever tenant the loop was on.

``compute_team_benchmarks`` is scoped too, but it is the one that was *not*
leaking: every metric it reads is org-scoped, so a foreign rep already came back
with no activity and was dropped before the averages. That is asserted below as
well, because "it happens to be safe via another model's filter" is a property
worth pinning rather than re-deriving.

The fix is :func:`app.dependencies.scope_users_to_org` (a caller in hand) and
:func:`app.dependencies.scope_users_to_current_org` (an org binding in hand).
These tests pin the behaviour at each of the four surfaces.
"""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.rep_performance import RepPerformance
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
        id=uuid.uuid4(), name="Coaching Beta", slug="coaching-beta",
        is_active=True, billing_plan="free",
    )
    db_session.add(org)
    await db_session.commit()
    return org


# --- GET /coaching/team ------------------------------------------------------

@pytest.mark.asyncio
async def test_team_roster_is_only_the_callers_org(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    manager_a = await make_user(
        "co-mgr@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        name="Alpha Manager", org_id=APPREND_ORG_ID,
    )
    await make_user(
        "co-stranger@beta-corp.com", name="Beta Stranger", org_id=org_b.id
    )

    resp = await client.get("/api/v1/coaching/team", headers=auth_headers(manager_a))
    assert resp.status_code == 200, resp.text

    names = {r["user_name"] for r in resp.json()["reps"]}
    assert "Beta Stranger" not in names, "another tenant's rep is on this team page"
    assert "Alpha Manager" in names


@pytest.mark.asyncio
async def test_team_page_does_not_write_snapshots_for_other_orgs_users(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """The read was the visible half; this was the durable one.

    ``_get_or_generate`` writes a RepPerformance row for every rep it lists, and
    the ``before_flush`` hook stamps it with the *caller's* org — so merely
    opening the team page minted rows in org A describing org B's people.
    """
    manager_a = await make_user(
        "co-mgr2@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user("co-stranger2@beta-corp.com", org_id=org_b.id)

    resp = await client.get("/api/v1/coaching/team", headers=auth_headers(manager_a))
    assert resp.status_code == 200, resp.text

    token = set_current_org_id(None)  # look at the table with no filter at all
    try:
        rows = (
            await db_session.execute(
                select(RepPerformance).where(RepPerformance.user_id == stranger.id)
            )
        ).scalars().all()
    finally:
        reset_current_org_id(token)
    assert rows == [], (
        "the team page generated a performance snapshot for a user in another org"
    )


# --- POST /coaching/generate -------------------------------------------------

@pytest.mark.asyncio
async def test_generate_rejects_a_target_in_another_org(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    manager_a = await make_user(
        "co-mgr3@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user("co-stranger3@beta-corp.com", org_id=org_b.id)

    resp = await client.post(
        f"/api/v1/coaching/generate?target_user_id={stranger.id}",
        headers=auth_headers(manager_a),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_generate_still_works_for_a_teammate(
    client: AsyncClient, db_session, make_user, auth_headers
):
    """The guard must not cost a manager their own team."""
    manager_a = await make_user(
        "co-mgr4@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    teammate = await make_user(
        "co-teammate@alpha-corp.com", name="Alpha Teammate", org_id=APPREND_ORG_ID
    )

    resp = await client.post(
        f"/api/v1/coaching/generate?target_user_id={teammate.id}",
        headers=auth_headers(manager_a),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == str(teammate.id)


@pytest.mark.asyncio
async def test_a_nonexistent_target_and_a_foreign_one_are_indistinguishable(
    client: AsyncClient, db_session, make_user, auth_headers, org_b
):
    """404 either way, so the endpoint is not an existence oracle for user ids."""
    manager_a = await make_user(
        "co-mgr5@alpha-corp.com", role=UserRole.ORG_MANAGER.value,
        org_id=APPREND_ORG_ID,
    )
    stranger = await make_user("co-stranger5@beta-corp.com", org_id=org_b.id)

    foreign = await client.post(
        f"/api/v1/coaching/generate?target_user_id={stranger.id}",
        headers=auth_headers(manager_a),
    )
    absent = await client.post(
        f"/api/v1/coaching/generate?target_user_id={uuid.uuid4()}",
        headers=auth_headers(manager_a),
    )
    assert foreign.status_code == absent.status_code == 404
    assert foreign.json()["detail"] == absent.json()["detail"]


# --- compute_team_benchmarks -------------------------------------------------

async def _give_rep_activity(db: AsyncSession, user, org_id, *, count: int) -> None:
    """Seed interactions so the rep is counted.

    ``compute_team_benchmarks`` skips reps with no activity, so a fixture of
    inert users makes ``rep_count`` 0 whatever the scoping does — the assertion
    would pass against the bug it is meant to catch.
    """
    from datetime import datetime, timedelta, timezone

    from app.models.interaction import Interaction

    token = set_current_org_id(org_id)
    try:
        for i in range(count):
            db.add(
                Interaction(
                    user_id=user.id,
                    interaction_type="call",
                    subject=f"seeded {i}",
                    occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
                    org_id=org_id,
                )
            )
        await db.commit()
    finally:
        reset_current_org_id(token)


@pytest.mark.asyncio
async def test_benchmarks_count_only_the_bound_orgs_reps(
    db_session: AsyncSession, make_user, org_b
):
    """``rep_count`` is the cheapest observable proof of who was averaged in."""
    from app.services.sales_coach import compute_team_benchmarks, period_bounds

    rep_a = await make_user("co-bench-a1@alpha-corp.com", org_id=APPREND_ORG_ID)
    rep_b1 = await make_user("co-bench-b1@beta-corp.com", org_id=org_b.id)
    rep_b2 = await make_user("co-bench-b2@beta-corp.com", org_id=org_b.id)

    await _give_rep_activity(db_session, rep_a, APPREND_ORG_ID, count=3)
    await _give_rep_activity(db_session, rep_b1, org_b.id, count=3)
    await _give_rep_activity(db_session, rep_b2, org_b.id, count=3)

    period_start, period_end = period_bounds("weekly")

    token = set_current_org_id(org_b.id)
    try:
        scoped = await compute_team_benchmarks(db_session, period_start, period_end)
    finally:
        reset_current_org_id(token)

    assert scoped["rep_count"] == 2, (
        "benchmarks counted reps outside the bound org — one tenant's averages "
        f"were computed over {scoped['rep_count']} reps, not its own 2"
    )


@pytest.mark.asyncio
async def test_benchmarks_were_already_safe_via_the_metric_tables(
    db_session: AsyncSession, make_user, org_b
):
    """Why the User scoping here is depth, not the fix.

    Every table :func:`compute_rep_metrics` reads is org-scoped, so a foreign rep
    contributes no activity and is dropped for having none — the roster leak
    could not reach the averages. Pinned because it is the reason this function
    was never the vulnerability, and because it stops being true the moment
    somebody adds a metric sourced from an unscoped table.
    """
    from app.services.sales_coach import compute_rep_metrics, period_bounds

    rep_b = await make_user("co-bench-b3@beta-corp.com", org_id=org_b.id)
    await _give_rep_activity(db_session, rep_b, org_b.id, count=4)

    period_start, period_end = period_bounds("weekly")

    # Read org B's rep while bound to org A: the metrics must come back empty.
    token = set_current_org_id(APPREND_ORG_ID)
    try:
        metrics = await compute_rep_metrics(
            db_session, rep_b.id, period_start, period_end
        )
    finally:
        reset_current_org_id(token)

    assert metrics["interactions_total"] == 0
    assert metrics["emails_sent"] == 0
    assert metrics["calls_completed"] == 0


@pytest.mark.asyncio
async def test_benchmarks_are_unfiltered_with_no_org_bound(
    db_session: AsyncSession, make_user, org_b
):
    """Matches the global filter's own rule: no org context, no filter.

    Pinned so the helper is not later 'hardened' into failing closed, which would
    silently break every platform-admin view and every background job that runs
    outside ``for_each_org``.
    """
    from app.services.sales_coach import compute_team_benchmarks, period_bounds

    period_start, period_end = period_bounds("weekly")
    token = set_current_org_id(None)
    try:
        result = await compute_team_benchmarks(db_session, period_start, period_end)
    finally:
        reset_current_org_id(token)
    assert "rep_count" in result


# --- the weekly Celery task --------------------------------------------------

@pytest.mark.asyncio
async def test_the_weekly_task_selects_only_the_bound_orgs_reps(
    db_session: AsyncSession, make_user, org_b
):
    """The rep selection inside ``generate_weekly_coaching``'s per-org body.

    The task itself runs through ``for_each_org`` on a real ``celery_session``,
    so what is exercised here is the query the body builds — the single line that
    was wrong. Its loop then calls ``generate_rep_performance`` on each row, and
    that writes: unscoped, every org's pass produced a snapshot for every user on
    the platform, stamped with whichever tenant the loop was on. With N orgs that
    is N-1 wrong-tenant rows per user, every week.
    """
    from app.dependencies import scope_users_to_current_org
    from app.models.user import User

    await make_user("co-task-a@alpha-corp.com", org_id=APPREND_ORG_ID)
    b1 = await make_user("co-task-b1@beta-corp.com", org_id=org_b.id)
    b2 = await make_user("co-task-b2@beta-corp.com", org_id=org_b.id)

    token = set_current_org_id(org_b.id)
    try:
        reps = (
            await db_session.execute(
                scope_users_to_current_org(
                    select(User).where(User.is_active.is_(True))
                )
            )
        ).scalars().all()
    finally:
        reset_current_org_id(token)

    assert {r.id for r in reps} == {b1.id, b2.id}


@pytest.mark.asyncio
async def test_the_weekly_task_body_scopes_its_user_read(db_session, make_user):
    """The task module must route its rep read through the helper, not select raw.

    A source-level check, deliberately: the leak was one ``select(User)`` with
    nothing around it, and it read as ordinary because every other query in the
    file is scoped invisibly. This fails if someone reintroduces the bare form.
    """
    import inspect

    import app.tasks.coaching as task_module

    source = inspect.getsource(task_module)
    assert "scope_users_to_current_org" in source, (
        "app/tasks/coaching.py no longer scopes its User read — inside "
        "for_each_org that writes a snapshot for every user on the platform, "
        "once per org"
    )


# --- the helpers themselves --------------------------------------------------

@pytest.mark.asyncio
async def test_platform_admin_is_exempt_from_user_scoping(make_user):
    """The role exists to cross orgs; /platform and /tenants are built on it."""
    from app.dependencies import scope_users_to_org
    from app.models.user import User

    superadmin = await make_user(
        "co-super@landline.app", role=UserRole.PLATFORM_ADMIN.value, org_id=None
    )
    stmt = select(User)
    assert scope_users_to_org(stmt, superadmin) is stmt, (
        "a platform admin's query should come back untouched"
    )


@pytest.mark.asyncio
async def test_an_org_less_non_platform_admin_does_not_get_the_whole_table(make_user):
    """Fail closed on the odd case: org_id IS NULL matches org-less rows, not all."""
    from app.dependencies import scope_users_to_org
    from app.models.user import User

    orphan = await make_user(
        "co-orphan@nowhere.test", role=UserRole.ORG_AGENT.value, org_id=None
    )
    scoped = scope_users_to_org(select(User), orphan)
    assert "org_id" in str(scoped).lower()
