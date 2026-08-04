"""The workflow engine's exposure to free-form ``params``.

Three related holes, all downstream of the same fact: a workflow step is a JSON
blob an operator writes, and ``app.routers.workflows._validate_steps`` checks
exactly one thing about it — that ``action`` names a registered action. Nothing
looks at ``params`` at all, so every constraint the REST API enforces on the same
write (a ``field_validator`` on the schema, a ``CHECK … IN (…)`` on the table) is
simply absent on this path.

1. **Enum params reach the database unvalidated.** ``update_deal_stage`` with
   ``{"stage": "Closed Won"}`` and ``create_task`` with ``{"priority": "P1"}``
   are ordinary things for an operator to type — that is what other CRMs call
   them — and are refused only by Postgres, in a worker, at flush.
2. **A step whose *database* write fails takes the whole run with it.** The
   engine catches the step's exception, but the session is left
   rolled-back-pending, so the ``flush`` that writes ``run.step_results`` raises
   on the way out — past the per-step handler, which is not around it.
3. **``max_runs_per_day`` was never read.** Stored, exposed on every read,
   editable via ``PATCH``, documented on the model as the guard "against runaway
   self-triggering loops", enforced nowhere.

These are pinned here rather than in ``test_workflows.py`` because none of them
is visible under the test schema by accident: ``conftest`` strips every CHECK
constraint for SQLite, so the enum writes *succeed* in tests and fail only in
production. The tests below therefore assert on the validation, not on the
database's refusal of it.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.company import Company
from app.models.deal import Deal
from app.models.task import Task
from app.models.workflow import Workflow, WorkflowRun
from app.org_context import APPREND_ORG_ID
from app.services.workflow_engine import get_workflow_engine


async def _seed_deal(db_session, stage="discovery") -> Deal:
    company = Company(id=uuid.uuid4(), name="Acme Stamping", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    deal = Deal(
        id=uuid.uuid4(), company_id=company.id, title="Line upgrade",
        value=1000, deal_stage=stage,
    )
    db_session.add(deal)
    await db_session.flush()
    return deal


def _workflow(steps, **kw) -> Workflow:
    return Workflow(
        id=uuid.uuid4(),
        org_id=kw.pop("org_id", APPREND_ORG_ID),
        name=kw.pop("name", "wf"),
        trigger_event=kw.pop("trigger_event", "lead.replied"),
        steps=steps,
        **kw,
    )


# --- 1. Enum params ---------------------------------------------------------


@pytest.mark.asyncio
async def test_update_deal_stage_refuses_a_stage_the_column_would_refuse(db_session):
    deal = await _seed_deal(db_session, stage="discovery")
    wf = _workflow([
        {"id": "s1", "action": "update_deal_stage",
         "params": {"deal_id": str(deal.id), "stage": "Closed Won"}},
    ])
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})

    assert run.status == "failed"
    assert run.step_results[0]["status"] == "failed"
    assert "must be one of" in run.step_results[0]["error"]
    # And the deal is untouched — the point of refusing before the write.
    await db_session.refresh(deal)
    assert deal.deal_stage == "discovery"


@pytest.mark.asyncio
async def test_update_deal_stage_still_accepts_a_valid_stage(db_session):
    deal = await _seed_deal(db_session, stage="discovery")
    wf = _workflow([
        {"id": "s1", "action": "update_deal_stage",
         "params": {"deal_id": str(deal.id), "stage": "negotiation"}},
    ])
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})

    assert run.status == "completed"
    await db_session.refresh(deal)
    assert deal.deal_stage == "negotiation"


@pytest.mark.parametrize(
    "params,bad",
    [
        ({"priority": "P1"}, "priority"),
        ({"task_type": "linkedin"}, "task_type"),
    ],
)
@pytest.mark.asyncio
async def test_create_task_refuses_enum_params_the_column_would_refuse(
    db_session, params, bad
):
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    wf = _workflow([
        {"id": "s1", "action": "create_task",
         "params": {"company_id": str(company.id), "title": "T", **params}},
    ])
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})

    assert run.status == "failed"
    assert bad in run.step_results[0]["error"]
    assert (await db_session.execute(select(Task))).scalars().all() == []


# --- 2. A failing DB write must not take the run with it --------------------


@pytest.mark.asyncio
async def test_a_step_that_raises_mid_write_leaves_the_run_recordable(db_session):
    """A step failing *after* dirtying the session must not leave the write.

    This is the shape the savepoint exists for, reproduced without depending on
    a constraint the test schema doesn't carry: the action writes and then
    raises. Recording the step as "failed" is not enough on its own — without a
    savepoint the write is still in the transaction, so a step the run reports
    as failed committed its half anyway, and a *database* error in the same
    position leaves the session rolled-back-pending, which turns the flush at
    the bottom of ``execute_workflow`` into a ``PendingRollbackError`` raised
    past the per-step handler.
    """
    from app.services import workflow_engine as we

    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()
    deal = await _seed_deal(db_session, stage="discovery")

    async def _half_writes(db, params, ctx):
        d = (await db.execute(select(Deal).where(Deal.id == deal.id))).scalar_one()
        d.deal_stage = "closing"
        await db.flush()
        raise RuntimeError("transport blew up after the write")

    we.ACTIONS["_test_half_write"] = _half_writes
    try:
        wf = _workflow([
            {"id": "s1", "action": "_test_half_write", "params": {}},
            {"id": "s2", "action": "create_task",
             "params": {"company_id": str(company.id), "title": "T"}},
        ])
        db_session.add(wf)
        await db_session.flush()

        # The run is still recordable: step 1 failed, step 2 still ran.
        run = await get_workflow_engine().execute_workflow(db_session, wf, {})
    finally:
        we.ACTIONS.pop("_test_half_write", None)

    assert run.status == "failed"
    assert run.step_results[0]["status"] == "failed"
    assert run.step_results[1]["status"] == "completed"
    # Step 1's write went back to the savepoint — the deal never moved.
    await db_session.refresh(deal)
    assert deal.deal_stage == "discovery"
    # ...and step 2's write, in its own savepoint, stands.
    assert len((await db_session.execute(select(Task))).scalars().all()) == 1


# --- 3. max_runs_per_day ----------------------------------------------------


@pytest.mark.asyncio
async def test_daily_cap_holds_further_event_dispatches(db_session):
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    wf = _workflow(
        [{"id": "s1", "action": "create_task",
          "params": {"company_id": str(company.id), "title": "T"}}],
        max_runs_per_day=2,
    )
    db_session.add(wf)
    await db_session.flush()

    engine = get_workflow_engine()
    for _ in range(2):
        result = await engine.dispatch_event(db_session, "lead.replied", {}, APPREND_ORG_ID)
        assert result["workflows_matched"] == 1

    third = await engine.dispatch_event(db_session, "lead.replied", {}, APPREND_ORG_ID)
    assert third["workflows_matched"] == 0
    assert third["capped"] == [{"workflow_id": str(wf.id), "status": "capped"}]

    # Two runs, two tasks — the third produced neither.
    runs = (await db_session.execute(select(WorkflowRun))).scalars().all()
    assert len(runs) == 2
    assert len((await db_session.execute(select(Task))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_no_cap_set_is_unbounded(db_session):
    """``None`` is unset, and must not be read as zero."""
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    wf = _workflow(
        [{"id": "s1", "action": "create_task",
          "params": {"company_id": str(company.id), "title": "T"}}],
        max_runs_per_day=None,
    )
    db_session.add(wf)
    await db_session.flush()

    engine = get_workflow_engine()
    for _ in range(5):
        result = await engine.dispatch_event(db_session, "lead.replied", {}, APPREND_ORG_ID)
        assert result["workflows_matched"] == 1
        assert result["capped"] == []


@pytest.mark.asyncio
async def test_zero_cap_holds_everything(db_session):
    """``0`` is an operator switching a workflow off without deactivating it."""
    wf = _workflow([{"id": "s1", "action": "create_task", "params": {}}], max_runs_per_day=0)
    db_session.add(wf)
    await db_session.flush()

    result = await get_workflow_engine().dispatch_event(
        db_session, "lead.replied", {}, APPREND_ORG_ID
    )
    assert result["workflows_matched"] == 0
    assert result["capped"][0]["status"] == "capped"
    assert (await db_session.execute(select(WorkflowRun))).scalars().all() == []


@pytest.mark.asyncio
async def test_manual_run_is_not_refused_by_the_cap_but_counts_towards_it(db_session):
    """The cap binds autonomous volume, mirroring ``refusable`` on the send caps.

    An operator hitting Run on a workflow whose day is spent is asking a
    question; the runaway loop this guard exists for has no operator in it. So
    the manual path is never refused — and does count, because a run that spends
    the same LLM calls and writes the same rows is the thing being bounded.
    """
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    wf = _workflow(
        [{"id": "s1", "action": "create_task",
          "params": {"company_id": str(company.id), "title": "T"}}],
        max_runs_per_day=1,
    )
    db_session.add(wf)
    await db_session.flush()

    engine = get_workflow_engine()
    # Manual runs go straight to execute_workflow (as POST /workflows/{id}/run
    # does) and are not refused, however many.
    for _ in range(3):
        run = await engine.execute_workflow(db_session, wf, {}, APPREND_ORG_ID)
        assert run.status == "completed"

    # But they were counted: the event path is now held.
    result = await engine.dispatch_event(db_session, "lead.replied", {}, APPREND_ORG_ID)
    assert result["workflows_matched"] == 0


# --- 4. The branch-loop guard reports itself --------------------------------


@pytest.mark.asyncio
async def test_a_branch_cycle_is_reported_not_silently_truncated(db_session):
    """Two steps pointing at each other used to report a clean "completed" run."""
    company = Company(id=uuid.uuid4(), name="Acme", country="US", source="test")
    db_session.add(company)
    await db_session.flush()

    params = {"company_id": str(company.id), "title": "T"}
    wf = _workflow([
        {"id": "s1", "action": "create_task", "params": params, "on_success": "s2"},
        {"id": "s2", "action": "create_task", "params": params, "on_success": "s1"},
    ])
    db_session.add(wf)
    await db_session.flush()

    run = await get_workflow_engine().execute_workflow(db_session, wf, {})

    assert run.status == "failed"
    last = run.step_results[-1]
    assert last["status"] == "failed"
    assert "form a loop" in last["error"]
