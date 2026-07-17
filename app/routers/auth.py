"""Authentication endpoints — login, registration, token refresh, current user."""

import logging
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import PRIVILEGED_ROLES, get_current_user
from app.rate_limit import AUTH_RATE_LIMIT, limiter
from app.models.tenant import Organization
from app.models.user import User, UserRole, normalize_role
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    ProfileUpdateRequest,
    RefreshRequest,
    RegisterRequest,
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

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _slugify(value: str) -> str:
    """Best-effort URL-safe slug from a free-text org name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:63] or "org"


async def _unique_slug(db: AsyncSession, base: str) -> str:
    """Return ``base`` (or ``base-<suffix>``) that no organization uses yet."""
    slug = base
    for _ in range(5):
        exists = (
            await db.execute(select(Organization.id).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if exists is None:
            return slug
        slug = f"{base[:55]}-{uuid.uuid4().hex[:6]}"
    # Extremely unlikely; fall back to a fully random slug.
    return f"org-{uuid.uuid4().hex[:12]}"


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit(AUTH_RATE_LIMIT)
async def register(
    request: Request, payload: RegisterRequest, db: AsyncSession = Depends(get_db)
):
    """Public self-service sign-up: create an organization + its first admin.

    Creates the ``Organization`` (on the free plan) and a single ``org_admin``
    user, then returns access/refresh tokens so the caller is logged straight
    in. Rate-limited like login to blunt abuse. Neither ``Organization`` nor
    ``User`` is org-scoped, so the writes are safe without a bound org context;
    the new user's ``org_id`` is set explicitly.
    """
    email = payload.admin_email.lower().strip()

    # Global email uniqueness — surfaced as 409 rather than a DB IntegrityError.
    clash = (
        await db.execute(select(User.id).where(User.email == email))
    ).scalar_one_or_none()
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists",
        )

    # Resolve a unique, valid slug.
    if payload.slug:
        slug = payload.slug.lower().strip()
        if not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="slug must be 2-63 chars: lowercase letters, digits, hyphens",
            )
        taken = (
            await db.execute(select(Organization.id).where(Organization.slug == slug))
        ).scalar_one_or_none()
        if taken is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An organization with slug {slug!r} already exists",
            )
    else:
        slug = await _unique_slug(db, _slugify(payload.org_name))

    org = Organization(name=payload.org_name.strip(), slug=slug, is_active=True, billing_plan="free")
    db.add(org)
    await db.flush()  # assign org.id

    admin = User(
        email=email,
        name=(payload.admin_name or email.split("@")[0]).strip(),
        role=UserRole.ORG_ADMIN.value,
        password_hash=hash_password(payload.password),
        is_active=True,
        must_change_password=False,
        org_id=org.id,
    )
    db.add(admin)
    await db.flush()

    access = create_access_token(admin.id, admin.role, admin.org_id)
    refresh = create_refresh_token(admin.id)
    logger.info("New organization registered: %s (%s) admin=%s", org.name, slug, email)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        must_change_password=False,
        role=admin.role,
        org_id=admin.org_id,
        org_name=org.name,
    )


@router.post("/login", response_model=TokenResponse)
@limiter.limit(AUTH_RATE_LIMIT)
async def login(request: Request, payload: LoginRequest, db: AsyncSession = Depends(get_db)):
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

    # Resolve the org (name for the response + active check). Members of a
    # deactivated organization cannot log in; platform admins have no org.
    org_name = None
    if user.org_id is not None:
        org = (
            await db.execute(
                select(Organization).where(Organization.id == user.org_id)
            )
        ).scalar_one_or_none()
        if org is None or not org.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Organization is deactivated",
            )
        org_name = org.name

    access = create_access_token(user.id, user.role, user.org_id)
    refresh = create_refresh_token(user.id)

    logger.info("User %s logged in (org=%s)", user.email, user.org_id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        must_change_password=user.must_change_password,
        role=user.role,
        org_id=user.org_id,
        org_name=org_name,
    )


@router.post("/refresh", response_model=TokenResponse)
@limiter.limit(AUTH_RATE_LIMIT)
async def refresh(request: Request, payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
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

    # A refresh must also fail once the user's organization is deactivated,
    # otherwise a suspended tenant could mint fresh access tokens indefinitely.
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

    access = create_access_token(user.id, user.role, user.org_id)
    new_refresh = create_refresh_token(user.id)
    return TokenResponse(
        access_token=access,
        refresh_token=new_refresh,
        role=user.role,
        org_id=user.org_id,
    )


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)):
    """Return the currently authenticated user's profile."""
    return user


@router.get("/team-members", response_model=list[UserRead])
async def team_members(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List active users for the admin/manager "View as" switcher.

    Only admins and managers may enumerate the team; regular users get 403.
    Returns active users ordered by name so the dropdown can offer per-user
    inboxes alongside the "All Users" option.
    """
    if user.role not in PRIVILEGED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only admins and managers can list team members",
        )
    stmt = select(User).where(User.is_active.is_(True))
    # Org admins/managers see only their own org's members; a platform admin
    # (org_id is None) sees everyone.
    if normalize_role(user.role) != UserRole.PLATFORM_ADMIN.value and user.org_id is not None:
        stmt = stmt.where(User.org_id == user.org_id)
    result = await db.execute(stmt.order_by(User.name.asc()))
    return list(result.scalars().all())


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
