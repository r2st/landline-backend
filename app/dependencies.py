"""Shared FastAPI dependencies — current-user extraction, role checks, scoping."""

import logging
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.tenant import Organization
from app.models.user import User, UserRole, normalize_role
from app.org_context import set_current_org_id
from app.security import decode_token

logger = logging.getLogger(__name__)

# Roles allowed to view other users' data via ``?view_as_user_id=``. Includes the
# legacy names so pre-multi-tenancy tokens/fixtures keep working alongside the
# new org_* / platform_admin roles.
PRIVILEGED_ROLES = (
    "admin",
    "manager",
    UserRole.ORG_ADMIN.value,
    UserRole.ORG_MANAGER.value,
    UserRole.PLATFORM_ADMIN.value,
)

# Optional so endpoints can gracefully handle missing tokens
_bearer = HTTPBearer(auto_error=False)


def scope_users_to_org(stmt, caller: User):
    """Restrict a ``User`` query to ``caller``'s org. **Use this every time.**

    :class:`~app.models.user.User` is one of three models deliberately outside
    :class:`~app.models.mixins.OrgScopedMixin` — membership *is* the ``org_id``
    column, and login has to read a user before any org context is bound — so
    the ``do_orm_execute`` filter that silently scopes every other SELECT in the
    application does not scope this one. Isolation here is whatever the call
    site remembered to write, which is why it is worth having exactly one thing
    to remember.

    There were four hand-rolled versions of this predicate and two places that
    needed it and didn't have it: ``GET /coaching/team`` enumerated every active
    user on the platform — and, because it generates a snapshot for each rep it
    lists, *wrote* a RepPerformance row per stranger — and
    ``POST /coaching/generate`` accepted any user id on the platform as
    ``target_user_id`` and returned a snapshot labelled with that person's name.
    Neither looked wrong: an unscoped ``select(User)`` looks exactly like every
    scoped query in the codebase, because everywhere else the scoping is
    invisible.

    A ``platform_admin`` is exempt: the role exists to reach across orgs, and
    ``/platform`` and ``/tenants`` are built on its doing so. The check is on the
    *role* rather than on ``org_id is None``, so a caller who is not a platform
    admin and somehow has no org gets ``org_id IS NULL`` — matching only other
    org-less rows — rather than the whole table.
    """
    if normalize_role(caller.role) == UserRole.PLATFORM_ADMIN.value:
        return stmt
    return stmt.where(User.org_id == caller.org_id)


def scope_users_to_current_org(stmt):
    """Restrict a ``User`` query to the org bound to the current context.

    The same rule as :func:`scope_users_to_org`, for code with no ``User`` in
    hand — services and Celery tasks, which are handed a session and an org
    binding rather than a caller. Semantics deliberately match the global filter
    in :mod:`app.models.mixins`: no org bound (platform admin, or a background
    run that never entered :func:`app.tasks.org_runner.for_each_org`) means no
    filter, exactly as it does for every org-scoped model.

    ``generate_weekly_coaching`` is why this exists. It runs inside
    ``for_each_org``, which binds each org in turn precisely so that scoped reads
    come out right — and then read users, which are not scoped, so every org's
    pass enumerated every user *on the platform* and wrote a RepPerformance row
    for each. The org binding that was supposed to isolate the loop instead
    stamped N-orgs × N-users rows, each attributed to whichever tenant the loop
    happened to be on.
    """
    from app.org_context import get_current_org_id

    org_id = get_current_org_id()
    if org_id is None:
        return stmt
    return stmt.where(User.org_id == org_id)


async def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extract the authenticated user from the JWT Bearer token.

    Raises 401 if the token is missing, expired, or belongs to a
    deactivated / non-existent user.
    """
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(creds.credentials)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    user = (
        await db.execute(select(User).where(User.id == UUID(user_id)))
    ).scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    if not user.is_active:
        # A deactivated user's token is no longer valid credentials → 401.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is deactivated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Reject users whose *organization* has been deactivated. Enforced here (the
    # single dependency every authenticated route runs through) so a suspended
    # tenant is locked out everywhere, not just at login. Platform admins carry
    # ``org_id = NULL`` and are exempt. ``Organization`` is not org-scoped, and
    # no org context is bound yet, so this select is not self-filtered.
    if user.org_id is not None:
        org_active = (
            await db.execute(
                select(Organization.is_active).where(Organization.id == user.org_id)
            )
        ).scalar_one_or_none()
        if not org_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization is deactivated",
            )

    # Bind the caller's org for the rest of the request: drives the automatic
    # ``org_id`` on inserts and the global read filter (see app.org_context /
    # app.models.mixins). ``platform_admin`` has org_id=None → not filtered.
    set_current_org_id(user.org_id)
    return user


async def get_current_user_optional(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    """Like ``get_current_user`` but returns ``None`` when no token is present.

    Use for endpoints that work for both authenticated users and API-key
    callers (backward compat).

    Note the asymmetry: **no token** → ``None`` (legacy/API-key caller, unscoped
    by ``get_scope``); a **present but invalid** token (bad signature, expired,
    or belonging to a deleted/deactivated user) → 401. Swallowing the latter
    would let a deactivated user's token silently fall back to the unscoped
    "see everything" path, so it must fail closed.
    """
    if creds is None:
        return None
    return await get_current_user(creds, db)


def require_role(*roles: str):
    """FastAPI dependency enforcing a minimum role in the privilege hierarchy.

    The caller passes the role(s) that may access the route; access is granted
    to anyone whose role is at least as privileged as the *least* privileged of
    them. So ``require_role("org_admin")`` also admits ``platform_admin``, and a
    higher role is never locked out of a lower-privilege route. Legacy role
    names (``admin``/``manager``/``agent``) are normalized first.
    """
    min_required = min(UserRole.rank(normalize_role(r)) for r in roles)

    async def _check(user: User = Depends(get_current_user)) -> User:
        if UserRole.rank(normalize_role(user.role)) < min_required:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not allowed. Required: {', '.join(roles)}",
            )
        return user

    return _check


class OrgScope:
    """Resolved per-request organization scope + role-hierarchy gate.

    Injected into routes that want to be explicit about org scoping (reads are
    already filtered globally — see :mod:`app.models.mixins`). ``query(model)``
    returns a SELECT pre-filtered to the caller's org (a no-op extra safety net
    for platform admins, who see everything). ``check_permission`` enforces the
    role hierarchy.
    """

    def __init__(self, user: User, db: AsyncSession) -> None:
        self.user = user
        self.org_id = user.org_id
        self.role = normalize_role(user.role)
        self.db = db

    @property
    def is_platform_admin(self) -> bool:
        return self.role == UserRole.PLATFORM_ADMIN.value

    def query(self, model):
        """A SELECT for *model*, filtered to this org unless a platform admin."""
        from sqlalchemy import select

        stmt = select(model)
        if not self.is_platform_admin and hasattr(model, "org_id"):
            stmt = stmt.where(model.org_id == self.org_id)
        return stmt

    def check_permission(self, required_role: str) -> None:
        """Raise 403 unless the caller's role meets *required_role* or higher."""
        if UserRole.rank(self.role) < UserRole.rank(normalize_role(required_role)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )


