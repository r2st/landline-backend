"""Workflow execution engine (feature 4.3).

Given an internal event, the engine:

1. finds active :class:`Workflow` rows whose ``trigger_event`` matches and whose
   org scope matches the event,
2. evaluates each workflow's ``trigger_conditions`` against the event payload,
3. for matches, creates a :class:`WorkflowRun` and executes its steps in order,
   honoring per-step ``condition`` gates and ``on_success`` / ``on_failure``
   branching,
4. records per-step results and a terminal status on the run.

Steps invoke the **action registry** (:data:`ACTIONS`) — thin adapters over
existing Landline services (AI email generation, lead scoring, task creation,
deal updates, Slack/webhook notifications). Actions receive a mutable ``ctx``
seeded from the trigger payload; their outputs are merged back into ``ctx`` so
later steps can reference them (e.g. a generated email body).

Everything is defensive: a failing step is captured on the run (not raised), and
the engine's public methods never propagate exceptions to the event emitter.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workflow import Workflow, WorkflowRun

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------


def _resolve_field(ctx: dict, path: str) -> Any:
    """Look up a dotted path in the context dict (``lead.email_opens``)."""
    cur: Any = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def _eval_leaf(cond: dict, ctx: dict) -> bool:
    field = cond.get("field")
    op = cond.get("op", "eq")
    expected = cond.get("value")
    actual = _resolve_field(ctx, field) if field else None

    try:
        if op == "exists":
            return actual is not None
        if op == "eq":
            return actual == expected
        if op == "ne":
            return actual != expected
        if op in ("gt", "gte", "lt", "lte"):
            if actual is None:
                return False
            a, e = float(actual), float(expected)
            return {"gt": a > e, "gte": a >= e, "lt": a < e, "lte": a <= e}[op]
        if op == "contains":
            return expected in (actual or [])
        if op == "in":
            return actual in (expected or [])
    except (TypeError, ValueError):
        return False
    return False


def evaluate_conditions(conditions: Optional[dict], ctx: dict) -> bool:
    """Evaluate a condition tree against ``ctx``.

    Supports ``{"all": [...]}`` (AND), ``{"any": [...]}`` (OR), and a bare leaf
    ``{"field","op","value"}``. An empty/None condition always matches.
    """
    if not conditions:
        return True
    if "all" in conditions:
        return all(evaluate_conditions(c, ctx) for c in conditions["all"])
    if "any" in conditions:
        return any(evaluate_conditions(c, ctx) for c in conditions["any"])
    return _eval_leaf(conditions, ctx)


# ---------------------------------------------------------------------------
# Action registry — thin adapters over existing services
# ---------------------------------------------------------------------------

ActionFn = Callable[[AsyncSession, dict, dict], Awaitable[dict]]
ACTIONS: dict[str, ActionFn] = {}


def action(name: str):
    def deco(fn: ActionFn) -> ActionFn:
        ACTIONS[name] = fn
        return fn

    return deco


@action("generate_email")
async def _generate_email(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """AI-generate a personalized outreach email (feature 1.x reuse)."""
    from app.services.ai_service import get_ai_service

    result = await get_ai_service().generate_email(
        company_name=params.get("company_name") or _resolve_field(ctx, "company.name") or "",
        contact_name=params.get("contact_name") or _resolve_field(ctx, "contact.name") or "",
        contact_title=params.get("contact_title") or _resolve_field(ctx, "contact.title") or "",
        industry=params.get("industry") or _resolve_field(ctx, "company.industry") or "manufacturing",
        purpose=params.get("purpose", "initial outreach"),
    )
    return {"email": result}


@action("ai_generate")
async def _ai_generate(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Generic single-shot AI JSON completion from a prompt template."""
    from app.services.ai_service import get_ai_service

    prompt = params.get("prompt", "")
    for key, val in ctx.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", str(val))
    out = await get_ai_service().complete_json(prompt, max_tokens=params.get("max_tokens", 1024))
    return {"ai": out}


