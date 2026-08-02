"""No tenant-owned table gets to sit outside the isolation machinery.

``company_groups`` did, for two years. Nothing caught it because there was
nothing to catch it *with*: isolation is opt-in (mix in
:class:`~app.models.mixins.OrgScopedMixin`), and a model that never opted in
looks, from every angle the test suite had, exactly like a model that does not
need to. Alembic autogenerate diffs the models against the schema, so it agreed;
the multi-tenancy tests each pick a model and assert isolation *for that model*,
so they had no opinion about the ones nobody had thought of.

This is the check that has an opinion about all of them at once. It walks the
mapper registry — so a new model is in scope the moment it is imported, with no
list to remember to update — and requires every mapped class to either be
org-scoped or be named below with a reason.

If this fails for a model you just added: the fix is almost always the mixin
plus a migration that adds ``org_id`` (see ``111_company_groups_org_id``), not
an entry in the allowlist. Add one only for a table that genuinely belongs to
the platform rather than to a customer, and say why.
"""

import pathlib

import pytest
from sqlalchemy import inspect as sa_inspect

from app.database import Base
from app.models.mixins import OrgScopedMixin


# Mapped classes that are deliberately NOT org-scoped, and why. The reason is
# part of the test: an entry without one is an unreviewed exception.
GLOBAL_MODELS = {
    "Organization": "the tenant itself — scoping it to a tenant is circular",
    "TenantUsage": "already keyed by tenant_id, its own scoping column",
    "User": (
        "membership is the org_id column itself; login and the platform-admin "
        "cross-org views read users before any org context is bound, and the "
        "filter would make both invisible to themselves"
    ),
}


def _import_every_model_module() -> None:
    """Import each module in ``app.models``, not just what the package re-exports.

    Importing the package would be the obvious thing and is exactly what let the
    original bug through: ``company_group`` was not in
    ``app/models/__init__.py``, so its class was never registered, so a check
    that walked the registry would have reported full coverage of a schema it
    could not see. Walking the directory is the version that cannot be fooled by
    a missing import — see :func:`test_models_package_registers_every_model`,
    which pins the two views agreeing.
    """
    import importlib
    import pkgutil

    import app.models

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"app.models.{module.name}")


def _mapped_classes():
    """Every mapped class, from every module in the package."""
    _import_every_model_module()
    return sorted(
        (m.class_ for m in Base.registry.mappers),
        key=lambda c: c.__name__,
    )


def test_every_model_is_org_scoped_or_explicitly_exempt():
    unscoped = {
        cls.__name__
        for cls in _mapped_classes()
        if not issubclass(cls, OrgScopedMixin)
    }
    unexplained = unscoped - set(GLOBAL_MODELS)
    assert not unexplained, (
        "these models are not org-scoped and have no recorded reason: "
        f"{sorted(unexplained)}. Mix in OrgScopedMixin and add a migration, or "
        "add an entry to GLOBAL_MODELS explaining why the table is platform-wide."
    )


def test_the_allowlist_has_no_stale_entries():
    """An exemption that no longer applies is a stale claim about the schema."""
    scoped = {
        cls.__name__
        for cls in _mapped_classes()
        if issubclass(cls, OrgScopedMixin)
    }
    stale = scoped & set(GLOBAL_MODELS)
    assert not stale, f"GLOBAL_MODELS lists org-scoped models: {sorted(stale)}"


@pytest.mark.parametrize("attr", ["nullable", "index"])
def test_org_id_columns_are_uniform(attr):
    """``org_id`` must be NOT NULL and indexed everywhere.

    A nullable one fails open — the filter compares ``org_id == <org>`` and a
    NULL row matches nobody, so it silently disappears from its owner's reads
    instead of raising. An unindexed one is a sequential scan on every query in
    the application, since the filter is on every query in the application.
    """
    expected = {"nullable": False, "index": True}[attr]
    wrong = {
        cls.__name__
        for cls in _mapped_classes()
        if issubclass(cls, OrgScopedMixin)
        and getattr(sa_inspect(cls).columns["org_id"], attr) is not expected
    }
    assert not wrong, f"org_id has the wrong {attr} on: {sorted(wrong)}"


# Run in a *fresh interpreter*. ``Base.registry`` is process-global and only ever
# grows, so by the time any assertion runs in this process, conftest and the
# tests above have already imported every model module — making "what does
# `import app.models` register?" unanswerable from in here. Asked in-process, the
# check passes with the ``company_group`` import deleted again, i.e. it passes
# against the exact bug it exists to catch.
_REGISTRY_PROBE = """
import json, sys
import app.models
from app.database import Base

via_package = sorted(m.class_.__name__ for m in Base.registry.mappers)

import importlib, pkgutil
for module in pkgutil.iter_modules(app.models.__path__):
    importlib.import_module("app.models." + module.name)

via_filesystem = sorted(m.class_.__name__ for m in Base.registry.mappers)
print(json.dumps({
    "via_package": via_package,
    "via_filesystem": via_filesystem,
    "exported": sorted(app.models.__all__),
}))
"""


def test_models_package_registers_every_model():
    """``import app.models`` must be enough to see the whole schema.

    ``alembic/env.py`` builds ``target_metadata`` from this import, and
    autogenerate diffs the database against it — so a class the package does not
    register is a table autogenerate believes does not exist, and will happily
    propose dropping while never proposing a change to. That is precisely how
    ``company_groups`` missed migration 078's ``org_id`` sweep.
    """
    import json
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _REGISTRY_PROBE],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parent.parent),
        timeout=180,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    data = json.loads(proc.stdout.strip().splitlines()[-1])

    missing = set(data["via_filesystem"]) - set(data["via_package"])
    assert not missing, (
        f"these models are not registered by `import app.models`: {sorted(missing)}. "
        "Add the import to app/models/__init__.py — Alembic's target_metadata "
        "is built from that package, and a model missing from it is invisible to "
        "every autogenerated migration."
    )

    unexported = set(data["via_filesystem"]) - set(data["exported"])
    assert not unexported, f"mapped but absent from __all__: {sorted(unexported)}"


def test_company_group_is_scoped():
    """The regression this file was written for, named outright."""
    from app.models.company_group import CompanyGroup

    assert issubclass(CompanyGroup, OrgScopedMixin)
