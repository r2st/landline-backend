"""Email communication router — unified inbox, thread view, and reply sending."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user_optional
from app.models.gmail_account import GmailAccount
from app.models.user import User
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


async def _user_gmail_ids(db: AsyncSession, user: Optional[User]) -> Optional[set[uuid.UUID]]:
    """Return the set of GmailAccount IDs owned by the user, or None if no user."""
    if user is None:
        return None
    result = await db.execute(
        select(GmailAccount.id).where(GmailAccount.user_id == user.id)
    )
    return {row[0] for row in result.all()}


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
    gmail_account_id: Optional[uuid.UUID] = Query(
        None, description="Filter to a single connected email account by its id"
    ),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Unified inbox with filtering and search.

    Filter values: all, sent, received, opened, needs_reply

    When called with a JWT Bearer token, scopes the inbox to the
    current user's connected Gmail accounts.

    Pass ``gmail_account_id`` to filter to a single connected email account
    (e.g. only mail sent from / received on support@company.com). Each returned
    message includes ``gmail_account_id`` and ``gmail_account_email`` so the
    frontend can label and group mail by the email address it belongs to.
    """
    valid_filters = ("all", "sent", "received", "opened", "needs_reply")
    if filter not in valid_filters:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid filter. Must be one of: {', '.join(valid_filters)}",
        )
    # Collect user's gmail account emails for scoping
    user_gmail_emails = None
    if current_user is not None:
        result = await db.execute(
            select(GmailAccount.email).where(GmailAccount.user_id == current_user.id)
        )
        emails = list(result.scalars().all())
        if emails:
            user_gmail_emails = {e.lower() for e in emails}

    # When filtering by a specific account, make sure it exists — and, when
    # authenticated, that it belongs to the current user (no cross-user peeking).
    if gmail_account_id is not None:
        owner_query = select(GmailAccount.id).where(GmailAccount.id == gmail_account_id)
        if current_user is not None:
            owner_query = owner_query.where(GmailAccount.user_id == current_user.id)
        account_exists = (await db.execute(owner_query)).scalar_one_or_none()
        if account_exists is None:
            raise HTTPException(status_code=404, detail="Email account not found")

    service = _get_service()
    result = await service.get_inbox(
        db, page=page, per_page=per_page, filter_type=filter, search=search,
        user_gmail_emails=user_gmail_emails, gmail_account_id=gmail_account_id,
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
async def trigger_sync(
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """Trigger a manual sync of email replies from Gmail.

    When called with a JWT Bearer token, syncs only the current user's
    connected Gmail accounts.
    """
    service = _get_service()
    user_id = current_user.id if current_user else None
    result = await service.sync_all_accounts(db, user_id=user_id)
    return {
        "synced": result.get("synced", 0),
        "errors": result.get("errors", 0),
        "last_sync": None,
    }
