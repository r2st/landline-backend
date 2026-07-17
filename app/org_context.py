"""Per-request organization context for multi-tenant isolation.

A single :class:`~contextvars.ContextVar` carries the *current* organization id
for the duration of a request (or a background task). It is set by the auth
dependency (see :mod:`app.dependencies`) from the caller's JWT ``org_id`` claim
and read in two places:

* **Writes** — :class:`app.models.mixins.OrgScopedMixin` uses it as the default
  value for the ``org_id`` column, so every ORM insert is stamped with the
  caller's org automatically (no need to thread ``org_id`` through every
  service).
* **Reads** — a ``do_orm_execute`` event (registered in
  :mod:`app.models.mixins`) injects ``org_id == current_org`` into every SELECT
  that touches an org-scoped model, so isolation is enforced globally.

When no org context is set (background jobs, the login path, platform admins),
the read filter is skipped and writes fall back to the first-party
"Apprend Technologies" organization — see :data:`APPREND_ORG_ID`.
"""

import logging
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from typing import Optional

# Fixed id of the first-party "Apprend Technologies" organization. Migration 078
# inserts the organizations row with exactly this id and backfills every
# pre-existing row to it, so the application-layer default below and the
# database rows always agree. Deterministic (not random) so tests and backfills
# can reference it directly.
APPREND_ORG_ID: uuid.UUID = uuid.UUID("a9910000-0000-4000-8000-000000000001")
APPREND_ORG_SLUG = "apprend-technologies"

# ``None`` means "no org context" → read filter is skipped, writes fall back to
# APPREND_ORG_ID. A concrete UUID scopes reads/writes to that organization.
_current_org_id: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "current_org_id", default=None
)


def get_current_org_id() -> Optional[uuid.UUID]:
    """Return the org id bound to the current context, or ``None``."""
    return _current_org_id.get()


def set_current_org_id(org_id: Optional[uuid.UUID]) -> Token:
    """Bind *org_id* to the current context. Returns a token for :func:`reset`."""
    return _current_org_id.set(org_id)


def reset_current_org_id(token: Token) -> None:
    """Restore the previous org context (pair with :func:`set_current_org_id`)."""
    _current_org_id.reset(token)


@asynccontextmanager
async def org_scope(org_id: Optional[uuid.UUID]):
    """Bind *org_id* as the current org for the duration of an ``async with``.

    Pair-safe: always restores the previous context on exit. ``None`` binds the
    "no org" context (unfiltered / platform-wide). Used by request handlers
    (e.g. the public tracking pixel, which must attribute an event to the
    enrollment's org) and by background tasks alike.
    """
    token = _current_org_id.set(org_id)
    try:
        yield
    finally:
        _current_org_id.reset(token)


class OrgContextLogFilter(logging.Filter):
    """Stamp every log record with the current org id for tenant-aware logs.

    Attach to the root handler so every line — request handler or background
    task — carries ``org=<id>`` (or ``org=-`` when unscoped). Because it always
    sets the attribute, a format string referencing ``%(org_id)s`` never
    KeyErrors on third-party records routed through the same handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        org = _current_org_id.get()
        record.org_id = str(org) if org is not None else "-"
        return True


def org_id_for_insert() -> uuid.UUID:
    """Resolve the org id to stamp on a new row.

    The current context's org if one is bound, else the first-party Apprend
    organization. Always returns a concrete UUID so the NOT NULL ``org_id``
    column is never left null by an ORM insert.
    """
    return _current_org_id.get() or APPREND_ORG_ID
