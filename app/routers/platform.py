"""Platform Admin API — cross-organization management (SaaS multi-tenancy, Phase 2).

These routes are the "god view" over every tenant on the platform and are
restricted to the ``platform_admin`` role. Unlike the org-scoped ``/tenants``
router (which an ``org_admin`` uses to manage *their own* org), this router lets
a platform operator provision organizations, inspect platform-wide usage, and
*impersonate* any user for debugging.

Why platform admins see everything: a ``platform_admin`` carries ``org_id =
NULL``, so the global org read-filter (see :mod:`app.models.mixins`) is a no-op
for their requests and every SELECT here spans all organizations. Each route
still asserts the role explicitly via :meth:`OrgScope.check_permission` so the
guard is visible at the call site and never depends solely on the mount.

Endpoints (mounted under the API prefix, e.g. ``/api/v1/platform``):
    POST   /platform/organizations        — create an organization
    GET    /platform/organizations        — list all orgs (+ user / usage counts)
    GET    /platform/organizations/{id}   — org detail (+ users, usage)
    PUT    /platform/organizations/{id}   — update name/plan/active/config/...
    DELETE /platform/organizations/{id}   — soft-deactivate (is_active=False)
    GET    /platform/stats                — platform-wide roll-up
    POST   /platform/impersonate/{uid}    — mint a scoped JWT for any user
"""

import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEvent
from app.models.tenant import BILLING_PLANS, Organization, TenantUsage
from app.models.user import User, UserRole, normalize_role
from app.security import create_access_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platform", tags=["platform"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# EmailSend rows that count as an outbound email for platform metrics — mirrors
# the dashboard's headline "emails sent" definition (composed + skipped-but-logged).
_SENT_STATUSES = ("sent", "skipped")


# --- Schemas ---------------------------------------------------------------


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    # Optional — derived from ``name`` when omitted (lowercased, hyphenated).
    slug: Optional[str] = Field(None, min_length=2, max_length=63)
    industry: Optional[str] = None
    billing_plan: str = "free"
    config: dict = Field(default_factory=dict)
    branding: dict = Field(default_factory=dict)
    ai_monthly_call_limit: Optional[int] = None


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    slug: Optional[str] = Field(None, min_length=2, max_length=63)
    industry: Optional[str] = None
    billing_plan: Optional[str] = None
    config: Optional[dict] = None
    branding: Optional[dict] = None
    ai_monthly_call_limit: Optional[int] = None
    is_active: Optional[bool] = None


class OrgUsage(BaseModel):
    user_count: int = 0
    emails_sent: int = 0
    ai_calls: int = 0


