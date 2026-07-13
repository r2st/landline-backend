"""Email communication router — unified inbox, thread view, and reply sending."""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
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


async def _sender_emails_for_user(db: AsyncSession, user_id: uuid.UUID) -> set[str]:
    """Lowercased set of a user's own Gmail + SMTP sender addresses."""
    gmail_res = await db.execute(
        select(GmailAccount.email).where(GmailAccount.user_id == user_id)
    )
    smtp_res = await db.execute(
        select(SmtpAccount.email).where(SmtpAccount.user_id == user_id)
    )
    return {e.lower() for e in gmail_res.scalars().all()} | {
        e.lower() for e in smtp_res.scalars().all()
    }


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
    scope: UserScope = Depends(get_scope),
):
    """Unified inbox with filtering and search.

    Filter values: all, sent, received, opened, needs_reply

    Scoped to the caller's own connected Gmail/SMTP accounts. Admins/managers
    may pass ``view_as_user_id`` to view another user's inbox, or
    ``view_as_user_id=all`` to see everyone's mail. An authenticated user with
    no connected accounts sees an empty inbox (fail closed).

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
    # Resolve which sender addresses this request is scoped to. ``see_all``
    # (API-key legacy, or an admin's "All Users" view) means no address filter.
    user_gmail_emails = None
    if not scope.see_all:
        user_gmail_emails = await _sender_emails_for_user(db, scope.target_user_id)
        if not user_gmail_emails:
            # Authenticated but no connected accounts → nothing to show.
            return {"threads": [], "total": 0, "page": page, "per_page": per_page}

    # When filtering by a specific account, make sure it exists — and, when
    # scoped, that it belongs to the user in view (no cross-user peeking).
    if gmail_account_id is not None:
        owner_query = select(GmailAccount.id).where(GmailAccount.id == gmail_account_id)
        if not scope.see_all:
            owner_query = owner_query.where(GmailAccount.user_id == scope.target_user_id)
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


def _serialize_conversation_analysis(a) -> dict:
    """Serialize a ConversationAnalysis row for the API."""
    return {
        "id": str(a.id),
        "gmail_thread_id": a.gmail_thread_id,
        "company_id": str(a.company_id) if a.company_id else None,
        "contact_id": str(a.contact_id) if a.contact_id else None,
        "momentum_score": a.momentum_score,
        "momentum_label": a.momentum_label,
        "stakeholder_map": a.stakeholder_map or [],
        "unresolved_objections": a.unresolved_objections or [],
        "recommended_action": a.recommended_action,
        "next_message": a.next_message or {},
        "signals": a.signals or {},
        "message_count": a.message_count,
        "model_used": a.model_used,
        "analyzed_at": a.analyzed_at.isoformat() if a.analyzed_at else None,
    }


@router.get("/thread/{gmail_thread_id}/intelligence")
async def get_thread_intelligence(
    gmail_thread_id: str,
    refresh: bool = Query(False, description="Re-run analysis even if a stored one exists"),
    db: AsyncSession = Depends(get_db),
):
    """Return Conversation Intelligence for a thread (feature 1.5).

    Returns the latest stored analysis. If none exists (or ``refresh=true``),
    runs analysis synchronously over the full thread and stores the result.
    Returns 404 if the thread has no messages to analyze.
    """
    from app.services.conversation_intelligence import get_conversation_intelligence_service

    service = get_conversation_intelligence_service()

    if not refresh:
        existing = await service.get_latest(db, gmail_thread_id)
        if existing is not None:
            return {"status": "ok", **_serialize_conversation_analysis(existing)}

    analysis = await service.analyze_thread(db, gmail_thread_id)
    if analysis is None:
        raise HTTPException(
            status_code=404,
            detail="Thread not found or could not be analyzed",
        )
    return {"status": "ok", **_serialize_conversation_analysis(analysis)}


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
    scope: UserScope = Depends(get_scope),
):
    """Trigger a manual sync of email replies from Gmail.

    Syncs the caller's own connected Gmail accounts. Admins/managers may pass
    ``view_as_user_id`` to sync another user's (or ``all``) accounts.
    """
    service = _get_service()
    user_id = None if scope.see_all else scope.target_user_id
    result = await service.sync_all_accounts(db, user_id=user_id)
    return {
        "synced": result.get("synced", 0),
        "errors": result.get("errors", 0),
        "last_sync": None,
    }
