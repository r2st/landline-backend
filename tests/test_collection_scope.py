"""Pytest must not collect application code as tests.

`app/routers/email_test.py` defines `test_send` — a FastAPI route handler for
the "Send Test Email" endpoint, correctly named for what it does. Its filename
matches pytest's default `*_test.py` pattern and the function matches `test_*`,
so a bare `pytest` at the repo root collected the endpoint as a test case and
errored trying to resolve `payload` and `db` as fixtures: 3625 passing tests and
a red exit code, from application code that was never a test.

The fix is `testpaths = tests` in pytest.ini rather than renaming the endpoint,
which would change its generated OpenAPI operationId to satisfy a test runner.
That makes the two halves below load-bearing together: application modules are
*allowed* to look like test modules precisely because collection never reaches
them. Drop `testpaths` and the first test fails; drop it while `test_send` still
exists and the second one explains what broke.

Static by design, like `test_migration_chain.py` — no subprocess, no collecting
3600 tests twice to check how they were collected.
"""

import ast
import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# pytest's default `python_files`. The repo does not override it, and both
# patterns matter: the module that caused this is a `*_test.py`.
PYTHON_FILE_PATTERNS = ("test_*.py", "*_test.py")

# Package roots that hold shipping code and must never be collected from.
APPLICATION_ROOTS = ("app", "alembic")


def _collection_roots() -> list[Path]:
    """The directories a bare `pytest` would collect from.

    With no `testpaths`, pytest collects from the rootdir — so an empty setting
    means *everything*, not *nothing*. Returning the repo root in that case is
    what keeps the checks below from passing vacuously the moment the setting
    they exist to protect is deleted.
    """
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    entries = parser.get("pytest", "testpaths", fallback="").split()
    if not entries:
        return [REPO_ROOT]
    return [(REPO_ROOT / entry).resolve() for entry in entries]


def _defines_a_test_function(path: Path) -> bool:
    """True if pytest would find a `test_*` function at module level."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
        for node in tree.body
    )


def _application_modules_matching_pytest_patterns() -> list[Path]:
    found: list[Path] = []
    for root in APPLICATION_ROOTS:
        for pattern in PYTHON_FILE_PATTERNS:
            found.extend((REPO_ROOT / root).rglob(pattern))
    return sorted(found)


def test_collection_is_scoped_to_the_tests_directory():
    """A bare `pytest` must not walk the repo root into application packages."""
    for root in _collection_roots():
        assert root.is_dir(), f"collection root {root} is not a directory"
        assert root != REPO_ROOT, (
            "pytest.ini sets no testpaths, so a bare `pytest` collects from the "
            "repo root and imports application modules named like tests."
        )
        for app_root in APPLICATION_ROOTS:
            app_dir = (REPO_ROOT / app_root).resolve()
            assert not root.is_relative_to(app_dir), (
                f"collection root {root} reaches into {app_root}/"
            )


def test_application_modules_named_like_tests_are_out_of_collection_scope():
    """App code may look like a test module — as long as pytest never sees it.

    This is the half that actually caught the bug. It fails if application code
    that pytest would collect is reachable from a configured testpath.
    """
    roots = _collection_roots()

    for module in _application_modules_matching_pytest_patterns():
        if not _defines_a_test_function(module):
            continue
        for root in roots:
            assert not module.resolve().is_relative_to(root), (
                f"{module.relative_to(REPO_ROOT)} defines a test-looking function "
                f"and sits inside collected path {root}; pytest will collect "
                "application code as a test."
            )


def test_the_real_tests_are_all_inside_a_configured_testpath():
    """Scoping collection must not have hidden any of the suite."""
    roots = _collection_roots()

    orphans = [
        module.relative_to(REPO_ROOT)
        for pattern in PYTHON_FILE_PATTERNS
        for module in (REPO_ROOT / "tests").rglob(pattern)
        if not any(module.resolve().is_relative_to(r) for r in roots)
    ]

    assert not orphans, f"test modules excluded from collection: {orphans}"
