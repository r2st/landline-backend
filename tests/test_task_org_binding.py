"""Every Celery task binds a tenant before it touches tenant data.

A background task runs outside the request lifecycle, so nothing sets the org
context. With none bound, ``do_orm_execute`` adds no predicate and
``org_id_for_insert`` falls back to the first-party org — so an unbound sweep
reads *every* tenant's rows and stamps anything it writes as Apprend's. That is
spelled out at the top of :mod:`app.tasks.org_runner`, which exists to prevent
it, and there are two primitives for it: ``for_each_org`` for sweeps and
``org_scope`` for tasks dispatched per record.

This is the check that they were actually used. Nothing enforced it before, and
the failure is the quiet kind: an unbound task *works*. It returns plausible
numbers, writes rows that appear in the first-party org's UI, and logs nothing —
so it looks exactly like a task that works, right up until a customer asks why
their contacts are in someone else's account.

It is a strict rule on purpose. Rather than trying to decide which tasks touch
tenant-owned tables — most reach a service module, and following that is a
static-analysis problem this has no business solving — **every** task must bind
or carry a reasoned entry in :data:`REVIEWED`. There are ~70 tasks and two
exceptions, so the strict version costs nothing and cannot fail open on a call
it couldn't follow.

**Resolution is module-local.** A task that binds inside a helper in its own
module is accepted (``calls._beat``, ``verification._sweep`` and
``places_discovery._run_scheduled_discoveries`` all do); a task that binds via
some other module's helper is not, and would need a REVIEWED entry saying so.
No such task exists today, and the restriction is the point — binding is worth
being able to see from the task itself.
"""

import ast
import pathlib

import pytest

TASKS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app" / "tasks"

# The primitives that bind an org, in the forms they are actually written.
BINDERS = ("for_each_org", "org_scope", "set_current_org_id")

# Tasks that bind nothing, reviewed individually. The reason is part of the
# entry: one without a reason is an unreviewed exception, so don't add one.
REVIEWED: dict[str, str] = {
    "app.tasks.enrichment.enrich_batch": (
        "pure dispatch — it only calls enrich_company.delay(id) per id and "
        "never opens a session. The work it fans out binds per record via "
        "org_scope, which is where the org has to be decided anyway: the "
        "dispatcher is handed ids, not tenants."
    ),
    "app.tasks.lead_monitor.check_lead_gen_health": (
        "deliberately platform-wide — its totals are what a platform_admin sees "
        "on /leads/stats, and binding one org would make them a single tenant's. "
        "Reads only; writes nothing, so there is no insert to misattribute. Note "
        "that running unbound is not the same as reasoning unbound: its alerts "
        "used to be computed on the platform sum and so asked 'is every tenant "
        "dead?' — see the CLAUDE.md section on alarms and sums."
    ),
}


def _task_modules() -> list[pathlib.Path]:
    return sorted(
        p for p in TASKS_ROOT.glob("*.py")
        if p.name not in ("__init__.py", "celery_app.py", "org_runner.py")
    )


def _is_task(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    return any(
        "celery_app.task" in ast.unparse(d) or d_is_shared_task(d)
        for d in node.decorator_list
    )


def d_is_shared_task(decorator: ast.AST) -> bool:
    return "shared_task" in ast.unparse(decorator)


def _module_level_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        n.name: n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _called_names(node: ast.AST) -> set[str]:
    """Every plain name called anywhere in ``node``'s subtree.

    Attribute calls (``service.sweep()``) are deliberately not followed — see
    the module docstring on module-local resolution.
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            out.add(n.func.id)
    return out


def _binds(node: ast.AST, module_fns: dict[str, ast.AST]) -> bool:
    """Whether ``node`` reaches a binder, following module-local calls.

    Nested ``def``s (the ``_run`` / ``_body`` closures nearly every task uses)
    need no special handling — they are inside the subtree already.
    """
    seen: set[str] = set()
    stack = [node]
    while stack:
        current = stack.pop()
        source = ast.unparse(current)
        if any(b in source for b in BINDERS):
            return True
        for name in _called_names(current):
            if name in module_fns and name not in seen:
                seen.add(name)
                stack.append(module_fns[name])
    return False


def _all_tasks() -> list[tuple[str, ast.AST, dict[str, ast.AST]]]:
    found = []
    for path in _task_modules():
        tree = ast.parse(path.read_text())
        module_fns = _module_level_functions(tree)
        dotted_module = f"app.tasks.{path.stem}"
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_task(node):
                found.append((f"{dotted_module}.{node.name}", node, module_fns))
    return found


def test_the_scan_finds_the_tasks():
    """A checker that matches nothing passes forever and reads as coverage."""
    names = {name for name, _, _ in _all_tasks()}
    assert len(names) > 50, f"expected the full task surface, got {len(names)}"
    # Spot-check one of each shape so a decorator change can't empty the scan.
    assert "app.tasks.outreach.send_sequence_step" in names       # org_scope
    assert "app.tasks.outreach.process_outreach_queue" in names   # for_each_org
    assert "app.tasks.calls.generate_morning_call_queue" in names  # via a helper


def test_every_celery_task_binds_an_org():
    unbound = sorted(
        name
        for name, node, module_fns in _all_tasks()
        if name not in REVIEWED and not _binds(node, module_fns)
    )
    assert not unbound, (
        "these Celery tasks never bind an org, so every ORM read in them spans "
        "all tenants and every insert lands on the first-party org: "
        f"{unbound}. Wrap the body in for_each_org (sweeps) or org_scope "
        "(per-record tasks), or add a reasoned REVIEWED entry."
    )


def test_reviewed_entries_all_name_a_real_task():
    """A stale exemption is worse than none — it silently un-checks a task."""
    names = {name for name, _, _ in _all_tasks()}
    stale = sorted(set(REVIEWED) - names)
    assert not stale, f"REVIEWED names tasks that no longer exist: {stale}"


def test_reviewed_entries_carry_a_reason():
    assert all(len(reason) > 40 for reason in REVIEWED.values())


# --- self-tests: a checker nobody has seen fail is a checker nobody trusts ---

_UNBOUND_TASK = """
from app.tasks.celery_app import celery_app
from app.database_celery import celery_session
from app.models.contact import Contact

@celery_app.task(name="app.tasks.fake.sweep")
def sweep():
    async def _run():
        async with celery_session() as db:
            return await db.execute(select(Contact))
    return _run_async(_run())
"""

_BOUND_VIA_HELPER = """
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import for_each_org

def _beat(body, label):
    return for_each_org(body, label=label)

@celery_app.task(name="app.tasks.fake.sweep")
def sweep():
    return _beat(_do_work, "sweep")
"""

_BOUND_IN_A_NESTED_DEF = """
from app.tasks.celery_app import celery_app
from app.tasks.org_runner import org_scope

@celery_app.task(name="app.tasks.fake.one")
def one(record_id):
    async def _run():
        async with org_scope(org_id):
            return await _work(record_id)
    return _run_async(_run())
"""


@pytest.mark.parametrize(
    "source,expected",
    [
        (_UNBOUND_TASK, False),
        (_BOUND_VIA_HELPER, True),
        (_BOUND_IN_A_NESTED_DEF, True),
    ],
    ids=["unbound-is-caught", "helper-indirection-is-followed", "nested-def-counts"],
)
def test_the_checker_separates_bound_from_unbound(source: str, expected: bool):
    tree = ast.parse(source)
    module_fns = _module_level_functions(tree)
    tasks = [n for n in ast.walk(tree) if _is_task(n)]
    assert len(tasks) == 1
    assert _binds(tasks[0], module_fns) is expected
