"""Integration marketplace API (feature 4.2).

Lists the marketplace catalog, lets a tenant configure/connect a provider,
test the connection, run a connector action, and manage outbound webhook
subscriptions.

Integrations and webhooks are scoped to the caller's tenant (``org_id``).
Secret credential fields are encrypted at rest with Fernet; they are never
returned in API responses (only a redacted list of which keys are set).
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.integration import Integration, WebhookSubscription
from app.models.user import User
from app.services.crypto import TokenCryptoError, decrypt, encrypt
from app.services.integrations import get_connector, marketplace_catalog
from app.services.webhook_dispatch import get_webhook_dispatcher

logger = logging.getLogger(__name__)
router = APIRouter(tags=["integrations"])


# --- Schemas ---------------------------------------------------------------


class IntegrationCreate(BaseModel):
    provider: str
    config: dict = Field(default_factory=dict)
    credentials: dict = Field(default_factory=dict)


class IntegrationUpdate(BaseModel):
    config: Optional[dict] = None
    credentials: Optional[dict] = None
    is_active: Optional[bool] = None


class IntegrationRead(BaseModel):
    id: uuid.UUID
    provider: str
    auth_type: str
    status: str
    config: dict
    configured_credentials: list[str]
    is_active: bool
    last_error: Optional[str] = None

    class Config:
        from_attributes = True


class ActionRequest(BaseModel):
    action: str
    params: dict = Field(default_factory=dict)


class WebhookCreate(BaseModel):
    target_url: str
    event_types: list[str] = Field(default_factory=list)
    secret: Optional[str] = None


class EmitRequest(BaseModel):
    event_type: str
    payload: dict = Field(default_factory=dict)


def _to_read(i: Integration) -> IntegrationRead:
    return IntegrationRead(
        id=i.id,
        provider=i.provider,
        auth_type=i.auth_type,
        status=i.status,
        config=i.config or {},
        configured_credentials=sorted((i.credentials or {}).keys()),
        is_active=i.is_active,
        last_error=i.last_error,
    )


def _encrypt_credentials(creds: dict) -> dict:
    return {k: encrypt(str(v)) for k, v in (creds or {}).items() if v is not None and v != ""}


def _decrypt_credentials(creds: dict) -> dict:
    out = {}
    for k, v in (creds or {}).items():
        try:
            out[k] = decrypt(v)
        except TokenCryptoError:
            logger.warning("Could not decrypt credential %r", k)
    return out


# --- Marketplace catalog ---------------------------------------------------


@router.get("/integrations/marketplace")
async def get_marketplace(_: User = Depends(get_current_user)):
    """List every available integration and how to configure it."""
    return {"integrations": marketplace_catalog()}


# --- Configured integrations ----------------------------------------------


@router.get("/integrations", response_model=list[IntegrationRead])
async def list_integrations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's tenant's configured integrations."""
    rows = (
        await db.execute(select(Integration).where(Integration.org_id == current_user.org_id))
    ).scalars().all()
    return [_to_read(i) for i in rows]


