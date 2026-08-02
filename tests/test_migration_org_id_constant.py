"""Every migration's inlined Apprend org id must be the real one.

Migrations inline ``APPREND_ORG_ID`` rather than importing
:data:`app.org_context.APPREND_ORG_ID`, and that is the right call: a migration
describes the schema at its own revision and must not change meaning when
application code is refactored years later. The cost is that the literal is
copied ~12 times with nothing comparing the copies.

Two of them were wrong — ``00000000-0000-0000-0000-000000000001``, which looks
like exactly the kind of id a first-party tenant would have and matches no row
in ``organizations``:

* **111** backfilled ``company_groups.org_id`` with it and then added a foreign
  key to ``organizations``. On any deployment that had ever created a group,
  that migration aborts — the fix would have been found the hard way, mid-deploy,
  with the schema half-applied.
* **081** used it as the ``server_default`` on two new tables that also carry
  that foreign key. Those tables were empty, so nothing failed at migration
  time; what it leaves is a default that raises a FK violation for any insert
  that omits ``org_id``. ORM inserts are stamped by the ``before_flush`` hook in
  :mod:`app.models.mixins`, so it stayed latent — the failure is reserved for a
  raw insert, a backfill script, or a ``COPY``. Migration 112 corrects it.

Comparing the literals is cheap and pins all twelve at once.
"""

import pathlib
import re

import pytest

from app.org_context import APPREND_ORG_ID

VERSIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "alembic" / "versions"

# Matches `APPREND_ORG_ID = "..."` and the `_APPREND_ORG_ID` spelling.
_ASSIGNMENT = re.compile(
    r"^_?APPREND_ORG_ID\s*(?::\s*[^=]+)?=\s*[\"']([0-9a-fA-F-]{36})[\"']",
    re.MULTILINE,
)


def _migrations_declaring_the_org_id():
    for path in sorted(VERSIONS_DIR.glob("*.py")):
        for literal in _ASSIGNMENT.findall(path.read_text()):
            yield path.name, literal


def test_the_scan_finds_the_migrations_that_have_one():
    """Guard the regex itself — a pattern that matches nothing passes vacuously."""
    found = list(_migrations_declaring_the_org_id())
    assert len(found) >= 10, (
        f"only found {len(found)} inlined org ids; the assignment pattern has "
        "probably stopped matching, which would make the check below vacuous"
    )


@pytest.mark.parametrize(
    "migration,literal", list(_migrations_declaring_the_org_id())
)
def test_every_inlined_org_id_is_the_real_one(migration, literal):
    assert literal.lower() == str(APPREND_ORG_ID).lower(), (
        f"{migration} inlines org id {literal!r}, but the Apprend organization "
        f"row created by migration 078 is {APPREND_ORG_ID}. A literal naming no "
        "row in `organizations` either aborts the migration (if it is followed "
        "by the FK) or installs a server_default that fails every insert which "
        "omits org_id."
    )


def test_migration_078_is_the_source_of_truth():
    """078 is the migration that INSERTs the row; it defines the value."""
    text = (VERSIONS_DIR / "078_multi_tenancy.py").read_text()
    literals = _ASSIGNMENT.findall(text)
    assert literals, "078 no longer declares APPREND_ORG_ID — this check is stale"
    assert literals[0].lower() == str(APPREND_ORG_ID).lower()
