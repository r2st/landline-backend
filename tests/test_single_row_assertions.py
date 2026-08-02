"""``scalar_one_or_none`` may only be used where the schema promises one row.

CLAUDE.md records this bug class costing four times now, in unrelated code, the
same way each time: the query reads as the careful option and is in fact an
assertion that raises ``MultipleResultsFound`` — a 500 — the moment a second row
exists.

- a duplicate contact broke **every** unsubscribe link;
- a duplicate group name broke **all three** ``/groups/auto-group-by-*``
  endpoints;
- a second connected mailbox broke welcome mail and notifications outright, and
  outreach's fallback sender;
- a company name entered twice broke the business-card scanner's quick-add.

Reviewing for it does not work, because there is nothing to see: the unsafe call
is character-for-character the safe one, and what separates them lives in a
different file — the model's constraints. So this checks it mechanically. Every
``scalar_one_or_none`` in ``app/`` must be covered by one of:

1. a ``.limit(1)`` on the statement, which makes the assertion unfalsifiable;
2. an equality filter on a set of columns carrying a PK or unique constraint —
   the schema genuinely promising at most one row;
3. an entry in :data:`REVIEWED`, with a reason.

The org-scoping check in ``test_org_scoping_coverage.py`` has the same shape and
exists for the same reason: a property that is invisible at the call site needs
a test that looks at all of them at once.
"""

import ast
import collections
import pathlib
import re

import pytest

import app.models  # noqa: F401  — registers every mapped class
from app.database import Base
from app.models.mixins import OrgScopedMixin

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / "app"

# Sites that are none of the three, reviewed individually and judged safe.
# An entry without a reason is an unreviewed exception; don't add one.
REVIEWED: dict[str, str] = {
    "app/services/opt_out.py": (
        "the scalar_one_or_none here is on a different statement from the "
        "contact lookup the scan attributes it to — the contact query above it "
        "was already fixed to read every duplicate row (see the unsubscribe "
        "notes in CLAUDE.md) and the 14-line lookback runs the two together"
    ),
}


def _unique_column_sets() -> dict[str, list[set[str]]]:
    """Per model, the column sets the schema promises are unique."""
    out = collections.defaultdict(list)
    for mapper in Base.registry.mappers:
        table = mapper.class_.__table__
        name = mapper.class_.__name__
        out[name].append({c.name for c in table.primary_key})
        out[name].extend({c.name} for c in table.columns if c.unique)
        out[name].extend(
            {c.name for c in con.columns}
            for con in table.constraints
            if type(con).__name__ == "UniqueConstraint"
        )
        out[name].extend(
            {c.name for c in ix.columns} for ix in table.indexes if ix.unique
        )
    return out


def _attr_to_column() -> dict[str, dict[str, str]]:
    return {
        m.class_.__name__: {p.key: p.columns[0].name for p in m.column_attrs}
        for m in Base.registry.mappers
    }


def _model_classes() -> dict[str, type]:
    return {m.class_.__name__: m.class_ for m in Base.registry.mappers}


EQUALITY = re.compile(r"(\w+)\.(\w+)\s*==")
SELECT = re.compile(r"select\(\s*(\w+)")
LIMIT_ONE = re.compile(r"\.limit\(\s*1\s*\)")


def find_unsafe_sites(root: pathlib.Path | None = None) -> list[tuple[str, int, str, list[str]]]:
    """Every ``scalar_one_or_none`` not covered by limit(1) or a constraint.

    ``root`` is a parameter so the self-tests below can point the scan at a
    planted file instead of monkeypatching a module global.
    """
    root = root or APP_ROOT
    uniques = _unique_column_sets()
    attr2col = _attr_to_column()
    classes = _model_classes()

    unsafe = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent)
        lines = path.read_text().splitlines()
        for index, line in enumerate(lines):
            if "scalar_one_or_none" not in line:
                continue
            # The statement being consumed is normally within a dozen lines.
            block = "\n".join(lines[max(0, index - 14): index + 1])
            if LIMIT_ONE.search(block):
                continue
            selected = SELECT.findall(block)
            if not selected:
                continue
            model = selected[-1]
            if model not in uniques:
                continue  # not a mapped class (a column, a subquery, a CTE)

            filtered = {
                attr2col[model].get(attr, attr)
                for owner, attr in EQUALITY.findall(block)
                if owner == model
            }
            if issubclass(classes[model], OrgScopedMixin):
                # The global read filter adds org_id to every statement.
                filtered.add("org_id")
            if any(u and u <= filtered for u in uniques[model]):
                continue
            unsafe.append((str(rel), index + 1, model, sorted(filtered)))
    return unsafe