class OrganizationSummary(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    industry: Optional[str]
    billing_plan: str
    ai_monthly_call_limit: Optional[int]
    effective_ai_call_limit: Optional[int]
    created_at: str
    usage: OrgUsage
    # A user in this org the platform admin can impersonate to "view as" the org
    # (prefers an org_admin). ``None`` when the org has no active users yet.
    impersonatable_user_id: Optional[uuid.UUID] = None


class OrganizationUser(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True


class OrganizationDetail(OrganizationSummary):
    config: dict
    branding: dict
    users: list[OrganizationUser] = Field(default_factory=list)


class PlatformStats(BaseModel):
    total_organizations: int
    active_organizations: int
    total_users: int
    total_emails_sent: int


class ImpersonationToken(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: uuid.UUID
    email: str
    role: str
    org_id: Optional[uuid.UUID] = None
    org_name: Optional[str] = None


# --- Helpers ---------------------------------------------------------------


def _slugify(value: str) -> str:
    """Best-effort URL-safe slug from a free-text name."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:63]


async def _emails_sent_by_org(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Per-org outbound email counts, summing both send-tracking tables.

    Sends land in one of two places depending on the path: the compose/engagement
    flow logs an ``EmailSend`` row; sequence steps log an ``OutreachEvent`` with
    ``event_type='sent'`` (see the dual-tracking note in the activity log).
    """
    counts: dict[uuid.UUID, int] = {}

    rows = await db.execute(
        select(EmailSend.org_id, func.count(EmailSend.id))
        .where(EmailSend.status.in_(_SENT_STATUSES))
        .group_by(EmailSend.org_id)
    )
    for org_id, n in rows.all():
        counts[org_id] = counts.get(org_id, 0) + (n or 0)

    rows = await db.execute(
        select(OutreachEvent.org_id, func.count(OutreachEvent.id))
        .where(OutreachEvent.event_type == "sent")
        .group_by(OutreachEvent.org_id)
    )
    for org_id, n in rows.all():
        counts[org_id] = counts.get(org_id, 0) + (n or 0)

    return counts


async def _users_by_org(db: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(User.org_id, func.count(User.id))
        .where(User.org_id.isnot(None))
        .group_by(User.org_id)
    )
    return {org_id: (n or 0) for org_id, n in rows.all()}


async def _ai_calls_by_org(db: AsyncSession) -> dict[uuid.UUID, int]:
    rows = await db.execute(
        select(TenantUsage.tenant_id, func.count(TenantUsage.id)).group_by(
            TenantUsage.tenant_id
        )
    )
    return {org_id: (n or 0) for org_id, n in rows.all()}


async def _impersonatable_user_by_org(db: AsyncSession) -> dict[uuid.UUID, uuid.UUID]:
    """Pick one active user per org to impersonate, preferring higher roles.

    The org switcher needs a concrete user to mint a scoped token for; a higher
    role gives the most representative "view as" of the org.
    """
    rows = await db.execute(
        select(User.org_id, User.id, User.role)
        .where(User.org_id.isnot(None), User.is_active.is_(True))
    )
    best: dict[uuid.UUID, tuple[int, uuid.UUID]] = {}
    for org_id, user_id, role in rows.all():
        rank = UserRole.rank(normalize_role(role))
        current = best.get(org_id)
        if current is None or rank > current[0]:
            best[org_id] = (rank, user_id)
    return {org_id: uid for org_id, (_, uid) in best.items()}


async def _get_org_or_404(db: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = (
        await db.execute(select(Organization).where(Organization.id == org_id))
    ).scalar_one_or_none()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


def _summary(org: Organization, usage: OrgUsage, impersonatable: Optional[uuid.UUID]) -> dict:
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "is_active": org.is_active,
        "industry": org.industry,
        "billing_plan": org.billing_plan,
        "ai_monthly_call_limit": org.ai_monthly_call_limit,
        "effective_ai_call_limit": org.effective_ai_call_limit,
        "created_at": org.created_at.isoformat() if org.created_at else "",
        "usage": usage,
        "impersonatable_user_id": impersonatable,
    }


# --- Routes ----------------------------------------------------------------


@router.get("/stats", response_model=PlatformStats)
async def platform_stats(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide roll-up across every organization (platform admin only)."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)

    total_orgs = (await db.execute(select(func.count(Organization.id)))).scalar() or 0
    active_orgs = (
        await db.execute(
            select(func.count(Organization.id)).where(Organization.is_active.is_(True))
        )
    ).scalar() or 0
    total_users = (await db.execute(select(func.count(User.id)))).scalar() or 0

    email_sends = (
        await db.execute(
            select(func.count(EmailSend.id)).where(EmailSend.status.in_(_SENT_STATUSES))
        )
    ).scalar() or 0
    outreach_sends = (
        await db.execute(
            select(func.count(OutreachEvent.id)).where(OutreachEvent.event_type == "sent")
        )
    ).scalar() or 0

    return PlatformStats(
        total_organizations=total_orgs,
        active_organizations=active_orgs,
        total_users=total_users,
        total_emails_sent=email_sends + outreach_sends,
    )


@router.get("/organizations", response_model=list[OrganizationSummary])
async def list_organizations(
    active_only: bool = Query(False),
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """List every organization with per-org user / usage counts (platform admin only)."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)

    stmt = select(Organization).order_by(Organization.created_at.desc())
    if active_only:
        stmt = stmt.where(Organization.is_active.is_(True))
    orgs = (await db.execute(stmt)).scalars().all()

    users = await _users_by_org(db)
    emails = await _emails_sent_by_org(db)
    ai_calls = await _ai_calls_by_org(db)
    impersonatable = await _impersonatable_user_by_org(db)

    return [
        _summary(
            org,
            OrgUsage(
                user_count=users.get(org.id, 0),
                emails_sent=emails.get(org.id, 0),
                ai_calls=ai_calls.get(org.id, 0),
            ),
            impersonatable.get(org.id),
        )
        for org in orgs
    ]


@router.post(
    "/organizations", response_model=OrganizationDetail, status_code=status.HTTP_201_CREATED
)
async def create_organization(
    body: OrganizationCreate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Provision a new organization (platform admin only)."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)

    slug = (body.slug or _slugify(body.name)).lower().strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=400,
            detail="slug must be 2-63 chars: lowercase letters, digits, hyphens",
        )
    if body.billing_plan not in BILLING_PLANS:
        raise HTTPException(status_code=400, detail=f"billing_plan must be one of {BILLING_PLANS}")

    existing = (
        await db.execute(select(Organization).where(Organization.slug == slug))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"An organization with slug {slug!r} already exists")

    org = Organization(
        name=body.name,
        slug=slug,
        industry=body.industry,
        billing_plan=body.billing_plan,
        config=body.config or {},
        branding=body.branding or {},
        ai_monthly_call_limit=body.ai_monthly_call_limit,
    )
    db.add(org)
    await db.flush()
    await db.refresh(org)
    logger.info("Platform admin %s created organization %s (%s)", scope.user.email, org.name, slug)
    return _detail(org, OrgUsage(), [], None)


@router.get("/organizations/{org_id}", response_model=OrganizationDetail)
async def get_organization(
    org_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Organization detail with its users and usage stats (platform admin only)."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)
    org = await _get_org_or_404(db, org_id)

    org_users = (
        await db.execute(
            select(User).where(User.org_id == org_id).order_by(User.created_at.desc())
        )
    ).scalars().all()

    emails = await _emails_sent_by_org(db)
    ai_calls = await _ai_calls_by_org(db)
    usage = OrgUsage(
        user_count=len(org_users),
        emails_sent=emails.get(org_id, 0),
        ai_calls=ai_calls.get(org_id, 0),
    )
    impersonatable = _pick_impersonatable(org_users)
    return _detail(org, usage, org_users, impersonatable)


@router.put("/organizations/{org_id}", response_model=OrganizationDetail)
async def update_organization(
    org_id: uuid.UUID,
    body: OrganizationUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Update an organization's name, plan, config, branding, or active state."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)
    org = await _get_org_or_404(db, org_id)

    if body.billing_plan is not None:
        if body.billing_plan not in BILLING_PLANS:
            raise HTTPException(status_code=400, detail=f"billing_plan must be one of {BILLING_PLANS}")
        org.billing_plan = body.billing_plan
    if body.slug is not None:
        slug = body.slug.lower().strip()
        if not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=400,
                detail="slug must be 2-63 chars: lowercase letters, digits, hyphens",
            )
        if slug != org.slug:
            clash = (
                await db.execute(
                    select(Organization).where(
                        Organization.slug == slug, Organization.id != org_id
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise HTTPException(status_code=409, detail=f"slug {slug!r} is already taken")
            org.slug = slug
    if body.name is not None:
        org.name = body.name
    if body.industry is not None:
        org.industry = body.industry
    if body.config is not None:
        org.config = body.config
    if body.branding is not None:
        org.branding = body.branding
    if body.ai_monthly_call_limit is not None:
        org.ai_monthly_call_limit = body.ai_monthly_call_limit
    if body.is_active is not None:
        org.is_active = body.is_active

    await db.flush()
    await db.refresh(org)

    org_users = (
        await db.execute(
            select(User).where(User.org_id == org_id).order_by(User.created_at.desc())
        )
    ).scalars().all()
    emails = await _emails_sent_by_org(db)
    ai_calls = await _ai_calls_by_org(db)
    usage = OrgUsage(
        user_count=len(org_users),
        emails_sent=emails.get(org_id, 0),
        ai_calls=ai_calls.get(org_id, 0),
    )
    logger.info("Platform admin %s updated organization %s", scope.user.email, org.slug)
    return _detail(org, usage, org_users, _pick_impersonatable(org_users))


@router.delete("/organizations/{org_id}")
async def deactivate_organization(
    org_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Soft-deactivate an organization (``is_active=False``; data is retained)."""
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)
    org = await _get_org_or_404(db, org_id)
    org.is_active = False
    await db.flush()
    logger.info("Platform admin %s deactivated organization %s", scope.user.email, org.slug)
    return {"status": "deactivated", "org_id": str(org_id)}


@router.post("/impersonate/{user_id}", response_model=ImpersonationToken)
async def impersonate_user(
    user_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Mint a JWT scoped to *user_id*'s org and role, for debugging (platform admin only).

    The token carries the target user's ``sub``, so on every subsequent request
    ``get_current_user`` resolves that user from the DB and binds *their* org —
    the platform admin genuinely sees exactly what the target user sees.
    """
    scope.check_permission(UserRole.PLATFORM_ADMIN.value)

    target = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not target.is_active:
        raise HTTPException(status_code=400, detail="Cannot impersonate a deactivated user")

    org_name = None
    if target.org_id is not None:
        org = (
            await db.execute(select(Organization).where(Organization.id == target.org_id))
        ).scalar_one_or_none()
        org_name = org.name if org is not None else None

    token = create_access_token(target.id, target.role, target.org_id)
    logger.info(
        "Platform admin %s is impersonating user %s (org=%s)",
        scope.user.email,
        target.email,
        target.org_id,
    )
    return ImpersonationToken(
        access_token=token,
        user_id=target.id,
        email=target.email,
        role=target.role,
        org_id=target.org_id,
        org_name=org_name,
    )


# --- Detail assembly -------------------------------------------------------


def _pick_impersonatable(org_users) -> Optional[uuid.UUID]:
    """Highest-privilege active user in a list, or ``None``."""
    best_rank = -2
    best_id: Optional[uuid.UUID] = None
    for u in org_users:
        if not u.is_active:
            continue
        rank = UserRole.rank(normalize_role(u.role))
        if rank > best_rank:
            best_rank = rank
            best_id = u.id
    return best_id


def _detail(org: Organization, usage: OrgUsage, org_users, impersonatable) -> dict:
    data = _summary(org, usage, impersonatable)
    data["config"] = org.config or {}
    data["branding"] = org.branding or {}
    data["users"] = [OrganizationUser.model_validate(u) for u in org_users]
    return data