@action("score_lead")
async def _score_lead(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Re-score the company referenced by the event."""
    from app.models.company import Company
    from app.services.scoring import score_company_rule_based

    company_id = params.get("company_id") or _resolve_field(ctx, "company.id") or _resolve_field(ctx, "company_id")
    if not company_id:
        return {"skipped": "no company_id"}
    company = (
        await db.execute(select(Company).where(Company.id == _as_uuid(company_id)))
    ).scalar_one_or_none()
    if company is None:
        return {"skipped": "company not found"}
    score = await score_company_rule_based(db, company)
    return {"fit_score": score.fit_score, "intent_score": getattr(score, "intent_score", None)}


def _one_of(value: Any, allowed: tuple[str, ...], label: str) -> None:
    """Refuse a step param that a CHECK constraint would refuse at flush time.

    The columns these actions write are enums twice over: the REST schemas run a
    ``field_validator`` against the same tuples, and the tables carry a
    ``CHECK … IN (…)``. A workflow step reaches neither. ``params`` is free-form
    JSON that :func:`app.routers.workflows._validate_steps` checks only for a
    known ``action`` name, so ``{"stage": "Closed Won"}`` — an ordinary thing for
    an operator to type, given that is what every other CRM calls it — is stored,
    accepted, and only refused by Postgres, in a worker, at flush.

    Failing there rather than here is not a smaller version of the same failure.
    The engine catches a step's exception and records the step as failed, but the
    session that raised is now in a rolled-back-pending state, so the ``flush``
    that writes ``run.step_results`` raises ``PendingRollbackError`` on the way
    out of :meth:`WorkflowEngine.execute_workflow` — past the per-step handler,
    which is not around it. One mistyped enum therefore loses the whole run
    record, takes down every later step, and 500s the caller. Raising a
    ``ValueError`` before the write keeps it a step failure, which is what it is.

    Not enforced against the DB's constraint list at runtime on purpose: these
    tuples are the schemas' own, so drifting from them shows up in
    ``tests/test_workflow_action_validation.py`` rather than in a worker.
    """
    if value not in allowed:
        raise ValueError(f"{label} must be one of {allowed}, got {value!r}")


@action("create_task")
async def _create_task(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Create a follow-up Task."""
    from app.models.task import Task
    from app.schemas.task import VALID_PRIORITIES, VALID_TASK_TYPES

    company_id = params.get("company_id") or _resolve_field(ctx, "company.id") or _resolve_field(ctx, "company_id")
    if not company_id:
        return {"skipped": "no company_id"}
    priority = params.get("priority", "medium")
    task_type = params.get("task_type", "follow_up")
    _one_of(priority, VALID_PRIORITIES, "priority")
    _one_of(task_type, VALID_TASK_TYPES, "task_type")
    task = Task(
        company_id=_as_uuid(company_id),
        title=params.get("title", "Workflow follow-up"),
        description=params.get("description"),
        priority=priority,
        task_type=task_type,
    )
    db.add(task)
    await db.flush()
    return {"task_id": str(task.id)}


@action("update_deal_stage")
async def _update_deal_stage(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Advance/set a deal's stage."""
    from app.models.deal import Deal
    from app.schemas.deal import VALID_DEAL_STAGES

    deal_id = params.get("deal_id") or _resolve_field(ctx, "deal.id") or _resolve_field(ctx, "deal_id")
    stage = params.get("stage")
    if not deal_id or not stage:
        return {"skipped": "deal_id and stage required"}
    _one_of(stage, VALID_DEAL_STAGES, "stage")
    deal = (
        await db.execute(select(Deal).where(Deal.id == _as_uuid(deal_id)))
    ).scalar_one_or_none()
    if deal is None:
        return {"skipped": "deal not found"}
    deal.deal_stage = stage
    await db.flush()
    return {"deal_id": str(deal.id), "stage": stage}


@action("send_notification")
async def _send_notification(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Send an internal notification email about the event."""
    from app.services.notifications import get_notification_service

    svc = get_notification_service()
    await svc._send_notification(
        params.get("subject", "Workflow notification"),
        params.get("body", ""),
    )
    return {"notified": True}


@action("slack_notify")
async def _slack_notify(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Post a Slack message via the tenant's Slack integration (4.2 reuse)."""
    from app.routers.integrations import run_integration_action

    text = params.get("text", "")
    for key, val in ctx.items():
        text = text.replace(f"{{{{{key}}}}}", str(val))
    org_id = ctx.get("_org_id")
    result = await run_integration_action(
        db, org_id, "slack", "send_notification", {"text": text, "channel": params.get("channel")}
    )
    return {"slack": result}


@action("emit_webhook")
async def _emit_webhook(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Fan the event out to tenant webhook subscriptions (4.2 reuse)."""
    from app.services.webhook_dispatch import get_webhook_dispatcher

    org_id = ctx.get("_org_id")
    events = await get_webhook_dispatcher().emit(
        db,
        params.get("event_type", "workflow.custom"),
        params.get("payload") or {"context": {k: v for k, v in ctx.items() if not k.startswith("_")}},
        _as_uuid(org_id) if org_id else None,
    )
    return {"deliveries": len(events)}


def _as_uuid(value: Any) -> Optional[uuid.UUID]:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class WorkflowEngine:
    """Finds and executes workflows for internal events."""

    async def dispatch_event(
        self,
        db: AsyncSession,
        event_type: str,
        payload: dict,
        org_id: Optional[uuid.UUID] = None,
    ) -> dict:
        """Run every active workflow triggered by ``event_type``."""
        query = select(Workflow).where(
            Workflow.trigger_event == event_type,
            Workflow.is_active.is_(True),
            Workflow.org_id == org_id,
        )
        workflows = (await db.execute(query)).scalars().all()

        runs = []
        capped = []
        for wf in workflows:
            ctx = self._seed_context(payload, org_id)
            if not evaluate_conditions(wf.trigger_conditions, ctx):
                continue
            if await self._over_daily_cap(db, wf):
                logger.warning(
                    "Workflow %s (%s) held: max_runs_per_day=%s already spent",
                    wf.id, wf.name, wf.max_runs_per_day,
                )
                capped.append({"workflow_id": str(wf.id), "status": "capped"})
                continue
            run = await self.execute_workflow(db, wf, payload, org_id)
            runs.append({"workflow_id": str(wf.id), "run_id": str(run.id), "status": run.status})

        # ``workflows_matched`` counts what ran, as it always has. A held
        # workflow is reported separately rather than folded in: the caller of
        # ``/workflows/emit`` needs to be able to tell "your conditions didn't
        # match" from "your conditions matched and the cap is spent", and those
        # are the same number otherwise.
        return {"workflows_matched": len(runs), "runs": runs, "capped": capped}

    async def _over_daily_cap(self, db: AsyncSession, workflow: Workflow) -> bool:
        """Has this workflow already run ``max_runs_per_day`` times today (UTC)?

        The column has existed since 4.3, is editable through ``POST``/``PATCH
        /workflows``, is returned by every read, and is documented on the model
        as the guard "against runaway self-triggering loops". Nothing read it.
        An operator setting 50 got no bound at all — and the loop it names is
        reachable: ``emit_webhook`` and ``slack_notify`` put events back out, and
        every run can spend ``ai_generate`` and ``generate_email`` LLM calls, so
        the cost of an unbounded trigger is not just rows.

        Counted, deliberately, from ``workflow_runs`` rather than a counter:
        the run table is the record the cap is *about*, it survives a restart,
        and it is what the operator is shown on ``/workflows/{id}/runs`` — a cap
        enforced against a number nobody can see is the harder thing to debug.

        Advisory, not an interlock. Two workers dispatching the same event
        concurrently can both read a count under the cap and both run; the
        overshoot is bounded by worker concurrency, not by the day. That is the
        right trade here — this bounds runaway cost, it is not a compliance
        floor — and it is the same reasoning that makes ``can_send_today`` safe
        as a pre-check and unsafe as a gate. Anything needing exactness wants
        the Redis reservation shape from ``SendLimiter`` instead.
        """
        cap = workflow.max_runs_per_day
        if cap is None:
            return False
        if cap <= 0:
            # Not the same as unset. ``0`` is an operator switching a workflow
            # off without deactivating it, and must hold rather than divide.
            return True
        since = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        count = (
            await db.execute(
                select(func.count())
                .select_from(WorkflowRun)
                .where(
                    WorkflowRun.workflow_id == workflow.id,
                    WorkflowRun.started_at >= since,
                    # A run held by this very cap is recorded as ``skipped`` by
                    # nothing today, but manual runs and future statuses share
                    # the table — count only executions, or a cap that records
                    # its own refusals would ratchet itself shut.
                    WorkflowRun.status != "skipped",
                )
            )
        ).scalar_one()
        return count >= cap

    def _seed_context(self, payload: dict, org_id: Optional[uuid.UUID]) -> dict:
        ctx = dict(payload or {})
        ctx["_org_id"] = str(org_id) if org_id else None
        return ctx

    async def execute_workflow(
        self,
        db: AsyncSession,
        workflow: Workflow,
        payload: dict,
        org_id: Optional[uuid.UUID] = None,
    ) -> WorkflowRun:
        """Execute one workflow's steps against a trigger payload."""
        run = WorkflowRun(
            workflow_id=workflow.id,
            org_id=org_id if org_id is not None else workflow.org_id,
            status="running",
            trigger_payload=payload or {},
            step_results=[],
        )
        db.add(run)
        await db.flush()

        ctx = self._seed_context(payload, run.org_id)
        results: list[dict] = []
        steps = workflow.steps or []
        # Index steps by id for explicit branching jumps.
        by_id = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}

        i = 0
        guard = 0
        max_iter = len(steps) * 4 + 8  # cheap infinite-branch-loop guard
        while 0 <= i < len(steps) and guard < max_iter:
            guard += 1
            step = steps[i]
            step_id = step.get("id", f"step{i}")
            action_name = step.get("action")

            # Per-step condition gate (branching by skipping).
            if not evaluate_conditions(step.get("condition"), ctx):
                results.append({"step_id": step_id, "action": action_name, "status": "skipped"})
                i = self._next_index(step, by_id, i, success=False, skipped=True)
                continue

            fn = ACTIONS.get(action_name)
            if fn is None:
                results.append(
                    {"step_id": step_id, "action": action_name, "status": "failed",
                     "error": f"unknown action {action_name!r}"}
                )
                i = self._next_index(step, by_id, i, success=False)
                continue

            try:
                # Each step runs inside a SAVEPOINT so that "a failing step is
                # captured on the run, not raised" — the promise in this
                # module's docstring — holds for the failures that come from the
                # *database* rather than from Python.
                #
                # Those are the ones this engine is most exposed to, because
                # ``params`` is free-form JSON: a ``company_id`` naming a row
                # that has since been deleted is a foreign-key violation, and
                # every enum column these actions write has a CHECK behind it.
                # Catching that exception is not enough on its own. The session
                # it came out of is in a rolled-back-pending state, so the
                # ``flush`` at the bottom of this method — outside the per-step
                # handler — raises ``PendingRollbackError`` and takes the run
                # record, the remaining steps and the caller's request with it.
                # Rolling back to the savepoint returns the session to the state
                # the step started in, and the run continues and is recorded.
                async with db.begin_nested():
                    output = await fn(db, step.get("params", {}) or {}, ctx)
                ctx.update({k: v for k, v in output.items() if not k.startswith("_")})
                results.append(
                    {"step_id": step_id, "action": action_name, "status": "completed", "output": output}
                )
                i = self._next_index(step, by_id, i, success=True)
            except Exception as exc:  # noqa: BLE001 — a step failure fails the step, not the run
                logger.exception("Workflow %s step %s failed", workflow.id, step_id)
                results.append(
                    {"step_id": step_id, "action": action_name, "status": "failed", "error": str(exc)}
                )
                i = self._next_index(step, by_id, i, success=False)

        if guard >= max_iter and 0 <= i < len(steps):
            # The guard fired: `on_success`/`on_failure` point backwards and the
            # workflow is a cycle. It was silently indistinguishable from a
            # workflow that ran to the end — the loop exited, no result recorded
            # why, and a run truncated mid-cycle was reported "completed" so long
            # as every step it *did* reach succeeded. That is the one outcome an
            # operator most needs told, because the steps have real side effects:
            # a two-step cycle on `generate_email` ran the LLM `max_iter` times
            # and then reported a clean run.
            logger.error(
                "Workflow %s stopped after %s steps: branch targets form a loop",
                workflow.id, guard,
            )
            results.append(
                {"step_id": steps[i].get("id", f"step{i}"),
                 "action": steps[i].get("action"),
                 "status": "failed",
                 "error": (
                     f"stopped after {guard} steps: branch targets form a loop "
                     f"(limit {max_iter})"
                 )}
            )

        run.step_results = results
        run.status = "failed" if any(r["status"] == "failed" for r in results) else "completed"
        run.completed_at = datetime.now(timezone.utc)
        await db.flush()
        return run

    def _next_index(
        self, step: dict, by_id: dict, current: int, success: bool, skipped: bool = False
    ) -> int:
        """Resolve the next step index, honoring explicit branch targets."""
        key = "on_success" if success else "on_failure"
        target = step.get(key)
        if skipped and step.get("on_skip"):
            target = step["on_skip"]
        if target is None:
            return current + 1  # fall through to the next step
        if target in ("__end__", "end", "stop"):
            return -1
        return by_id.get(target, current + 1)


_engine: Optional[WorkflowEngine] = None


def get_workflow_engine() -> WorkflowEngine:
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine
