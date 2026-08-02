"""The migration chain must be able to build a database from nothing.

Prod applies migrations incrementally, so a chain that only works forward from
the state prod happens to be in looks perfectly healthy — right up to the moment
you need a new environment or a restore. Both breaks below were live on `main`
and invisible for exactly that reason, and both were found by pointing
`alembic upgrade head` at an empty Postgres rather than by reading the files.

These are static checks so they run in the normal SQLite suite. They do not
replace actually running the chain — see the module note at the bottom — they
stop the two specific classes of break that already happened from coming back.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

ALEMBIC = Path(__file__).resolve().parent.parent / "alembic"
VERSIONS = ALEMBIC / "versions"


def _version_num_width() -> int:
    """Read the width ``env.py`` provisions, without importing it.

    ``alembic/env.py`` runs migrations as a side effect of import — it is a
    script Alembic execs, not a module — so this reads the constant out of the
    source rather than importing it.
    """
    text = (ALEMBIC / "env.py").read_text()
    match = re.search(r"^_VERSION_NUM_WIDTH\s*=\s*(\d+)", text, re.M)
    assert match, "alembic/env.py no longer declares _VERSION_NUM_WIDTH"
    return int(match.group(1))

_REVISION_RE = re.compile(r"^revision(?::\s*str)?\s*=\s*[\"']([^\"']+)", re.M)
_CREATE_INDEX_RE = re.compile(r"op\.create_index\(\s*[\"']([^\"']+)[\"']")
_CREATE_TABLE_RE = re.compile(r"op\.create_table\(\s*[\"']([^\"']+)[\"']")


def _revision_files():
    return sorted(p for p in VERSIONS.glob("*.py") if p.name != "__init__.py")


def _revision_id(text: str, path: Path) -> str:
    match = _REVISION_RE.search(text)
    return match.group(1) if match else path.stem


def test_every_revision_id_fits_the_version_table():
    """A too-long id writes 36 revisions of schema, then dies stamping one.

    ``037_add_user_id_to_email_accounts`` is 33 characters against Alembic's
    default 32, so a fresh build failed *after* doing most of the work — the
    worst shape of failure, because the database is left half-migrated.
    """
    width = _version_num_width()
    too_long = {
        _revision_id(p.read_text(), p)
        for p in _revision_files()
        if len(_revision_id(p.read_text(), p)) > width
    }
    assert not too_long, (
        f"revision ids longer than the {width}-char version column "
        f"env.py provisions: {sorted(too_long)}"
    )


@pytest.mark.parametrize(
    "pattern,kind",
    [(_CREATE_INDEX_RE, "index"), (_CREATE_TABLE_RE, "table")],
)
def test_no_object_is_created_by_two_revisions(pattern, kind):
    """Two revisions claiming one object is a from-scratch failure.

    ``005_add_indexes`` and ``015_extend_interactions`` both created
    ``ix_interactions_company_id``. Applied incrementally that is fine — 005 had
    already run long before 015 was written — so it never showed up until
    somebody tried to build the schema from revision 001.
    """
    creators: dict[str, list[str]] = defaultdict(list)
    for path in _revision_files():
        text = path.read_text()
        for name in set(pattern.findall(text)):
            creators[name].append(path.name)

    collisions = {n: sorted(f) for n, f in creators.items() if len(f) > 1}
    assert not collisions, (
        f"{kind}(s) created by more than one revision — a fresh "
        f"'alembic upgrade head' will fail on the second: {collisions}"
    )


def test_the_chain_is_linear_from_one_root_to_one_head():
    """A second head means 'upgrade head' is ambiguous and may not run at all.

    Concurrent sessions land migrations on main, which is how this repo grows
    stray branches; catching it here is cheaper than catching it on the box.
    """
    # ``down_revision`` is a string, None, or — for a merge revision — a tuple
    # of every branch it joins. This repo has four merges, from concurrent
    # sessions landing migrations on main; a parser that only reads the string
    # form reports each merged branch as a live head.
    down_re = re.compile(
        r"^down_revision(?::\s*[^=]+)?\s*=\s*(\(.*?\)|[\"'][^\"']+[\"']|None)",
        re.M | re.S,
    )
    revisions: dict[str, str] = {}
    downs: dict[str, tuple[str, ...]] = {}
    for path in _revision_files():
        text = path.read_text()
        rev = _revision_id(text, path)
        revisions[rev] = path.name
        match = down_re.search(text)
        if match and match.group(1) != "None":
            downs[rev] = tuple(re.findall(r"[\"']([^\"']+)[\"']", match.group(1)))

    parents = {p for group in downs.values() for p in group}
    heads = sorted(r for r in revisions if r not in parents)
    roots = sorted(r for r in revisions if r not in downs)
    assert len(heads) == 1, (
        f"expected one head, found {heads} — 'alembic upgrade head' is "
        "ambiguous with more than one; add a merge revision"
    )
    assert len(roots) == 1, f"expected one root, found {roots}"

    # And every down_revision must name a revision that exists.
    dangling = {
        r: [p for p in group if p not in revisions]
        for r, group in downs.items()
        if any(p not in revisions for p in group)
    }
    assert not dangling, f"down_revision points at a missing revision: {dangling}"
