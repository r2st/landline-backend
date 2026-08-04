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
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.integration import Integration, WebhookSubscription
from app.models.user import User
from app.services.crm_import import CRM_PULL_PROVIDERS, get_crm_importer
from app.services.crypto import TokenCryptoError, decrypt, encrypt
from app.services.integrations import get_connector, marketplace_catalog
from app.services.web_scraper import (
    UnresolvableHostError,
    UnsafeURLError,
    validate_public_url_async,
)
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
    # "When did this last actually pull anything?" is the first question asked
    # of a sync, and the column has always been there without being reported.
    last_synced_at: Optional[datetime] = None

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
        last_synced_at=i.last_synced_at,
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


class CrmImportRequest(BaseModel):
    """Bounds on one CRM pull. Every field has a safe default."""

    max_records: int = Field(
        500, ge=1, le=10_000,
        description="Stop after this many CRM rows. The response carries a "
                    "next_cursor so a large book imports in slices.",
    )
    page_size: int = Field(100, ge=1, le=200)
    cursor: Optional[str] = Field(
        None, description="Opaque cursor from a previous import's next_cursor."
    )
    dry_run: bool = Field(
        False,
        description="Fetch and resolve against existing records, write nothing. "
                    "Answers 'how much of this book do we already have?'",
    )


@router.post("/integrations/{integration_id}/import/contacts")
async def import_crm_contacts(
    integration_id: uuid.UUID,
    body: CrmImportRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull contacts from a CRM into Landline, deduplicated against what we have.

    Imported records are **persisted, never enrolled** — see
    ``app.services.crm_import``. Bringing your book in must not be the event
    that starts emailing it.
    """
    integration = await _get_integration_or_404(db, integration_id, current_user.org_id)
    if integration.provider not in CRM_PULL_PROVIDERS:
        raise HTTPException(
            400,
            f"{integration.provider!r} cannot pull contacts "
            f"(providers that can: {', '.join(CRM_PULL_PROVIDERS)})",
        )
    connector = get_connector(integration.provider)
    if connector is None:
        raise HTTPException(400, f"Unknown provider {integration.provider!r}")

    summary = await get_crm_importer().import_contacts(
        db,
        connector,
        integration.config,
        _decrypt_credentials(integration.credentials),
        provider=integration.provider,
        max_records=body.max_records,
        page_size=body.page_size,
        cursor=body.cursor,
        dry_run=body.dry_run,
    )
    if not body.dry_run:
        integration.last_synced_at = datetime.now(timezone.utc)
        integration.last_error = summary.errors[0] if summary.errors else None
        await db.flush()
    return summary.as_dict()


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
    # The scheme check alone admitted `http://169.254.169.254/…` and
    # `http://127.0.0.1:8001/…`, which this server then POSTs to — with a valid
    # HMAC signature over a body the same user supplies via
    # `POST /integrations/webhooks/emit`, whose response reports the target's
    # status code back to them. `validate_public_url` is the guard the codebase
    # already applies to org-supplied RSS feed URLs; rejecting here gives the
    # user a 400 they can act on, and `webhook_dispatch` re-checks at delivery
    # because DNS can be re-pointed after registration.
    try:
        await validate_public_url_async(body.target_url)
    except UnresolvableHostError:
        # Storing a URL is not fetching one. A host that doesn't resolve in this
        # second says nothing about whether it is private — a receiver whose DNS
        # is briefly down, or whose record is about to be created, is an ordinary
        # thing to configure — and the delivery path re-checks and fails closed.
        logger.info("Webhook target %r did not resolve at registration", body.target_url)
    except UnsafeURLError as exc:
        raise HTTPException(400, f"target_url is not a reachable public URL: {exc}")
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
