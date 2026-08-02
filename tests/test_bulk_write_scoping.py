"""A bulk ``update()`` / ``delete()`` must scope itself to the org.

``OrgScopedMixin`` isolates reads through a ``do_orm_execute`` hook that injects
``org_id == <caller>`` into every SELECT. The hook checks
``execute_state.is_select``, so a Core ``update()`` or ``delete()`` receives
nothing — while sitting in the same file, in the same style, with the same
absence of a visible org predicate as the scoped reads around it.

This is the same shape as the two checkers next door
(``test_single_row_assertions.py``, ``test_org_scoping_coverage.py``) and exists
for the same reason: **reviewing for it does not work.** In this codebase, not
writing an org predicate is the *correct* shape everywhere else, so an unscoped
bulk write has nothing to notice. Four had gone wrong by the time this was
written:

- ``email_templates`` and ``email_signatures`` cleared every tenant's default
  when one org promoted its own;
- ``companies.bulk-update-stage`` took client-supplied ids and moved any company
  on the platform to any stage — a write IDOR — while the count in its response
  came from a *scoped* SELECT, so the caller was told "0 updated";
- the Dialtone kill switch paused and resumed every org's enrollments from one
  org's switch, which on the resume side meant one tenant could silently
  *release* another tenant's safety control.

A statement is accepted when one of these holds:

1. it is wrapped in :func:`app.org_context.scope_write_to_current_org`, or the
   enclosing function writes an ``org_id`` equality on that model itself;
2. it pins a row by an equality filter on columns the schema promises unique —
   in practice ``Model.id == <one id>``, which callers reach by reading the row
   back through a scoped SELECT first;
3. it opts out explicitly with ``execution_options(skip_org_filter=True)``,
   which is deliberate and greppable;
4. it has a reasoned entry in :data:`REVIEWED`.

**What this does not catch**, stated plainly so the check is not read as more
than it is:

- an ``id ==`` whose id came from somewhere *other* than a scoped read. Rule 2
  trusts the convention; it cannot see where the value came from.
- writes to association tables (``company_group_members``), which are ``Table``
  objects with no ``org_id`` of their own. CLAUDE.md covers these: what scopes
  them is the owning row you looked up to reach them.
- raw ``text()`` SQL.
"""

import ast
import collections
import pathlib

import pytest

import app.models  # noqa: F401  — registers every mapped class
from app.database import Base
from app.models.mixins import OrgScopedMixin

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# Sites that satisfy none of the four rules, reviewed individually and judged
# safe. An entry without a reason is an unreviewed exception; don't add one.
REVIEWED: dict[str, str] = {}


def _org_scoped_models() -> dict[str, type]:
    return {
        m.class_.__name__: m.class_
        for m in Base.registry.mappers
        if issubclass(m.class_, OrgScopedMixin)
    }


def _unique_column_sets() -> dict[str, list[set[str]]]:
    """Per model, the column sets the schema promises are unique.

    ``org_id`` is subtracted from composite unique constraints: it is exactly
    the column a cross-tenant write fails to supply, so counting it as part of a
    uniqueness promise would let the shapes under test pass.
    """
    out = collections.defaultdict(list)
    for mapper in Base.registry.mappers:
        table = mapper.class_.__table__
        name = mapper.class_.__name__
        out[name].append({c.name for c in table.primary_key.columns})
        for constraint in table.constraints:
            cols = {c.name for c in getattr(constraint, "columns", [])}
            if cols and cols != {"org_id"}:
                out[name].append(cols - {"org_id"})
        for index in table.indexes:
            if index.unique:
                cols = {c.name for c in index.columns}
                if cols and cols != {"org_id"}:
                    out[name].append(cols - {"org_id"})
    return out


