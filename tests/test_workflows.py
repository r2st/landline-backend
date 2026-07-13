"""Tests for the AI Workflow Builder (feature 4.3).

Covers condition evaluation, the action registry adapters, the execution engine
(step gating + branching + run tracking), the event bus dispatch path, and the
workflow API (CRUD, step validation, manual run, emit, run history).
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.company import Company
from app.models.task import Task
from app.services.workflow_engine import evaluate_conditions, get_workflow_engine


async def _seed_company(db_engine, name="Acme Stamping") -> uuid.UUID:
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        company = Company(id=uuid.uuid4(), name=name, country="US", source="test")
        s.add(company)
        await s.commit()
        return company.id


# --- Engine execution (async) ---------------------------------------------


@pytest.mark.asyncio
async def test_execute_workflow_runs_action_and_creates_task(db_session, db_engine):
    company_id = await _seed_company(db_engine)
    # db_session and db_engine share the same in-memory DB (see conftest).
    from app.models.workflow import Workflow

    wf = Workflow(
        id=uuid.uuid4(),
        name="Follow up on reply",
        trigger_event="lead.replied",
        steps=[
            {
                "id": "s1",
                "action": "create_task",
                "params": {"company_id": str(company_id), "title": "Call them back"},
            }
        ],
    )
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(
        db_session, wf, {"company_id": str(company_id)}
    )
    assert run.status == "completed"
    assert run.step_results[0]["status"] == "completed"

    tasks = (await db_session.execute(select(Task))).scalars().all()
    assert len(tasks) == 1
    assert tasks[0].title == "Call them back"


@pytest.mark.asyncio
async def test_step_condition_gates_execution(db_session, db_engine):
    company_id = await _seed_company(db_engine)
    from app.models.workflow import Workflow

    wf = Workflow(
        id=uuid.uuid4(),
        name="High-intent only",
        trigger_event="email.opened",
        steps=[
            {
                "id": "s1",
                "action": "create_task",
                "condition": {"field": "email_opens", "op": "gte", "value": 3},
                "params": {"company_id": str(company_id), "title": "Hot lead"},
            }
        ],
    )
    db_session.add(wf)
    await db_session.flush()

    # Below threshold → step skipped, no task.
    run_low = await get_workflow_engine().execute_workflow(db_session, wf, {"email_opens": 1})
    assert run_low.step_results[0]["status"] == "skipped"
    assert (await db_session.execute(select(Task))).scalars().all() == []

    # At/above threshold → step runs.
    run_hi = await get_workflow_engine().execute_workflow(db_session, wf, {"email_opens": 5})
    assert run_hi.step_results[0]["status"] == "completed"
    assert len((await db_session.execute(select(Task))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_branching_on_failure_jumps_to_target(db_session):
    from app.models.workflow import Workflow

    # step1 fails (unknown action) → on_failure jumps to "recover"; the middle
    # step "s2" is skipped by the jump.
    wf = Workflow(
        id=uuid.uuid4(),
        name="Branching",
        trigger_event="test.branch",
        steps=[
            {"id": "s1", "action": "does_not_exist", "params": {}, "on_failure": "recover"},
            {"id": "s2", "action": "send_notification", "params": {"subject": "should be skipped"}},
            {"id": "recover", "action": "send_notification", "params": {"subject": "recovered"}},
        ],
    )
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})
    executed = [r["step_id"] for r in run.step_results]
    assert "recover" in executed
    assert "s2" not in executed  # jumped past the middle step
    assert run.status == "failed"  # s1 failed


@pytest.mark.asyncio
async def test_on_success_branch_skips_middle_step(db_session):
    from app.models.workflow import Workflow

    wf = Workflow(
        id=uuid.uuid4(),
        name="Success branch",
        trigger_event="test.branch2",
        steps=[
            {"id": "s1", "action": "send_notification", "params": {"subject": "go"}, "on_success": "final"},
            {"id": "s2", "action": "send_notification", "params": {"subject": "skip me"}},
            {"id": "final", "action": "send_notification", "params": {"subject": "done"}},
        ],
    )
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})
    executed = [r["step_id"] for r in run.step_results]
    assert executed == ["s1", "final"]
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_ai_generate_email_action(db_session, db_engine, monkeypatch):
    company_id = await _seed_company(db_engine)
    from app.models.workflow import Workflow

    class _FakeAI:
        async def generate_email(self, **kwargs):
            return {"subject": "quick question", "body": "hi there", "model": "claude-sonnet-4-6"}

    monkeypatch.setattr("app.services.ai_service.get_ai_service", lambda: _FakeAI())

    wf = Workflow(
        id=uuid.uuid4(),
        name="Draft email",
        trigger_event="lead.qualified",
        steps=[{"id": "s1", "action": "generate_email", "params": {"company_name": "Acme"}}],
    )
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {"company_id": str(company_id)})
    assert run.status == "completed"
    assert run.step_results[0]["output"]["email"]["subject"] == "quick question"


@pytest.mark.asyncio
async def test_unknown_action_fails_step_not_run(db_session):
    from app.models.workflow import Workflow

    wf = Workflow(
        id=uuid.uuid4(),
        name="Bad",
        trigger_event="x",
        steps=[{"id": "s1", "action": "does_not_exist", "params": {}}],
    )
    db_session.add(wf)
    await db_session.flush()
    run = await get_workflow_engine().execute_workflow(db_session, wf, {})
    assert run.status == "failed"
    assert run.step_results[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_dispatch_event_matches_trigger_conditions(db_session, db_engine):
    company_id = await _seed_company(db_engine)
    from app.models.workflow import Workflow

    wf = Workflow(
        id=uuid.uuid4(),
        name="On strong reply",
        trigger_event="lead.replied",
        trigger_conditions={"all": [{"field": "email_opens", "op": "gte", "value": 3}]},
        steps=[{"id": "s1", "action": "create_task", "params": {"company_id": str(company_id), "title": "T"}}],
    )
    db_session.add(wf)
    await db_session.flush()

    # Non-matching payload → no run.
    miss = await get_workflow_engine().dispatch_event(db_session, "lead.replied", {"email_opens": 1})
    assert miss["workflows_matched"] == 0

    # Matching payload → one run.
    hit = await get_workflow_engine().dispatch_event(db_session, "lead.replied", {"email_opens": 9})
    assert hit["workflows_matched"] == 1
    assert hit["runs"][0]["status"] == "completed"


# --- API (async) -----------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_crud_api(client):
    created = await client.post(
        "/api/v1/workflows",
        json={
            "name": "My workflow",
            "trigger_event": "lead.replied",
            "steps": [{"id": "s1", "action": "send_notification", "params": {"subject": "hi"}}],
        },
    )
    assert created.status_code == 201, created.text
    wid = created.json()["id"]

    got = await client.get(f"/api/v1/workflows/{wid}")
    assert got.json()["trigger_event"] == "lead.replied"

    patched = await client.patch(f"/api/v1/workflows/{wid}", json={"is_active": False})
    assert patched.json()["is_active"] is False

    listed = await client.get("/api/v1/workflows")
    assert len(listed.json()) == 1

    deleted = await client.delete(f"/api/v1/workflows/{wid}")
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_create_workflow_rejects_unknown_action(client):
    resp = await client.post(
        "/api/v1/workflows",
        json={"name": "bad", "trigger_event": "x", "steps": [{"action": "teleport"}]},
    )
    assert resp.status_code == 400
    assert "unknown action" in resp.text


@pytest.mark.asyncio
async def test_list_actions_endpoint(client):
    resp = await client.get("/api/v1/workflows/actions")
    actions = resp.json()["actions"]
    assert "create_task" in actions and "generate_email" in actions


@pytest.mark.asyncio
async def test_run_and_run_history_api(client, db_engine):
    company_id = await _seed_company(db_engine)
    created = await client.post(
        "/api/v1/workflows",
        json={
            "name": "Runnable",
            "trigger_event": "manual",
            "steps": [{"id": "s1", "action": "create_task", "params": {"company_id": str(company_id), "title": "Do it"}}],
        },
    )
    wid = created.json()["id"]

    run = await client.post(f"/api/v1/workflows/{wid}/run", json={"payload": {}})
    assert run.status_code == 200
    assert run.json()["status"] == "completed"

    history = await client.get(f"/api/v1/workflows/{wid}/runs")
    assert len(history.json()) == 1


@pytest.mark.asyncio
async def test_emit_event_api_triggers_workflow(client, db_engine):
    company_id = await _seed_company(db_engine)
    await client.post(
        "/api/v1/workflows",
        json={
            "name": "Emit target",
            "trigger_event": "deal.won",
            "steps": [{"id": "s1", "action": "create_task", "params": {"company_id": str(company_id), "title": "Celebrate"}}],
        },
    )
    resp = await client.post(
        "/api/v1/workflows/emit", json={"event_type": "deal.won", "payload": {}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workflows_matched"] == 1
    assert body["event_type"] == "deal.won"


# --- Pure sync unit tests (kept last — see test ordering note) -------------


def test_evaluate_conditions_operators():
    ctx = {"email_opens": 5, "stage": "engaged", "tags": ["hot", "vip"]}
    assert evaluate_conditions({"field": "email_opens", "op": "gte", "value": 3}, ctx) is True
    assert evaluate_conditions({"field": "email_opens", "op": "lt", "value": 3}, ctx) is False
    assert evaluate_conditions({"field": "stage", "op": "eq", "value": "engaged"}, ctx) is True
    assert evaluate_conditions({"field": "tags", "op": "contains", "value": "vip"}, ctx) is True
    assert evaluate_conditions({"field": "missing", "op": "exists"}, ctx) is False
    assert evaluate_conditions(None, ctx) is True  # empty always matches


def test_evaluate_conditions_and_or_nested():
    ctx = {"opens": 4, "job_posted": True}
    all_cond = {"all": [{"field": "opens", "op": "gte", "value": 3}, {"field": "job_posted", "op": "eq", "value": True}]}
    assert evaluate_conditions(all_cond, ctx) is True
    any_cond = {"any": [{"field": "opens", "op": "gte", "value": 99}, {"field": "job_posted", "op": "eq", "value": True}]}
    assert evaluate_conditions(any_cond, ctx) is True
    fail = {"all": [{"field": "opens", "op": "gte", "value": 99}]}
    assert evaluate_conditions(fail, ctx) is False


def test_dotted_field_resolution():
    ctx = {"company": {"name": "Acme", "industry": "cnc"}}
    assert evaluate_conditions({"field": "company.name", "op": "eq", "value": "Acme"}, ctx) is True
    assert evaluate_conditions({"field": "company.missing", "op": "exists"}, ctx) is False
