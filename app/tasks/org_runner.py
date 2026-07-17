"""Helpers that make Celery/background tasks multi-tenant aware.

Background tasks run OUTSIDE the HTTP request lifecycle, so nothing sets the
per-request organization context (see :mod:`app.org_context`). With no context
bound, the global ``do_orm_execute`` read filter (see
:mod:`app.models.mixins`) is a *no-op* — a beat task would query every
organization's rows at once and inserts would be misattributed to the
first-party Apprend org. That is a cross-tenant data leak.

Two primitives fix this:

* :func:`for_each_org` — the workhorse for beat/batch tasks that used to sweep
  "all rows". It runs the task body once per active organization, each in its
  own session with that org's context bound, isolating failures so one bad org
  can't abort the rest.
* :func:`org_scope` — an async context manager for the single-entity tasks
  (``send_sequence_step(enrollment_id)`` etc.) that are dispatched per-record:
  after loading the record (unfiltered, since no context is bound yet) the task
  binds the record's ``org_id`` so everything downstream is correctly scoped.

Both deliberately keep the "read the organization list" query unfiltered — the
``organizations`` table is the tenant boundary itself and is not org-scoped.
"""

import logging
from typing import Awaitable, Callable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database_celery import celery_session
from app.models.tenant import Organization
from app.org_context import (  # noqa: F401 — org_scope re-exported for tasks
    org_scope,
    reset_current_org_id,
    set_current_org_id,
)

logger = logging.getLogger(__name__)


async def active_org_ids(db: AsyncSession) -> list[UUID]:
    """Return the id of every *active* organization.

    ``Organization`` is not org-scoped, so this select is never touched by the
    global read filter — it always sees every tenant, which is exactly what a
    platform-wide beat task needs before it fans out per org.
    """
    rows = (
        await db.execute(
            select(Organization.id).where(Organization.is_active.is_(True))
        )
    ).scalars().all()
    return list(rows)


async def for_each_org(
    body: Callable[[AsyncSession], Awaitable],
    *,
    label: Optional[str] = None,
) -> list:
    """Run ``await body(db)`` once per active org, scoped + isolated.

    Each organization gets a *fresh* :func:`~app.database_celery.celery_session`
    with its ``org_id`` bound as the current context, so every ORM read/write in
    ``body`` is automatically filtered/stamped for that org. A failure in one
    org is logged and skipped — it never aborts the sweep for the others.

    Returns the list of per-org return values (skipping orgs that raised), so a
    caller can still summarise the run.
    """
    async with celery_session() as db:
        org_ids = await active_org_ids(db)

    results: list = []
    for org_id in org_ids:
        token = set_current_org_id(org_id)
        try:
            async with celery_session() as db:
                results.append(await body(db))
        except Exception:  # never let one org abort the whole sweep
            logger.exception(
                "for_each_org(%s): org=%s failed", label or "task", org_id
            )
        finally:
            reset_current_org_id(token)
    return results


async def org_id_for_record(
    db: AsyncSession, model, pk: UUID
) -> Optional[UUID]:
    """Look up a single record's ``org_id`` *without* the org filter applied.

    Used by per-record tasks to discover which tenant a record belongs to
    before binding its context. Returns ``None`` if the record doesn't exist or
    the model isn't org-scoped.
    """
    if not hasattr(model, "org_id"):
        return None
    return (
        await db.execute(
            select(model.org_id)
            .where(model.id == pk)
            .execution_options(skip_org_filter=True)
        )
    ).scalar_one_or_none()
