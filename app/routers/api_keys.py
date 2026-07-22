"""API key management — org-admin CRUD for public-API credentials.

Mounted under the API prefix at ``/org/api-keys``. An ``org_admin`` (or higher)
mints, lists, and revokes the keys their org uses to call the public API. The
org is always the caller's own (resolved from the JWT via :class:`OrgScope`), so
one org can never see or touch another's keys — reinforced by the global org
read filter, which scopes every ``ApiKey`` query to the caller's org.

Routes:
    GET    /org/api-keys/scopes        — the catalog of grantable scopes
    GET    /org/api-keys               — list the org's keys (no secrets)
    POST   /org/api-keys               — mint a key (returns the secret ONCE)
    DELETE /org/api-keys/{id}          — revoke a key (soft, immediate)

Reads and writes alike require ``org_admin`` — an API key is a tenant-wide
credential, so managing them is an administrative act, not a per-agent one.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.api_key import ApiKey
from app.models.user import UserRole
from app.schemas.api_key import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyRead,
    AvailableScopesResponse,
)
from app.services.api_key_auth import generate_api_key

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/org/api-keys", tags=["api-keys"])


def _require_org_admin(scope: OrgScope) -> None:
    """Gate on org_admin and reject platform admins (they have no org of their own).

    Platform admins carry ``org_id = NULL``, so a key they minted couldn't be
    stamped with a real org — they manage tenants via ``/platform`` instead.
    """
    if scope.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization context — platform admins don't own API keys",
        )
    scope.check_permission(UserRole.ORG_ADMIN.value)


@router.get("/scopes", response_model=AvailableScopesResponse)
async def list_scopes(scope: OrgScope = Depends(get_org_scope)):
    """Return the catalog of scopes a key may be granted."""
    _require_org_admin(scope)
    return AvailableScopesResponse()


@router.get("", response_model=list[ApiKeyRead])
async def list_api_keys(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's org's API keys, newest first (secrets never included)."""
    _require_org_admin(scope)
    rows = (
        await db.execute(select(ApiKey).order_by(ApiKey.created_at.desc()))
    ).scalars().all()
    return list(rows)


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    body: ApiKeyCreate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Mint a new API key.

    The raw secret is returned exactly once, in ``api_key`` — it is only ever
    stored hashed, so it cannot be retrieved again. ``org_id`` is stamped
    automatically from the caller's org context.
    """
    _require_org_admin(scope)

    if body.expires_at is not None:
        exp = body.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="expires_at must be in the future",
            )

    raw_key, key_hash, key_prefix = generate_api_key()
    api_key = ApiKey(
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=body.scopes,
        rate_limit=body.rate_limit,
        expires_at=body.expires_at,
        created_by=scope.user.id,
        # org_id is stamped by the OrgScopedMixin before_flush hook from context.
    )
    db.add(api_key)
    await db.flush()
    await db.refresh(api_key)
    logger.info(
        "API key %s (%s) created for org %s by %s",
        api_key.id, api_key.name, scope.org_id, scope.user.email,
    )

    out = ApiKeyRead.model_validate(api_key, from_attributes=True).model_dump()
    return ApiKeyCreateResponse(**out, api_key=raw_key)


@router.delete("/{key_id}", response_model=ApiKeyRead)
async def revoke_api_key(
    key_id: uuid.UUID,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a key (soft, immediate). It can never authenticate again.

    The row is retained (with ``revoked_at`` set) for audit rather than deleted.
    Revoking an already-revoked key is idempotent. The lookup is org-scoped, so
    another org's key id resolves to 404 rather than leaking its existence.
    """
    _require_org_admin(scope)
    api_key = (
        await db.execute(select(ApiKey).where(ApiKey.id == key_id))
    ).scalar_one_or_none()
    if api_key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        await db.refresh(api_key)
        logger.info("API key %s revoked by %s", api_key.id, scope.user.email)
    return api_key