@router.post("/integrations", response_model=IntegrationRead, status_code=201)
async def create_integration(
    body: IntegrationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Configure/connect a provider for the caller's tenant."""
    connector = get_connector(body.provider)
    if connector is None:
        raise HTTPException(400, f"Unknown provider {body.provider!r}")

    existing = (
        await db.execute(
            select(Integration).where(
                Integration.org_id == current_user.org_id,
                Integration.provider == body.provider,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"{body.provider!r} is already configured for this tenant")

    integration = Integration(
        org_id=current_user.org_id,
        provider=body.provider,
        auth_type=connector.auth_type,
        config=body.config or {},
        credentials=_encrypt_credentials(body.credentials),
        created_by=current_user.id,
        status="configured",
    )
    db.add(integration)
    await db.flush()

    # Verify the connection immediately; reflect the result in status.
    result = await connector.test_connection(integration.config, _decrypt_credentials(integration.credentials))
    integration.status = "connected" if result.ok else "error"
    integration.last_error = None if result.ok else result.message
    await db.flush()
    return _to_read(integration)


async def _get_integration_or_404(
    db: AsyncSession, integration_id: uuid.UUID, org_id: Optional[uuid.UUID]
) -> Integration:
    integration = (
        await db.execute(select(Integration).where(Integration.id == integration_id))
    ).scalar_one_or_none()
    if integration is None or integration.org_id != org_id:
        raise HTTPException(404, "Integration not found")
    return integration


@router.get("/integrations/{integration_id}", response_model=IntegrationRead)
async def get_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return _to_read(await _get_integration_or_404(db, integration_id, current_user.org_id))


@router.patch("/integrations/{integration_id}", response_model=IntegrationRead)
async def update_integration(
    integration_id: uuid.UUID,
    body: IntegrationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update an integration's config, credentials, or active state."""
    integration = await _get_integration_or_404(db, integration_id, current_user.org_id)
    if body.config is not None:
        integration.config = body.config
    if body.credentials is not None:
        # Merge: only overwrite provided keys, keep the rest.
        merged = dict(integration.credentials or {})
        merged.update(_encrypt_credentials(body.credentials))
        integration.credentials = merged
    if body.is_active is not None:
        integration.is_active = body.is_active
    await db.flush()
    return _to_read(integration)


@router.delete("/integrations/{integration_id}", status_code=200)
async def delete_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Disconnect and remove an integration."""
    integration = await _get_integration_or_404(db, integration_id, current_user.org_id)
    await db.delete(integration)
    await db.flush()
    return {"status": "disconnected", "integration_id": str(integration_id)}


@router.post("/integrations/{integration_id}/test")
async def test_integration(
    integration_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-test an integration's stored credentials."""
    integration = await _get_integration_or_404(db, integration_id, current_user.org_id)
    connector = get_connector(integration.provider)
    if connector is None:
        raise HTTPException(400, f"Unknown provider {integration.provider!r}")
    result = await connector.test_connection(
        integration.config, _decrypt_credentials(integration.credentials)
    )
    integration.status = "connected" if result.ok else "error"
    integration.last_error = None if result.ok else result.message
    await db.flush()
    return {"ok": result.ok, "message": result.message, "data": result.data}


@router.post("/integrations/{integration_id}/actions")
async def run_action(
    integration_id: uuid.UUID,
    body: ActionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Execute a connector action against an integration."""
    integration = await _get_integration_or_404(db, integration_id, current_user.org_id)
    connector = get_connector(integration.provider)
    if connector is None:
        raise HTTPException(400, f"Unknown provider {integration.provider!r}")
    result = await connector.execute(
        body.action, body.params, integration.config, _decrypt_credentials(integration.credentials)
    )
    return {"ok": result.ok, "message": result.message, "data": result.data}


# --- Shared helper for workflow actions ------------------------------------


async def run_integration_action(
    db: AsyncSession,
    org_id: Optional[uuid.UUID],
    provider: str,
    action: str,
    params: dict,
) -> dict:
    """Run a connector action for a tenant's configured provider.

    Used by the workflow engine's ``slack_notify`` action. Returns a plain dict
    (never raises) so a workflow step degrades to a failure result.
    """
    connector = get_connector(provider)
    if connector is None:
        return {"ok": False, "message": f"Unknown provider {provider!r}"}
    if isinstance(org_id, str):
        try:
            org_id = uuid.UUID(org_id)
        except (ValueError, TypeError):
            org_id = None
    integration = (
        await db.execute(
            select(Integration).where(
                Integration.org_id == org_id,
                Integration.provider == provider,
                Integration.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        return {"ok": False, "message": f"{provider!r} is not configured for this tenant"}
    result = await connector.execute(action, params, integration.config, _decrypt_credentials(integration.credentials))
    return {"ok": result.ok, "message": result.message, "data": result.data}


# --- Webhook framework -----------------------------------------------------


@router.get("/integrations/webhooks/subscriptions")
async def list_webhooks(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the tenant's webhook subscriptions."""
    rows = (
        await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.org_id == current_user.org_id)
        )
    ).scalars().all()
    return [
        {
            "id": str(s.id),
            "target_url": s.target_url,
            "event_types": s.event_types,
            "is_active": s.is_active,
            "has_secret": bool(s.secret),
        }
        for s in rows
    ]


@router.post("/integrations/webhooks/subscriptions", status_code=201)
async def create_webhook(
    body: WebhookCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Register a webhook subscription for internal events."""
    if not body.target_url.startswith(("http://", "https://")):
        raise HTTPException(400, "target_url must be an http(s) URL")
    sub = WebhookSubscription(
        org_id=current_user.org_id,
        target_url=body.target_url,
        event_types=body.event_types or [],
        secret=encrypt(body.secret) if body.secret else None,
    )
    db.add(sub)
    await db.flush()
    return {"id": str(sub.id), "target_url": sub.target_url, "event_types": sub.event_types}


@router.delete("/integrations/webhooks/subscriptions/{subscription_id}", status_code=200)
async def delete_webhook(
    subscription_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sub = (
        await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == subscription_id))
    ).scalar_one_or_none()
    if sub is None or sub.org_id != current_user.org_id:
        raise HTTPException(404, "Webhook subscription not found")
    await db.delete(sub)
    await db.flush()
    return {"status": "deleted", "subscription_id": str(subscription_id)}


@router.post("/integrations/webhooks/emit")
async def emit_webhook_event(
    body: EmitRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Emit an internal event to the tenant's matching webhook subscriptions."""
    events = await get_webhook_dispatcher().emit(
        db, body.event_type, body.payload, current_user.org_id
    )
    return {
        "event_type": body.event_type,
        "deliveries": [
            {"id": str(e.id), "status": e.status, "response_code": e.response_code}
            for e in events
        ],
    }
