"""GDPR data-subject endpoints: Subject Access Request export + right-to-erasure.

Admin-only. Both operations identify the subject by email address:

* ``GET  /gdpr/export?email=…``  — download everything Landline holds about the
  subject (a Subject Access Request), as JSON.
* ``POST /gdpr/erase``           — soft-delete: scrub the subject's PII in place
  across every table and block the address from future contact. Irreversible, so
  the body must carry ``confirm: true``.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import require_role
from app.models.user import User
from app.services import gdpr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/gdpr", tags=["gdpr"])


class EraseRequest(BaseModel):
    email: EmailStr
    # Erasure is irreversible — require an explicit acknowledgement.
    confirm: bool = False


@router.get("/export")
async def export_data_subject(
    email: EmailStr = Query(..., description="The data subject's email address"),
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
):
    """Subject Access Request: every record held about ``email``."""
    data = await gdpr.export_subject(db, str(email))
    if not any(data["record_counts"].values()):
        raise HTTPException(status_code=404, detail=f"No data found for {email}")
    return data


@router.post("/erase")
async def erase_data_subject(
    payload: EraseRequest,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_role("admin")),
):
    """Right-to-erasure: scrub all PII for ``email`` across every table.

    Requires ``confirm: true`` — this cannot be undone.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail="Erasure is irreversible. Set confirm=true to proceed.",
        )
    result = await gdpr.erase_subject(db, str(payload.email))
    return result
