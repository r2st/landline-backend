"""Organization self-service management (Phase 3 — SaaS multi-tenancy).

Lets an ``org_admin`` manage their *own* organization — settings, team members,
and usage — without platform-admin access. The organization is always the
caller's own, resolved from the JWT ``org_id`` claim; there is no ``org_id`` in
any path or body, so one org can never touch another's data.

Routes (mounted under the API prefix, e.g. ``/api/v1/org``):
    GET    /org/settings               — the caller's org settings
    PUT    /org/settings               — update settings (org_admin)
    GET    /org/members                — list members with roles
    POST   /org/members/invite         — invite a user (org_admin)
    PUT    /org/members/{id}/role       — change a member's role (org_admin)
    DELETE /org/members/{id}            — deactivate a member (org_admin)
    GET    /org/usage                  — usage stats + monthly trends

Reads (settings, members, usage) are open to any member of the org; writes are
gated by ``OrgScope.check_permission("org_admin")``. A ``platform_admin`` has no
org of their own (``org_id`` is NULL) — they manage organizations via the
``/tenants`` API, so the ``/org`` routes return 400 for them.
"""

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.tenant import Tenant
from app.models.user import User, UserRole, normalize_role
from app.schemas.org import (
    MemberInvite,
    MemberInviteResponse,
    MemberRoleUpdate,
    OrgSettingsRead,
    OrgSettingsUpdate,
    OrgUsageRead,
    UsageTrendPoint,
)
from app.schemas.auth import UserRead
from app.security import hash_password
from app.services.org_service import get_org_service
from app.services.tenant_service import get_tenant_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/org", tags=["org"])


async def _require_org(scope: OrgScope, db: AsyncSession) -> Tenant:
    """Load the caller's organization, or 400 if they have none.

    Platform admins (``org_id`` NULL) manage orgs via ``/tenants``; the
    self-service ``/org`` routes are for a user acting within their own org.
    """
    if scope.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization context — platform admins manage orgs via /tenants",
        )
    org = (
        await db.execute(select(Tenant).where(Tenant.id == scope.org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


async def _member_or_404(db: AsyncSession, member_id: uuid.UUID, org_id: uuid.UUID) -> User:
    """Load a user in the caller's org, or 404 (never leak other orgs' users)."""
    user = (
        await db.execute(select(User).where(User.id == member_id))
    ).scalar_one_or_none()
    if user is None or user.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")
    return user


# --- Settings ----------------------------------------------------------------


@router.get("/settings", response_model=OrgSettingsRead)
async def get_org_settings(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Return the caller's organization settings (any member)."""
    return await _require_org(scope, db)


@router.put("/settings", response_model=OrgSettingsRead)
async def update_org_settings(
    body: OrgSettingsUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update org name, industry, config, or branding (org_admin only)."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    org = await _require_org(scope, db)
    if body.name is not None:
        org.name = body.name
    if body.industry is not None:
        org.industry = body.industry
    if body.config is not None:
        org.config = body.config
    if body.branding is not None:
        org.branding = body.branding
    await db.flush()
    await db.refresh(org)
    logger.info("Org %s settings updated by %s", org.slug, scope.user.email)
    return org


# --- Members -----------------------------------------------------------------


@router.get("/members", response_model=list[UserRead])
async def list_members(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """List the org's members with roles and status (any member)."""
    await _require_org(scope, db)
    rows = (
        await db.execute(
            select(User).where(User.org_id == scope.org_id).order_by(User.created_at.asc())
        )
    ).scalars().all()
    return list(rows)


@router.post(
    "/members/invite",
    response_model=MemberInviteResponse,
    status_code=status.HTTP_201_CREATED,
)
async def invite_member(
    body: MemberInvite,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Invite a new user into the org with a temporary password (org_admin)."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    org = await _require_org(scope, db)

    email = body.email.lower()
    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        )

    # Generated when the admin doesn't supply one; always change-on-first-login.
    temp_password = body.password or secrets.token_urlsafe(12)
    member = User(
        email=email,
        name=body.name,
        role=normalize_role(body.role),
        org_id=org.id,
        password_hash=hash_password(temp_password),
        must_change_password=True,
        created_by=scope.user.id,
    )
    db.add(member)
    await db.flush()
    await db.refresh(member)
    logger.info("Org %s: %s invited member %s (role=%s)", org.slug, scope.user.email, email, member.role)

    return MemberInviteResponse(
        **UserRead.model_validate(member, from_attributes=True).model_dump(),
        temporary_password=temp_password,
    )


@router.put("/members/{member_id}/role", response_model=UserRead)
async def change_member_role(
    member_id: uuid.UUID,
    body: MemberRoleUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Change a member's role within the org (org_admin only)."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    await _require_org(scope, db)
    member = await _member_or_404(db, member_id, scope.org_id)

    # A platform_admin's role is never editable from an org context.
    if normalize_role(member.role) == UserRole.PLATFORM_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot change a platform admin's role",
        )
    # An admin can't demote themselves out of org_admin — it could orphan the
    # org with no administrator.
    if member.id == scope.user.id and normalize_role(body.role) != UserRole.ORG_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot change your own admin role",
        )

    member.role = normalize_role(body.role)
    await db.flush()
    await db.refresh(member)
    logger.info("Org member %s role changed to %s by %s", member.email, member.role, scope.user.email)
    return member


@router.delete("/members/{member_id}", response_model=UserRead)
async def remove_member(
    member_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Remove a member by deactivating the account (org_admin only, soft)."""
    scope.check_permission(UserRole.ORG_ADMIN.value)
    await _require_org(scope, db)

    if member_id == scope.user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot remove your own account",
        )
    member = await _member_or_404(db, member_id, scope.org_id)
    if normalize_role(member.role) == UserRole.PLATFORM_ADMIN.value:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot remove a platform admin",
        )
    member.is_active = False
    await db.flush()
    await db.refresh(member)
    logger.info("Org member %s deactivated by %s", member.email, scope.user.email)
    return member


# --- Usage -------------------------------------------------------------------


@router.get("/usage", response_model=OrgUsageRead)
async def org_usage(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Usage stats for the caller's org — counts, AI metering, trends (any member)."""
    org = await _require_org(scope, db)
    now = datetime.now(timezone.utc)
    period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    org_svc = get_org_service()
    counts = await org_svc.resource_counts(db, org.id, period_start)
    trend = await org_svc.monthly_trend(db, org.id, now)

    ai = await get_tenant_service().usage_summary(db, org, since=period_start)

    return OrgUsageRead(
        org_id=org.id,
        billing_plan=org.billing_plan,
        period_start=period_start,
        contacts=counts["contacts"],
        members=counts["members"],
        emails_sent_this_month=counts["emails_sent_this_month"],
        emails_sent_total=counts["emails_sent_total"],
        ai_calls=ai["ai_calls"],
        ai_call_limit=ai["ai_call_limit"],
        ai_calls_remaining=ai["remaining_calls"],
        over_limit=ai["over_limit"],
        estimated_cost_usd=ai["estimated_cost_usd"],
        by_feature=ai["by_feature"],
        trend=[UsageTrendPoint(**t) for t in trend],
    )