def _attr_to_column() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for mapper in Base.registry.mappers:
        out[mapper.class_.__name__] = {
            prop.key: prop.columns[0].name
            for prop in mapper.column_attrs
            if getattr(prop, "columns", None)
        }
    return out


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _whole_expression(node: ast.AST, parents: dict) -> ast.AST:
    """Climb to the outermost call/attribute chain containing ``node``.

    ``update(X)`` on its own says nothing; the ``.where(...)`` clauses that
    decide which rows it touches, and any ``scope_write_to_current_org(...)``
    wrapped around it, are ancestors of that call.
    """
    while isinstance(parents.get(node), (ast.Attribute, ast.Call)):
        node = parents[node]
    return node


def _enclosing_function(node: ast.AST, parents: dict) -> ast.AST | None:
    """The def containing ``node``, or None at module level."""
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return None


def _scopes_by_org_id(node: ast.AST, model: str, parents: dict) -> bool:
    """Whether the enclosing function establishes ``<model>.org_id ==`` itself.

    Predicates are routinely built a few lines above the statement that uses
    them — ``gmail.cleanup_orphaned_emails`` composes an ``and_(...)`` carrying
    ``EmailReply.org_id == current_user.org_id`` and hands it to both a count and
    a delete. Looking only at the delete expression reads that as unscoped.

    Widening to the whole function is deliberately generous: it can accept a
    function whose org predicate is on a *different* statement than the write.
    The alternative — flagging code that is scoped, in the idiom the codebase
    already uses — teaches people to silence the check, which costs more than
    the narrow miss.
    """
    func = _enclosing_function(node, parents)
    if func is None:
        return False
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Compare) or not sub.ops:
            continue
        if not isinstance(sub.ops[0], ast.Eq):
            continue
        target = sub.left
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "org_id"
            and isinstance(target.value, ast.Name)
            and target.value.id == model
        ):
            return True
    return False


def _equality_columns(expr: ast.AST, model: str, attr2col: dict) -> set[str]:
    """Columns of ``model`` compared with ``==`` anywhere in ``expr``.

    ``.in_(...)`` is deliberately not counted — a set of client-supplied ids is
    the shape that made ``bulk-update-stage`` a write IDOR.
    """
    found = set()
    for sub in ast.walk(expr):
        if not isinstance(sub, ast.Compare) or len(sub.ops) != 1:
            continue
        if not isinstance(sub.ops[0], ast.Eq):
            continue
        target = sub.left
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == model
        ):
            found.add(attr2col.get(model, {}).get(target.attr, target.attr))
    return found


def find_unscoped_writes(root: pathlib.Path | None = None):
    """Every bulk write on an org-scoped model that none of the four rules cover.

    ``root`` is a parameter so the self-tests below can point the scan at a
    planted file rather than monkeypatching a module global.
    """
    root = root or APP_ROOT
    models = _org_scoped_models()
    uniques = _unique_column_sets()
    attr2col = _attr_to_column()

    unscoped = []
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root.parent))
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - defensive
            continue
        parents = _parents(tree)

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("update", "delete")
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Name)
            ):
                continue
            model = node.args[0].id
            if model not in models:
                continue  # platform-wide table, or an association Table

            expr = _whole_expression(node, parents)
            segment = ast.get_source_segment(source, expr) or ""

            if "scope_write_to_current_org" in segment:
                continue
            if "skip_org_filter" in segment:
                continue
            if _scopes_by_org_id(node, model, parents):
                continue
            filtered = _equality_columns(expr, model, attr2col)
            if any(u and u <= filtered for u in uniques[model]):
                continue

            unscoped.append((rel, node.lineno, node.func.id, model, sorted(filtered)))
    return unscoped


def test_no_unscoped_bulk_writes():
    found = [s for s in find_unscoped_writes() if s[0] not in REVIEWED]
    assert not found, (
        "bulk update()/delete() on an org-scoped model with no org predicate:\n"
        + "\n".join(
            f"  {path}:{line} — {verb}({model}), filtered on {cols or 'nothing unique'}"
            for path, line, verb, model, cols in found
        )
        + "\n\nThe do_orm_execute filter in app/models/mixins.py only covers "
        "SELECTs, so this statement runs across every tenant on the platform. "
        "Wrap it in app.org_context.scope_write_to_current_org(stmt, Model), or "
        "pin the row by primary key after reading it through a scoped SELECT. "
        "Only add to REVIEWED with a reason."
    )


