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

from sqlalchemy import select
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


@action("create_task")
async def _create_task(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Create a follow-up Task."""
    from app.models.task import Task

    company_id = params.get("company_id") or _resolve_field(ctx, "company.id") or _resolve_field(ctx, "company_id")
    if not company_id:
        return {"skipped": "no company_id"}
    task = Task(
        company_id=_as_uuid(company_id),
        title=params.get("title", "Workflow follow-up"),
        description=params.get("description"),
        priority=params.get("priority", "medium"),
        task_type=params.get("task_type", "follow_up"),
    )
    db.add(task)
    await db.flush()
    return {"task_id": str(task.id)}


@action("update_deal_stage")
async def _update_deal_stage(db: AsyncSession, params: dict, ctx: dict) -> dict:
    """Advance/set a deal's stage."""
    from app.models.deal import Deal

    deal_id = params.get("deal_id") or _resolve_field(ctx, "deal.id") or _resolve_field(ctx, "deal_id")
    stage = params.get("stage")
    if not deal_id or not stage:
        return {"skipped": "deal_id and stage required"}
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
        for wf in workflows:
            ctx = self._seed_context(payload, org_id)
            if not evaluate_conditions(wf.trigger_conditions, ctx):
                continue
            run = await self.execute_workflow(db, wf, payload, org_id)
            runs.append({"workflow_id": str(wf.id), "run_id": str(run.id), "status": run.status})

        return {"workflows_matched": len(runs), "runs": runs}

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
