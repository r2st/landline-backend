"""Admin user-management router — restricted to the ``admin`` role.

Endpoints (mounted under the API prefix, e.g. ``/api/v1/admin``):
    GET    /admin/users        — list all users
    POST   /admin/users        — create a user with an initial password
    PATCH  /admin/users/{id}   — update role / active status / name
    DELETE /admin/users/{id}   — soft-delete (deactivate) a user

Every route requires a valid admin bearer token via ``require_role("admin")``.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_role
from app.database import get_db
from app.models.user import User, UserRole
from app.schemas.auth import UserCreate, UserRead, UserUpdate
from app.security import hash_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


async def _get_user_or_404(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/users", response_model=list[UserRead])
async def list_users(
    admin: User = Depends(require_role(UserRole.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """List all users, newest first."""
    users = (
        await db.execute(select(User).order_by(User.created_at.desc()))
    ).scalars().all()
    return list(users)


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    admin: User = Depends(require_role(UserRole.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user with an admin-supplied initial password."""
    email = payload.email.lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    user = User(
        email=email,
        name=payload.name,
        role=payload.role,
        password_hash=hash_password(payload.password),
        must_change_password=payload.must_change_password,
        created_by=admin.id,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s created user %s (role=%s)", admin.email, email, payload.role)
    return user


@router.patch("/users/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    admin: User = Depends(require_role(UserRole.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """Update a user's role, active status, or name."""
    user = await _get_user_or_404(db, user_id)

    # Guard against an admin locking themselves out.
    if user.id == admin.id:
        if payload.is_active is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account",
            )
        if payload.role is not None and payload.role != UserRole.ADMIN.value:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot change your own admin role",
            )

    if payload.name is not None:
        user.name = payload.name
    if payload.role is not None:
        user.role = payload.role
    if payload.is_active is not None:
        user.is_active = payload.is_active

    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s updated user %s", admin.email, user.email)
    return user


@router.delete("/users/{user_id}", response_model=UserRead)
async def deactivate_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_role(UserRole.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a user by deactivating the account (history is preserved)."""
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot delete your own account",
        )
    user = await _get_user_or_404(db, user_id)
    user.is_active = False
    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s deactivated user %s", admin.email, user.email)
    return user
