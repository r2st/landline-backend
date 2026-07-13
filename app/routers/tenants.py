"""Multi-tenant white-label platform API (feature 4.1).

Admin-only tenant CRUD plus per-tenant configuration (industry verticals, AI
prompt customization, branding), usage-based billing summaries, and a
``current`` view any authenticated user can read to drive white-label branding
and feature gating in the frontend.

Tenant management is restricted to ``admin`` users. The ``/tenants/current/*``
endpoints resolve the tenant from the caller's ``org_id`` and are readable by
any authenticated user.
"""

import logging
import re
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, require_role
from app.models.tenant import BILLING_PLANS, Tenant
from app.models.user import User
from app.services.tenant_service import get_tenant_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["tenants"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


# --- Schemas ---------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=2, max_length=63)
    industry: Optional[str] = None
    billing_plan: str = "free"
    config: dict = Field(default_factory=dict)
    branding: dict = Field(default_factory=dict)
    ai_monthly_call_limit: Optional[int] = None


class TenantUpdate(BaseModel):
    name: Optional[str] = None
    industry: Optional[str] = None
    billing_plan: Optional[str] = None
    config: Optional[dict] = None
    branding: Optional[dict] = None
    ai_monthly_call_limit: Optional[int] = None
    is_active: Optional[bool] = None


class TenantRead(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    is_active: bool
    industry: Optional[str]
    billing_plan: str
    config: dict
    branding: dict
    ai_monthly_call_limit: Optional[int]
    effective_ai_call_limit: Optional[int]

    class Config:
        from_attributes = True


def _to_read(t: Tenant) -> TenantRead:
    return TenantRead(
        id=t.id,
        name=t.name,
        slug=t.slug,
        is_active=t.is_active,
        industry=t.industry,
        billing_plan=t.billing_plan,
        config=t.config or {},
        branding=t.branding or {},
        ai_monthly_call_limit=t.ai_monthly_call_limit,
        effective_ai_call_limit=t.effective_ai_call_limit,
    )


# --- Admin CRUD ------------------------------------------------------------


@router.post("/tenants", response_model=TenantRead, status_code=201)
async def create_tenant(
    body: TenantCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """Provision a new tenant organization (admin only)."""
    slug = body.slug.lower().strip()
    if not _SLUG_RE.match(slug):
        raise HTTPException(400, "slug must be 2-63 chars: lowercase letters, digits, hyphens")
    if body.billing_plan not in BILLING_PLANS:
        raise HTTPException(400, f"billing_plan must be one of {BILLING_PLANS}")

    existing = (await db.execute(select(Tenant).where(Tenant.slug == slug))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"A tenant with slug {slug!r} already exists")

    tenant = Tenant(
        name=body.name,
        slug=slug,
        industry=body.industry,
        billing_plan=body.billing_plan,
        config=body.config or {},
        branding=body.branding or {},
        ai_monthly_call_limit=body.ai_monthly_call_limit,
    )
    db.add(tenant)
    await db.flush()
    return _to_read(tenant)


@router.get("/tenants", response_model=list[TenantRead])
async def list_tenants(
    active_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """List all tenants (admin only)."""
    query = select(Tenant).order_by(Tenant.created_at.desc())
    if active_only:
        query = query.where(Tenant.is_active.is_(True))
    rows = (await db.execute(query)).scalars().all()
    return [_to_read(t) for t in rows]


async def _get_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = (await db.execute(select(Tenant).where(Tenant.id == tenant_id))).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(404, "Tenant not found")
    return tenant


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
async def get_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    return _to_read(await _get_tenant_or_404(db, tenant_id))


@router.patch("/tenants/{tenant_id}", response_model=TenantRead)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """Update tenant config, branding, plan, or active state (admin only)."""
    tenant = await _get_tenant_or_404(db, tenant_id)
    if body.billing_plan is not None:
        if body.billing_plan not in BILLING_PLANS:
            raise HTTPException(400, f"billing_plan must be one of {BILLING_PLANS}")
        tenant.billing_plan = body.billing_plan
    if body.name is not None:
        tenant.name = body.name
    if body.industry is not None:
        tenant.industry = body.industry
    if body.config is not None:
        tenant.config = body.config
    if body.branding is not None:
        tenant.branding = body.branding
    if body.ai_monthly_call_limit is not None:
        tenant.ai_monthly_call_limit = body.ai_monthly_call_limit
    if body.is_active is not None:
        tenant.is_active = body.is_active
    await db.flush()
    return _to_read(tenant)


@router.delete("/tenants/{tenant_id}", status_code=200)
async def deactivate_tenant(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """Deactivate a tenant (soft — data is retained). Admin only."""
    tenant = await _get_tenant_or_404(db, tenant_id)
    tenant.is_active = False
    await db.flush()
    return {"status": "deactivated", "tenant_id": str(tenant_id)}


@router.get("/tenants/{tenant_id}/usage")
async def tenant_usage(
    tenant_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_role("admin")),
):
    """Current-period usage-based billing summary for a tenant (admin only)."""
    tenant = await _get_tenant_or_404(db, tenant_id)
    return await get_tenant_service().usage_summary(db, tenant)


# --- Current tenant (any authenticated user) -------------------------------


@router.get("/tenants/current/config")
async def current_tenant_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resolve the caller's tenant config + branding for the frontend.

    Returns a ``default`` marker for first-party users with no ``org_id`` so the
    UI falls back to Apprend's own branding.
    """
    ctx = await get_tenant_service().get_context(db, current_user.org_id)
    if ctx.is_default:
        return {"default": True, "branding": {}, "feature_flags": {}, "enabled_verticals": []}
    t = ctx.tenant
    return {
        "default": False,
        "tenant_id": str(t.id),
        "name": t.name,
        "slug": t.slug,
        "industry": t.industry,
        "billing_plan": t.billing_plan,
        "branding": t.branding or {},
        "feature_flags": (t.config or {}).get("feature_flags", {}),
        "enabled_verticals": t.enabled_verticals(),
    }


@router.get("/tenants/current/usage")
async def current_tenant_usage(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Usage summary for the caller's own tenant."""
    svc = get_tenant_service()
    tenant = await svc.get_tenant(db, current_user.org_id)
    if tenant is None:
        return {"default": True, "ai_calls": 0, "ai_call_limit": None}
    return await svc.usage_summary(db, tenant)
