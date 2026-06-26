"""Email communication router — unified inbox, thread view, and reply sending."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.email_history import (
    EmailInboxResponse,
    EmailLeadResponse,
    EmailReplyRequest,
    EmailReplyResponse,
    EmailThreadRead,
    SyncStatusResponse,
)
from app.services.email_history import EmailHistoryService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/emails", tags=["emails"])


def _get_service() -> EmailHistoryService:
    return EmailHistoryService()


@router.get("/lead/{company_id}", response_model=EmailLeadResponse)
async def get_lead_emails(
    company_id: uuid.UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Get all email threads for a company (lead)."""
    service = _get_service()
    result = await service.get_lead_emails(db, company_id, page=page, per_page=per_page)
    return result


@router.get("/inbox", response_model=EmailInboxResponse)
async def get_inbox(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    filter: str = Query("all", alias="filter"),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Unified inbox with filtering and search.

    Filter values: all, sent, received, opened, needs_reply
    """
    valid_filters = ("all", "sent", "received", "opened", "needs_reply")
    if filter not in valid_filters:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid filter. Must be one of: {', '.join(valid_filters)}",
        )
    service = _get_service()
    result = await service.get_inbox(
        db, page=page, per_page=per_page, filter_type=filter, search=search
    )
    return result


@router.get("/thread/{gmail_thread_id}", response_model=EmailThreadRead)
async def get_thread(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get full thread detail — all messages in a conversation."""
    service = _get_service()
    result = await service.get_thread(db, gmail_thread_id)
    if not result.get("messages"):
        raise HTTPException(status_code=404, detail="Thread not found")
    return result


@router.post("/reply/{email_id}", response_model=EmailReplyResponse)
async def send_reply(
    email_id: uuid.UUID,
    payload: EmailReplyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a reply in the same Gmail thread."""
    service = _get_service()
    result = await service.send_reply(
        db, email_id, body_html=payload.body_html, body_text=payload.body_text
    )
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result.get("detail", "Failed to send reply"))
    return result


@router.post("/sync", response_model=SyncStatusResponse)
async def trigger_sync(db: AsyncSession = Depends(get_db)):
    """Trigger a manual sync of email replies from Gmail."""
    service = _get_service()
    result = await service.sync_all_accounts(db)
    return {
        "synced": result.get("synced", 0),
        "errors": result.get("errors", 0),
        "last_sync": None,
    }
