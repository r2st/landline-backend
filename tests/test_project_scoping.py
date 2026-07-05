"""Project-wide per-user scoping + admin "View as" tests.

Covers the reusable scope dependency applied across activity routers: tasks,
call tasks, follow-ups, interactions, deal notes — plus the admin/manager
``view_as_user_id`` switcher and the /auth/team-members endpoint.

Pattern under test:
  * regular users only ever see/act on their own rows (view_as is ignored),
  * admins/managers see their own by default,
  * admins/managers may pass ``view_as_user_id=<uuid>`` to see one user's data
    or ``view_as_user_id=all`` to see everyone's,
  * creates stamp user_id, and cross-scope single-record access 404s.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.deal import Deal
from app.models.user import User
from app.security import create_access_token, hash_password


def _hdr(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.role)}"}


async def _mk_user(db: AsyncSession, email: str, role: str = "agent") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        password_hash=hash_password("pw123456"),
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def alice(db_session):
    return await _mk_user(db_session, "alice@corp.com", "agent")


@pytest_asyncio.fixture
async def bob(db_session):
    return await _mk_user(db_session, "bob@corp.com", "agent")


@pytest_asyncio.fixture
async def boss(db_session):
    return await _mk_user(db_session, "boss@corp.com", "admin")


@pytest_asyncio.fixture
async def company(db_session):
    c = Company(id=uuid.uuid4(), name="Acme", source="test")
    db_session.add(c)
    await db_session.flush()
    return c


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TestTaskScoping:
    async def _create(self, client, user, company, title="T"):
        return await client.post(
            "/api/v1/tasks",
            json={"company_id": str(company.id), "title": title, "task_type": "call"},
            headers=_hdr(user),
        )

    async def test_create_stamps_owner_and_list_scopes(self, client, alice, bob, company):
        r = await self._create(client, alice, company, "alice-task")
        assert r.status_code == 201
        await self._create(client, bob, company, "bob-task")

        # Alice sees only hers.
        r = await client.get("/api/v1/tasks", headers=_hdr(alice))
        titles = {t["title"] for t in r.json()}
        assert titles == {"alice-task"}

        # Bob sees only his.
        r = await client.get("/api/v1/tasks", headers=_hdr(bob))
        assert {t["title"] for t in r.json()} == {"bob-task"}

    async def test_admin_default_own_then_view_as(self, client, alice, bob, boss, company):
        await self._create(client, alice, company, "alice-task")
        await self._create(client, bob, company, "bob-task")
        await self._create(client, boss, company, "boss-task")

        # Admin default: own only.
        r = await client.get("/api/v1/tasks", headers=_hdr(boss))
        assert {t["title"] for t in r.json()} == {"boss-task"}

        # Admin view_as alice.
        r = await client.get(
            f"/api/v1/tasks?view_as_user_id={alice.id}", headers=_hdr(boss)
        )
        assert {t["title"] for t in r.json()} == {"alice-task"}

        # Admin view_as all.
        r = await client.get("/api/v1/tasks?view_as_user_id=all", headers=_hdr(boss))
        assert {t["title"] for t in r.json()} == {"alice-task", "bob-task", "boss-task"}

    async def test_regular_user_cannot_view_as(self, client, alice, bob, company):
        await self._create(client, bob, company, "bob-task")
        await self._create(client, alice, company, "alice-task")
        # Alice tries to view bob — ignored, scoped to herself.
        r = await client.get(
            f"/api/v1/tasks?view_as_user_id={bob.id}", headers=_hdr(alice)
        )
        assert {t["title"] for t in r.json()} == {"alice-task"}

    async def test_cross_user_get_update_delete_404(self, client, alice, bob, company):
        r = await self._create(client, alice, company, "alice-task")
        task_id = r.json()["id"]
        assert (await client.get(f"/api/v1/tasks/{task_id}", headers=_hdr(bob))).status_code == 404
        assert (
            await client.patch(
                f"/api/v1/tasks/{task_id}", json={"title": "x"}, headers=_hdr(bob)
            )
        ).status_code == 404
        assert (
            await client.delete(f"/api/v1/tasks/{task_id}", headers=_hdr(bob))
        ).status_code == 404
        # Owner still can.
        assert (await client.get(f"/api/v1/tasks/{task_id}", headers=_hdr(alice))).status_code == 200

    async def test_admin_view_as_can_reach_single_record(self, client, alice, boss, company):
        r = await self._create(client, alice, company, "alice-task")
        task_id = r.json()["id"]
        # Admin default (own) can't see alice's task.
        assert (await client.get(f"/api/v1/tasks/{task_id}", headers=_hdr(boss))).status_code == 404
        # With view_as alice, can.
        r = await client.get(
            f"/api/v1/tasks/{task_id}?view_as_user_id={alice.id}", headers=_hdr(boss)
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Call tasks
# ---------------------------------------------------------------------------


class TestCallScoping:
    async def _create(self, client, user, company):
        return await client.post(
            "/api/v1/calls/tasks",
            json={"company_id": str(company.id), "priority": "hot"},
            headers=_hdr(user),
        )

    async def test_list_scoped_and_view_as(self, client, alice, bob, boss, company):
        await self._create(client, alice, company)
        await self._create(client, bob, company)
        assert len((await client.get("/api/v1/calls/tasks", headers=_hdr(alice))).json()) == 1
        # admin default own => 0
        assert len((await client.get("/api/v1/calls/tasks", headers=_hdr(boss))).json()) == 0
        # admin all => 2
        r = await client.get("/api/v1/calls/tasks?view_as_user_id=all", headers=_hdr(boss))
        assert len(r.json()) == 2

    async def test_cross_user_get_404(self, client, alice, bob, company):
        r = await self._create(client, alice, company)
        tid = r.json()["id"]
        assert (await client.get(f"/api/v1/calls/tasks/{tid}", headers=_hdr(bob))).status_code == 404
        assert (await client.get(f"/api/v1/calls/tasks/{tid}", headers=_hdr(alice))).status_code == 200


# ---------------------------------------------------------------------------
# Follow-ups
# ---------------------------------------------------------------------------


class TestFollowUpScoping:
    async def _create(self, client, user, company):
        return await client.post(
            "/api/v1/follow-ups",
            json={"company_id": str(company.id), "due_date": "2030-01-01T00:00:00Z",
                  "reason": "check in"},
            headers=_hdr(user),
        )

    async def test_list_scoped_and_admin_all(self, client, alice, bob, boss, company):
        await self._create(client, alice, company)
        await self._create(client, bob, company)
        assert len((await client.get("/api/v1/follow-ups", headers=_hdr(alice))).json()) == 1
        assert len((await client.get("/api/v1/follow-ups", headers=_hdr(boss))).json()) == 0
        r = await client.get("/api/v1/follow-ups?view_as_user_id=all", headers=_hdr(boss))
        assert len(r.json()) == 2

    async def test_cross_user_update_404(self, client, alice, bob, company):
        r = await self._create(client, alice, company)
        fid = r.json()["id"]
        assert (
            await client.patch(
                f"/api/v1/follow-ups/{fid}", json={"status": "completed"}, headers=_hdr(bob)
            )
        ).status_code == 404


# ---------------------------------------------------------------------------
# Interactions
# ---------------------------------------------------------------------------


class TestInteractionScoping:
    async def _create(self, client, user, company):
        return await client.post(
            "/api/v1/interactions",
            json={"company_id": str(company.id), "interaction_type": "call",
                  "summary": "spoke"},
            headers=_hdr(user),
        )

    async def test_create_scoped_list_and_view_as(self, client, alice, bob, boss, company):
        r = await self._create(client, alice, company)
        assert r.status_code == 201
        await self._create(client, bob, company)
        assert len((await client.get("/api/v1/interactions", headers=_hdr(alice))).json()) == 1
        assert len((await client.get("/api/v1/interactions", headers=_hdr(boss))).json()) == 0
        r = await client.get("/api/v1/interactions?view_as_user_id=all", headers=_hdr(boss))
        assert len(r.json()) == 2

    async def test_cross_user_delete_404(self, client, alice, bob, company):
        r = await self._create(client, alice, company)
        iid = r.json()["id"]
        assert (await client.delete(f"/api/v1/interactions/{iid}", headers=_hdr(bob))).status_code == 404
        assert (await client.delete(f"/api/v1/interactions/{iid}", headers=_hdr(alice))).status_code == 204


# ---------------------------------------------------------------------------
# Deal notes
# ---------------------------------------------------------------------------


class TestDealNoteScoping:
    @pytest_asyncio.fixture
    async def deal(self, db_session, company):
        d = Deal(id=uuid.uuid4(), company_id=company.id, title="Big deal", value=1000)
        db_session.add(d)
        await db_session.flush()
        return d

    async def test_notes_scoped_and_view_as(self, client, alice, bob, boss, deal):
        await client.post(
            f"/api/v1/deals/{deal.id}/notes", json={"content": "alice note"}, headers=_hdr(alice)
        )
        await client.post(
            f"/api/v1/deals/{deal.id}/notes", json={"content": "bob note"}, headers=_hdr(bob)
        )
        r = await client.get(f"/api/v1/deals/{deal.id}/notes", headers=_hdr(alice))
        assert {n["content"] for n in r.json()} == {"alice note"}
        # admin all
        r = await client.get(
            f"/api/v1/deals/{deal.id}/notes?view_as_user_id=all", headers=_hdr(boss)
        )
        assert {n["content"] for n in r.json()} == {"alice note", "bob note"}


# ---------------------------------------------------------------------------
# Team members + scope validation
# ---------------------------------------------------------------------------


class TestTeamMembersAndScopeValidation:
    async def test_team_members_privileged_only(self, client, alice, boss):
        r = await client.get("/api/v1/auth/team-members", headers=_hdr(boss))
        assert r.status_code == 200
        emails = {u["email"] for u in r.json()}
        assert {"alice@corp.com", "boss@corp.com"} <= emails
        # Regular user forbidden.
        assert (await client.get("/api/v1/auth/team-members", headers=_hdr(alice))).status_code == 403

    async def test_invalid_view_as_400(self, client, boss):
        r = await client.get("/api/v1/tasks?view_as_user_id=not-a-uuid", headers=_hdr(boss))
        assert r.status_code == 400

    async def test_view_as_unknown_user_404(self, client, boss):
        r = await client.get(
            f"/api/v1/tasks?view_as_user_id={uuid.uuid4()}", headers=_hdr(boss)
        )
        assert r.status_code == 404

    async def test_manager_can_view_as(self, client, db_session, alice, company):
        manager = await _mk_user(db_session, "mgr@corp.com", "manager")
        await client.post(
            "/api/v1/tasks",
            json={"company_id": str(company.id), "title": "alice-task", "task_type": "call"},
            headers=_hdr(alice),
        )
        r = await client.get(
            f"/api/v1/tasks?view_as_user_id={alice.id}", headers=_hdr(manager)
        )
        assert r.status_code == 200
        assert {t["title"] for t in r.json()} == {"alice-task"}
