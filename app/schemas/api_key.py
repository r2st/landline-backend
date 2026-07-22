"""Pydantic schemas for public-API key management (``/org/api-keys``).

These back the org-admin routes that mint, list, and revoke API keys. The raw
secret appears in exactly one place — :class:`ApiKeyCreateResponse`, returned
once at creation — and never in any subsequent read.
"""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.api_key import ALL_SCOPES, DEFAULT_RATE_LIMIT


class ApiKeyCreate(BaseModel):
    """Request to mint a new API key."""

    name: str = Field(..., min_length=1, max_length=120)
    scopes: list[str] = Field(
        default_factory=list,
        description=f"Subset of: {', '.join(ALL_SCOPES)}",
    )
    # Per-key requests-per-minute budget.
    rate_limit: int = Field(DEFAULT_RATE_LIMIT, ge=1, le=100_000)
    # Optional hard expiry. Omit for a non-expiring key.
    expires_at: Optional[datetime] = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: list[str]) -> list[str]:
        invalid = [s for s in v if s not in ALL_SCOPES]
        if invalid:
            raise ValueError(
                f"invalid scope(s): {', '.join(invalid)}. Valid: {', '.join(ALL_SCOPES)}"
            )
        # De-dup while preserving order.
        return list(dict.fromkeys(v))


class ApiKeyRead(BaseModel):
    """An API key as shown in lists — never includes the secret."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str] = Field(default_factory=list)
    rate_limit: int
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_by: Optional[uuid.UUID] = None
    created_at: datetime


class ApiKeyCreateResponse(ApiKeyRead):
    """The response to a create — the ONLY place the raw key is ever returned.

    ``api_key`` is the full secret; store it now, it cannot be retrieved again.
    """

    api_key: str


class AvailableScopesResponse(BaseModel):
    """The catalog of grantable scopes, for building a key-creation UI."""

    scopes: list[str] = Field(default_factory=lambda: list(ALL_SCOPES))