def test_the_reviewed_list_has_no_stale_entries():
    flagged = {path for path, *_ in find_unscoped_writes()}
    stale = set(REVIEWED) - flagged
    assert not stale, (
        f"REVIEWED names files the scan no longer flags: {sorted(stale)}. "
        "Drop them, so the list stays a list of real exceptions."
    )


def test_the_scan_finds_a_planted_violation(tmp_path):
    """The scan must actually be capable of failing.

    A checker that silently matches nothing passes forever and is worse than no
    checker, because it reads as coverage. This plants the exact shape the
    signature and template routers had.
    """
    planted = tmp_path / "app" / "services"
    planted.mkdir(parents=True)
    (planted / "planted.py").write_text(
        "from sqlalchemy import update\n"
        "from app.models.email_template import EmailTemplate\n"
        "async def go(db):\n"
        "    await db.execute(\n"
        "        update(EmailTemplate)\n"
        "        .where(EmailTemplate.is_default == True)\n"
        "        .values(is_default=False)\n"
        "    )\n"
    )
    found = find_unscoped_writes(tmp_path / "app")

    assert any("planted.py" in path for path, *_ in found), (
        f"the scan did not flag an unmistakable violation — it is matching "
        f"nothing. Found: {found}"
    )


@pytest.mark.parametrize(
    "snippet,should_flag",
    [
        # Safe: pinned to one row by primary key.
        ("update(Company).where(Company.id == cid).values(name='x')", False),
        # Safe: explicitly scoped.
        (
            "scope_write_to_current_org("
            "update(Company).where(Company.source == s), Company"
            ").values(name='x')",
            False,
        ),
        # Safe: a deliberate, greppable opt-out.
        (
            "update(Company).where(Company.source == s).values(name='x')"
            ".execution_options(skip_org_filter=True)",
            False,
        ),
        # Unsafe: a broad predicate on a non-unique column.
        ("update(Company).where(Company.source == s).values(name='x')", True),
        # Unsafe: client-supplied ids. An `in_` is not an equality, and this is
        # exactly what made bulk-update-stage a cross-tenant write.
        ("update(Company).where(Company.id.in_(ids)).values(name='x')", True),
        # Unsafe: no predicate at all.
        ("update(Company).values(name='x')", True),
    ],
    ids=["primary-key", "wrapped", "explicit-opt-out", "broad", "id-in", "no-where"],
)
def test_the_scan_classifies_the_shapes(tmp_path, snippet, should_flag):
    """Both directions, so the checker is neither blind nor crying wolf.

    The ``id-in`` case is the one worth keeping honest: it filters on the
    primary key and is still a cross-tenant write, so a checker that merely
    looked for "is the PK mentioned?" would wave it through.
    """
    planted = tmp_path / "app" / "services"
    planted.mkdir(parents=True)
    (planted / "sample.py").write_text(
        "from sqlalchemy import update\n"
        "from app.models.company import Company\n"
        "from app.org_context import scope_write_to_current_org\n"
        "async def go(db, cid, ids, s):\n"
        f"    await db.execute({snippet})\n"
    )
    flagged = any(
        "sample.py" in path for path, *_ in find_unscoped_writes(tmp_path / "app")
    )
    assert flagged is should_flag


def test_the_scan_ignores_models_that_are_not_org_scoped():
    """``Organization`` and ``User`` are deliberately outside the mixin.

    Flagging them would push a reviewer toward adding an ``org_id`` predicate to
    a table that has none, or toward silencing the check.
    """
    models = _org_scoped_models()
    assert "Organization" not in models
    assert "User" not in models
    assert "Company" in models, "the scan is looking at an empty model registry"
