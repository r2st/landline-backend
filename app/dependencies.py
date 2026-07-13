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
from app.models.user import User
from app.security import decode_token

logger = logging.getLogger(__name__)

# Roles allowed to view other users' data via ``?view_as_user_id=``.
PRIVILEGED_ROLES = ("admin", "manager")

# Optional so endpoints can gracefully handle missing tokens
_bearer = HTTPBearer(auto_error=False)


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
    """FastAPI dependency that enforces one or more allowed roles."""

    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{user.role}' is not allowed. Required: {', '.join(roles)}",
            )
        return user

    return _check


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
        exists = (
            await db.execute(select(User.id).where(User.id == target))
        ).scalar_one_or_none()
        if exists is None:
            raise HTTPException(status_code=404, detail="User not found")
        return UserScope(current_user, target, see_all=False)

    # Regular users, and privileged users with no view_as override: own data.
    return UserScope(current_user, current_user.id, see_all=False)
