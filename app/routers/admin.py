"""Admin endpoints — user management (CRUD)."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.models.user import User
from app.schemas.auth import (
    AdminResetPasswordRequest,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.security import hash_password

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/users", response_model=list[UserRead])
async def list_users(
    role: str | None = Query(None, pattern="^(admin|manager|agent)$"),
    is_active: bool | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """List all users. Admin only."""
    query = select(User)
    if role is not None:
        query = query.where(User.role == role)
    if is_active is not None:
        query = query.where(User.is_active == is_active)
    query = query.order_by(User.created_at.desc())
    query = query.offset((page - 1) * page_size).limit(page_size)
    return list((await db.execute(query)).scalars().all())


@router.post("/users", response_model=UserRead, status_code=201)
async def create_user(
    payload: UserCreate,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Create a new user. Admin only."""
    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    user = User(
        email=payload.email,
        name=payload.name,
        role=payload.role,
        password_hash=hash_password(payload.password),
        must_change_password=payload.must_change_password,
        created_by=_admin.id,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s created user %s (role=%s)", _admin.email, user.email, user.role)
    return user


@router.get("/users/{user_id}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Get a single user by ID. Admin only."""
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


async def _apply_user_update(
    db: AsyncSession, admin: User, user_id: uuid.UUID, payload: UserUpdate
) -> User:
    """Shared update logic for both PUT and PATCH.

    Enforces email uniqueness, hashes a new password if given, and prevents an
    admin from locking themselves out (deactivating or demoting their own
    account).
    """
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    update_data = payload.model_dump(exclude_unset=True)

    # Self-protection: an admin cannot deactivate or demote themselves.
    if user.id == admin.id:
        if update_data.get("is_active") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot deactivate your own account",
            )
        if "role" in update_data and update_data["role"] != user.role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You cannot change your own role",
            )

    # Email uniqueness check.
    if "email" in update_data and update_data["email"]:
        existing = (
            await db.execute(
                select(User).where(User.email == update_data["email"], User.id != user_id)
            )
        ).scalar_one_or_none()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists",
            )

    # Hash password if provided.
    if "password" in update_data and update_data["password"]:
        update_data["password_hash"] = hash_password(update_data.pop("password"))
    else:
        update_data.pop("password", None)

    for field, value in update_data.items():
        setattr(user, field, value)

    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s updated user %s", admin.email, user.email)
    return user


@router.put("/users/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Update a user (full/partial). Admin only."""
    return await _apply_user_update(db, _admin, user_id, payload)


@router.patch("/users/{user_id}", response_model=UserRead)
async def patch_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Partially update a user. Admin only."""
    return await _apply_user_update(db, _admin, user_id, payload)


@router.post("/users/{user_id}/reset-password", response_model=UserRead)
async def reset_password(
    user_id: uuid.UUID,
    payload: AdminResetPasswordRequest,
    _admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Reset another user's password. Admin only.

    Sets a new password and, by default, flags the account so the user must
    change it on next login.
    """
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = payload.must_change_password
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s reset password for user %s", _admin.email, user.email)
    return user


@router.delete("/users/{user_id}", response_model=UserRead)
async def delete_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Soft-delete a user (deactivate). Admin only. Cannot delete yourself.

    We soft-delete — set ``is_active = False`` — rather than hard-deleting so
    that historical attribution (who contacted which client, who sent which
    email) is preserved. The account can no longer log in.
    """
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    await db.flush()
    await db.refresh(user)
    logger.info("Admin %s soft-deleted user %s", admin.email, user.email)
    return user
