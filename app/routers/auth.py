"""Authentication endpoints — login, token refresh, current user, password change."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    TokenResponse,
    UserRead,
)
from app.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Authenticate with email + password. Returns access and refresh tokens."""
    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()

    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    access = create_access_token(user.id, user.role)
    refresh = create_refresh_token(user.id)
    logger.info("User %s logged in", user.email)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        must_change_password=user.must_change_password,
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    """Exchange a valid refresh token for a new access + refresh pair."""
    try:
        data = decode_token(payload.refresh_token)
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    if data.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token is not a refresh token",
        )

    from uuid import UUID

    user_id = data.get("sub")
    user = (
        await db.execute(select(User).where(User.id == UUID(user_id)))
    ).scalar_one_or_none()

    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )

    access = create_access_token(user.id, user.role)
    new_refresh = create_refresh_token(user.id)
    return TokenResponse(access_token=access, refresh_token=new_refresh)


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return user


@router.patch("/me", response_model=UserRead)
async def update_profile(
    payload: ProfileUpdateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update the current user's own profile (name and/or email).

    Role and active status are intentionally NOT editable here — those are
    admin-only via the admin API.
    """
    data = payload.model_dump(exclude_unset=True)

    if "email" in data and data["email"] and data["email"] != user.email:
        clash = (
            await db.execute(
                select(User).where(User.email == data["email"], User.id != user.id)
            )
        ).scalar_one_or_none()
        if clash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A user with this email already exists",
            )
        user.email = data["email"]

    if "name" in data and data["name"]:
        user.name = data["name"]

    await db.flush()
    await db.refresh(user)
    logger.info("User %s updated their profile", user.email)
    return user


@router.post("/change-password", status_code=200)
async def change_password(
    payload: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's password."""
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect",
        )

    user.password_hash = hash_password(payload.new_password)
    user.must_change_password = False
    await db.flush()
    logger.info("User %s changed their password", user.email)
    return {"detail": "Password changed successfully"}
