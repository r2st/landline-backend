"""Shared declarative mixins.

:class:`OrgScopedMixin` gives a model an ``org_id`` foreign key to
``organizations.id`` and wires it into the multi-tenant isolation machinery:

* the column defaults to the current request's org (see
  :func:`app.org_context.org_id_for_insert`), so inserts are stamped
  automatically;
* a session-wide ``do_orm_execute`` event injects an ``org_id == <current org>``
  filter into every SELECT that touches an org-scoped model, so reads are
  isolated globally without every query having to remember to filter.

Every tenant-owned table mixes this in. The ``organizations`` table itself and
``tenant_usage`` (already scoped by ``tenant_id``) do not.
"""

import uuid

from sqlalchemy import ForeignKey, event, inspect, literal
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import (
    Mapped,
    Session,
    declared_attr,
    mapped_column,
    relationship,
    with_loader_criteria,
)

from app.org_context import APPREND_ORG_ID, get_current_org_id, org_id_for_insert


class OrgScopedMixin:
    """Adds a NOT NULL ``org_id`` FK + relationship to :class:`Organization`."""

    @declared_attr
    def org_id(cls) -> Mapped[uuid.UUID]:
        return mapped_column(
            UUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
            # DB-level safety net for non-ORM / raw inserts. ORM inserts are
            # stamped by the before_flush hook below (which, unlike a column
            # ``default``, can tell an *unset* org_id from an explicit None).
            server_default=str(APPREND_ORG_ID),
        )

    @declared_attr
    def organization(cls):
        return relationship("Organization", lazy="raise", viewonly=True)


@event.listens_for(Session, "before_flush")
def _stamp_org_id(session, flush_context, instances) -> None:
    """Populate ``org_id`` on new org-scoped rows that didn't set one.

    Runs only for objects where ``org_id`` was never assigned (``Company(...)``
    with no org). An *explicit* value — including ``None`` passed by a router
    that scopes to ``current_user.org_id`` — is left untouched, so a column-level
    ``default`` (which can't distinguish unset from explicit None) is avoided.
    """
    for obj in session.new:
        if not isinstance(obj, OrgScopedMixin):
            continue
        if "org_id" not in inspect(obj).dict:
            obj.org_id = org_id_for_insert()


@event.listens_for(Session, "do_orm_execute")
def _apply_org_filter(execute_state) -> None:
    """Globally scope every ORM SELECT of an org-owned entity to the current org.

    A no-op when there is no org context (login, background jobs, platform
    admins) or when a query explicitly opts out with
    ``execution_options(skip_org_filter=True)``.
    """
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
        or execute_state.execution_options.get("skip_org_filter", False)
    ):
        return

    org_id = get_current_org_id()
    if org_id is None:
        return

    # Bind the value with the org_id column's *own* type. Binding a bare
    # ``uuid.UUID`` inside a loader-criteria lambda makes SQLAlchemy infer its
    # generic ``Uuid`` type, which renders differently from the app's column
    # type on some dialects (e.g. 32-char hex vs hyphenated under the SQLite
    # test shim) and silently matches nothing. ``org_id`` columns are uniform,
    # so any org-scoped table's type works — use the current entity's own.
    bound = literal(org_id, type_=_org_id_type())

    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            OrgScopedMixin,
            lambda cls: cls.org_id == bound,
            include_aliases=True,
        )
    )


def _org_id_type():
    """The runtime SQL type of the shared ``org_id`` column.

    Resolved from the ``companies`` table (always present, always org-scoped) so
    this works whether the column is native PG ``UUID`` or the SQLite test
    shim's string decorator, with no import-time dependency on a model class.
    """
    from app.database import Base

    return Base.metadata.tables["companies"].columns["org_id"].type
