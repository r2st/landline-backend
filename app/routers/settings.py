"""User-facing settings — per-org LLM key management.

Mounted at ``/settings``. Lets org admins bring their own Gemini API key
so AI features use it instead of the platform default.
"""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import OrgScope, get_org_scope
from app.models.org_llm_key import OrgLlmKey
from app.models.user import UserRole
from app.services import crypto
from app.services.credential_store import mask

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/settings", tags=["settings"])


class LlmKeyUpdate(BaseModel):
    api_key: str = Field(..., min_length=1, max_length=500)
    # Only Gemini is read back (GET/DELETE here and ``services.org_gemini``); a
    # key stored under any other name would report "saved" and never be used.
    provider: Literal["gemini"] = "gemini"

    @field_validator("api_key")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        # Whitespace-only would be stored as "" — has_key=True here while
        # Discovery still sees no key.
        v = v.strip()
        if not v:
            raise ValueError("API key must not be blank")
        return v


class LlmKeyRead(BaseModel):
    provider: str
    key_hint: str
    has_key: bool


def _require_org_admin(scope: OrgScope) -> None:
    if scope.org_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No organization context",
        )
    scope.check_permission(UserRole.ORG_ADMIN.value)


@router.get("/llm-key", response_model=LlmKeyRead)
async def get_llm_key(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Check whether the org has a custom LLM key set."""
    _require_org_admin(scope)
    row = (
        await db.execute(
            select(OrgLlmKey).where(OrgLlmKey.provider == "gemini")
        )
    ).scalar_one_or_none()
    if row is None:
        return LlmKeyRead(provider="gemini", key_hint="", has_key=False)
    return LlmKeyRead(provider=row.provider, key_hint=row.key_hint, has_key=True)


@router.put("/llm-key", response_model=LlmKeyRead)
async def set_llm_key(
    body: LlmKeyUpdate,
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Set or replace the org's custom LLM API key."""
    _require_org_admin(scope)
    api_key = body.api_key

    row = (
        await db.execute(
            select(OrgLlmKey).where(OrgLlmKey.provider == body.provider)
        )
    ).scalar_one_or_none()

    if row is None:
        row = OrgLlmKey(provider=body.provider)
        db.add(row)

    row.key_encrypted = crypto.encrypt(api_key)
    row.key_hint = mask(api_key)
    row.updated_by = scope.user.email
    await db.flush()
    await db.refresh(row)

    logger.info(
        "Org %s LLM key (%s) updated by %s",
        scope.org_id, body.provider, scope.user.email,
    )
    return LlmKeyRead(provider=row.provider, key_hint=row.key_hint, has_key=True)


@router.delete("/llm-key", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_key(
    scope: OrgScope = Depends(get_org_scope),
    db: AsyncSession = Depends(get_db),
):
    """Remove the org's custom LLM key, reverting to the platform default."""
    _require_org_admin(scope)
    row = (
        await db.execute(
            select(OrgLlmKey).where(OrgLlmKey.provider == "gemini")
        )
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.flush()
        logger.info("Org %s LLM key removed by %s", scope.org_id, scope.user.email)