async def get_org_scope(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgScope:
    """Resolve the :class:`OrgScope` for the authenticated caller."""
    return OrgScope(current_user, db)


class UserScope:
    """Resolved per-request data scope for user-owned tables.

    Encapsulates the "users see their own data; admins/managers see their own
    by default but can view any user's (or everyone's) via ``?view_as_user_id``"
    pattern so every router applies it identically.

    Attributes:
        user: the authenticated user, or ``None`` for API-key (legacy) callers.
        target_user_id: the user id to scope to, or ``None`` when unscoped.
        see_all: when True, no ``user_id`` filter is applied (see everything).
    """

    def __init__(
        self,
        user: Optional[User],
        target_user_id: Optional[UUID],
        see_all: bool,
    ) -> None:
        self.user = user
        self.target_user_id = target_user_id
        self.see_all = see_all

    @property
    def is_privileged(self) -> bool:
        return self.user is not None and self.user.role in PRIVILEGED_ROLES

    def apply(self, query, column):
        """Add the ``column == target_user_id`` filter unless seeing all."""
        if self.see_all:
            return query
        return query.where(column == self.target_user_id)

    def can_access(self, owner_id: Optional[UUID]) -> bool:
        """Whether a single record owned by ``owner_id`` is in scope."""
        if self.see_all:
            return True
        return owner_id == self.target_user_id

    @property
    def owner_id(self) -> Optional[UUID]:
        """The user id to stamp on newly-created records.

        For a privileged user acting as someone else, records are attributed to
        that someone; otherwise to the caller (or ``None`` for API-key callers).
        """
        if self.target_user_id is not None:
            return self.target_user_id
        return self.user.id if self.user is not None else None


async def get_scope(
    view_as_user_id: Optional[str] = Query(
        None,
        description="Admins/managers only: scope to a specific user id, or 'all'.",
    ),
    current_user: Optional[User] = Depends(get_current_user_optional),
    db: AsyncSession = Depends(get_db),
) -> UserScope:
    """Resolve the data scope for the current request.

    - No JWT (API-key / legacy): unscoped (sees everything) for back-compat.
    - Regular user: always scoped to their own data; ``view_as_user_id`` ignored.
    - Admin/manager: own data by default; ``view_as_user_id=<uuid>`` views that
      user's data, ``view_as_user_id=all`` views everyone's.
    """
    if current_user is None:
        return UserScope(None, None, see_all=True)

    privileged = current_user.role in PRIVILEGED_ROLES
    if privileged and view_as_user_id:
        if view_as_user_id.lower() == "all":
            return UserScope(current_user, None, see_all=True)
        try:
            target = UUID(view_as_user_id)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid view_as_user_id — expected a user id or 'all'.",
            )
        # Scoped to the caller's own org, by hand — see ``scope_users_to_org``
        # for why nothing does this for us.
        #
        # Unscoped, an org_admin could name any user id on the platform. The org
        # filter still stopped the *data* coming back, since every other model
        # is scoped, so what leaked was thinner: 404-vs-200 told them whether an
        # id existed anywhere, and any record created during the request was
        # stamped with a ``user_id`` belonging to another tenant. Neither is a
        # breach on its own; both are the kind of thing that becomes one as soon
        # as a new model or a cross-org report reads ``target_user_id`` without
        # re-checking.
        target_q = scope_users_to_org(
            select(User.id).where(User.id == target), current_user
        )
        exists = (await db.execute(target_q)).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserScope(current_user, target, see_all=False)

    # Regular users, and privileged users with no view_as override: own data.
    return UserScope(current_user, current_user.id, see_all=False)