def test_no_unreviewed_single_row_assertions():
    unsafe = [s for s in find_unsafe_sites() if s[0] not in REVIEWED]

    detail = "\n".join(
        f"  {path}:{line} — select({model}) filtered on {cols or ['nothing']}"
        for path, line, model, cols in unsafe
    )
    assert not unsafe, (
        "these `scalar_one_or_none` calls assert a uniqueness the schema does "
        f"not promise, so a second matching row is a 500:\n{detail}\n\n"
        "Fix by ordering on something stable and taking "
        "`.order_by(...).limit(1)` + `.scalars().first()` — order matters, or "
        "repeat runs pick different rows and work scatters between the "
        "duplicates. For a pure existence check, `select(Model.id)...limit(1)` "
        "is both correct and cheaper. Only add to REVIEWED with a reason."
    )


def test_the_reviewed_list_has_no_stale_entries():
    """An exemption for a site that is now safe is a stale claim about the code."""
    flagged = {path for path, *_ in find_unsafe_sites()}
    stale = set(REVIEWED) - flagged
    assert not stale, (
        f"REVIEWED names files the scan no longer flags: {sorted(stale)}. "
        "Remove them so the list keeps meaning something."
    )


def test_the_scan_finds_a_planted_violation(tmp_path):
    """The scan must actually be capable of failing.

    A checker that silently matches nothing passes forever and is worse than no
    checker, because it reads as coverage. ``Contact`` has no unique constraint
    at all, which is what made the unsubscribe bug possible, so a bare lookup on
    it is a genuine violation of the rule under test.
    """
    planted = tmp_path / "app" / "services"
    planted.mkdir(parents=True)
    (planted / "planted.py").write_text(
        "from sqlalchemy import select\n"
        "from app.models.contact import Contact\n"
        "async def go(db, email):\n"
        "    return (\n"
        "        await db.execute(select(Contact).where(Contact.email == email))\n"
        "    ).scalar_one_or_none()\n"
    )
    found = find_unsafe_sites(tmp_path / "app")

    assert any("planted.py" in path for path, *_ in found), (
        "the scan did not flag an unmistakable violation — it is matching "
        f"nothing. Found: {found}"
    )


@pytest.mark.parametrize(
    "snippet,should_flag",
    [
        # Safe: filtered on the primary key.
        ("select(Contact).where(Contact.id == cid)", False),
        # Safe: an explicit single-row cap.
        ("select(Contact).where(Contact.email == e).limit(1)", False),
        # Unsafe: contacts has no unique constraint on email — or on anything.
        ("select(Contact).where(Contact.email == e)", True),
    ],
    ids=["primary-key", "limit-1", "unconstrained-column"],
)
def test_the_scan_classifies_the_three_shapes(tmp_path, snippet, should_flag):
    """Both directions, so the checker is neither blind nor crying wolf."""
    planted = tmp_path / "app" / "services"
    planted.mkdir(parents=True)
    (planted / "sample.py").write_text(
        "from sqlalchemy import select\n"
        "from app.models.contact import Contact\n"
        "async def go(db, cid, e):\n"
        f"    return (await db.execute({snippet})).scalar_one_or_none()\n"
    )
    flagged = any(
        "sample.py" in path for path, *_ in find_unsafe_sites(tmp_path / "app")
    )
    assert flagged is should_flag


def test_every_app_module_still_parses():
    """Cheap guard on the edits this rule prompts — they are all hand-applied."""
    for path in APP_ROOT.rglob("*.py"):
        try:
            ast.parse(path.read_text())
        except SyntaxError as exc:  # pragma: no cover - only on a broken edit
            pytest.fail(f"{path} does not parse: {exc}")
