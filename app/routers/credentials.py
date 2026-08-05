"""Platform credential management — view and rotate API keys from the dashboard.

Rotating the Google OAuth client used to mean SSH onto the box, edit
``.env.production``, restart the stack. This is that, as three endpoints:

    GET    /platform/credentials         — every managed key, masked
    PUT    /platform/credentials/{key}   — set an override
    DELETE /platform/credentials/{key}   — drop it, restoring the .env value

Three properties hold this together, and each is here because its absence would
be a hole rather than an inconvenience:

- **``platform_admin`` only.** These credentials identify the *deployment* to
  Google and to the LLM providers, not a tenant to us. An org admin who could
  write ``google_client_id`` could point the consent flow at a client they
  control and collect mailbox grants; one who could read the provider keys would
  be reading a shared bill. The mount carries the role check *and* every handler
  asserts it, matching :mod:`app.routers.platform` — the guard should be visible
  at the call site and never depend solely on where the router is mounted.
- **Secrets are never returned.** Not on read, not in the response to the write
  that set them, not in a log line. What comes back is a mask plus provenance
  (``database`` / ``environment`` / ``unset``), which is what an operator
  actually needs: *which* project is live, and whether it came from the UI or
  the deploy. Client *ids* are returned in full — they are in the consent URL
  every user sees, and hiding them would defeat the point of the screen.
- **The key must be in the registry.** ``MANAGED_CREDENTIALS`` is an allowlist;
  without it this is a write primitive over the whole ``Settings`` object.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.app_credential import AppCredential
from app.services import credential_store
from app.services.credential_store import BY_KEY, MANAGED_CREDENTIALS

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/platform/credentials", tags=["platform"])


class CredentialOut(BaseModel):
    key: str
    label: str
    category: str
    is_secret: bool
    help: str
    #: "database" (an override is set), "environment" (deployed value in use),
    #: or "unset" (neither — the feature depending on it is dark).
    source: str
    #: Masked for secrets, full for non-secrets, "" when unset.
    value: str
    has_env_fallback: bool
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class CredentialListOut(BaseModel):
    credentials: list[CredentialOut]
    #: When *this worker* last applied database values. Overrides propagate to
    #: other workers on their next refresh, so a stale timestamp here is the
    #: honest answer to "has my change taken effect everywhere yet?".
    applied_at: Optional[datetime] = None


class CredentialIn(BaseModel):
    value: str = Field(min_length=1, max_length=4096)


def _describe(
    cred, override_rows: dict[str, AppCredential], overrides: dict[str, str]
) -> CredentialOut:
    row = override_rows.get(cred.key)
    env = credential_store.env_value(cred.key)
    stored = overrides.get(cred.key, "")

    if stored:
        source, effective = "database", stored
    elif env:
        source, effective = "environment", env
    else:
        source, effective = "unset", ""

    return CredentialOut(
        key=cred.key,
        label=cred.label,
        category=cred.category,
        is_secret=cred.secret,
        help=cred.help,
        source=source,
        value=credential_store.mask(effective) if cred.secret else effective,
        has_env_fallback=bool(env),
        updated_at=row.updated_at if row else None,
        updated_by=row.updated_by if row else None,
    )


async def _rows_by_key(db: AsyncSession) -> dict[str, AppCredential]:
    rows = (await db.execute(select(AppCredential))).scalars().all()
    return {r.key: r for r in rows}


@router.get("", response_model=CredentialListOut)
async def list_credentials(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Every managed credential, with where its current value came from."""
    scope.check_permission("platform_admin")
    overrides = await credential_store.load_overrides(db)
    rows = await _rows_by_key(db)
    return CredentialListOut(
        credentials=[_describe(c, rows, overrides) for c in MANAGED_CREDENTIALS],
        applied_at=credential_store.last_applied(),
    )


@router.put("/{key}", response_model=CredentialOut)
async def set_credential(
    key: str,
    payload: CredentialIn,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Store an override for *key* and apply it to this worker immediately."""
    scope.check_permission("platform_admin")
    if key not in BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown credential '{key}'")
    try:
        await credential_store.set_credential(
            db, key, payload.value, updated_by=scope.user.email
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    overrides = await credential_store.load_overrides(db)
    rows = await _rows_by_key(db)
    return _describe(BY_KEY[key], rows, overrides)


@router.delete("/{key}", response_model=CredentialOut)
async def clear_credential(
    key: str,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Drop the override, restoring whatever the environment deployed."""
    scope.check_permission("platform_admin")
    if key not in BY_KEY:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown credential '{key}'")
    await credential_store.clear_credential(db, key, cleared_by=scope.user.email)

    overrides = await credential_store.load_overrides(db)
    rows = await _rows_by_key(db)
    return _describe(BY_KEY[key], rows, overrides)
