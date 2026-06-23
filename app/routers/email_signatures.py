"""Email signature management endpoints.

Powers the Outreach > Email Signatures experience: managing reusable email
signatures (one marked default) that are appended to outreach emails.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.email_signature import EmailSignature
from app.schemas.email_signature import (
    EmailSignatureCreate,
    EmailSignatureRead,
    EmailSignatureUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["email"])


# --- Helpers ---

async def _get_signature_or_404(db: AsyncSession, signature_id: uuid.UUID) -> EmailSignature:
    result = await db.execute(select(EmailSignature).where(EmailSignature.id == signature_id))
    signature = result.scalar_one_or_none()
    if not signature:
        raise HTTPException(status_code=404, detail="Email signature not found")
    return signature


async def _get_default_signature(db: AsyncSession) -> Optional[EmailSignature]:
    result = await db.execute(
        select(EmailSignature).where(EmailSignature.is_default == True)  # noqa: E712
    )
    return result.scalar_one_or_none()


async def _clear_default(db: AsyncSession, except_id: Optional[uuid.UUID] = None) -> None:
    """Unset is_default on all signatures (optionally except one), to keep a single default."""
    stmt = update(EmailSignature).where(EmailSignature.is_default == True).values(is_default=False)  # noqa: E712
    if except_id is not None:
        stmt = stmt.where(EmailSignature.id != except_id)
    await db.execute(stmt)
    await db.flush()


# --- Signature CRUD ---

@router.get("/email-signatures", response_model=list[EmailSignatureRead])
async def list_email_signatures(db: AsyncSession = Depends(get_db)):
    """List all email signatures (default first, then newest)."""
    result = await db.execute(
        select(EmailSignature).order_by(
            EmailSignature.is_default.desc(), EmailSignature.created_at.desc()
        )
    )
    return list(result.scalars().all())


@router.post("/email-signatures", response_model=EmailSignatureRead, status_code=201)
async def create_email_signature(
    payload: EmailSignatureCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new email signature. If marked default, any prior default is cleared.

    If no signature is currently marked default, the first one created becomes default.
    """
    make_default = payload.is_default
    if not make_default and await _get_default_signature(db) is None:
        make_default = True

    if make_default:
        await _clear_default(db)

    signature = EmailSignature(
        name=payload.name,
        content_html=payload.content_html,
        is_default=make_default,
    )
    db.add(signature)
    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A signature with that name already exists")
    await db.refresh(signature)
    logger.info("Created email signature '%s' (default=%s)", signature.name, signature.is_default)
    return signature


@router.put("/email-signatures/{signature_id}", response_model=EmailSignatureRead)
async def update_email_signature(
    signature_id: uuid.UUID,
    payload: EmailSignatureUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update an email signature's fields."""
    signature = await _get_signature_or_404(db, signature_id)
    update_data = payload.model_dump(exclude_unset=True)

    # Handle default toggling carefully to preserve the single-default invariant.
    if update_data.get("is_default") is True:
        await _clear_default(db, except_id=signature_id)
    elif update_data.get("is_default") is False and signature.is_default:
        raise HTTPException(
            status_code=400,
            detail="Cannot unset the default directly -- mark another signature as default instead",
        )

    for field, value in update_data.items():
        setattr(signature, field, value)

    try:
        await db.flush()
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="A signature with that name already exists")
    await db.refresh(signature)
    logger.info("Updated email signature %s", signature_id)
    return signature


@router.delete("/email-signatures/{signature_id}", status_code=204)
async def delete_email_signature(
    signature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Delete an email signature. The default signature cannot be deleted."""
    signature = await _get_signature_or_404(db, signature_id)
    if signature.is_default:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the default signature -- set another signature as default first",
        )
    await db.delete(signature)
    await db.flush()
    logger.info("Deleted email signature %s", signature_id)


@router.post("/email-signatures/{signature_id}/set-default", response_model=EmailSignatureRead)
async def set_default_signature(
    signature_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    """Mark a signature as the default, clearing the flag on any other signature."""
    signature = await _get_signature_or_404(db, signature_id)
    await _clear_default(db, except_id=signature_id)
    signature.is_default = True
    await db.flush()
    await db.refresh(signature)
    logger.info("Set email signature %s as default", signature_id)
    return signature
