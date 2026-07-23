"""Email communication router — unified inbox, thread view, and reply sending."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import UserScope, get_scope
from app.models.gmail_account import GmailAccount
from app.models.smtp_account import SmtpAccount
from app.schemas.email_history import (
    BulkThreadSnoozeRequest,
    EmailInboxResponse,
    EmailLeadResponse,
    EmailReplyRequest,
    EmailReplyResponse,
    EmailThreadRead,
    InboxAnalyticsResponse,
    LabelCreateRequest,
    LabelRead,
    LabelUpdateRequest,
    SyncStatusResponse,
    ThreadArchiveResponse,
    ThreadIdsRequest,
    ThreadLabelToggleResponse,
    ThreadReadResponse,
    ThreadSnoozeRequest,
    ThreadSnoozeResponse,
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
    date_from: Optional[datetime] = Query(
        None, description="Only threads with activity on/after this timestamp"
    ),
    date_to: Optional[datetime] = Query(
        None, description="Only threads with activity on/before this timestamp"
    ),
    gmail_account_id: Optional[uuid.UUID] = Query(
        None, description="Filter to a single connected Gmail account by its id"
    ),
    account_email: Optional[str] = Query(
        None,
        description=(
            "Filter to a single connected email account by its address. Unlike "
            "gmail_account_id this also covers SMTP accounts, which carry no "
            "foreign-key link on the message rows and so are matched by address."
        ),
    ),
    label_id: Optional[uuid.UUID] = Query(
        None, description="Filter to threads the caller has tagged with this label"
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

    Pass ``gmail_account_id`` (Gmail only) or ``account_email`` (Gmail *or*
    SMTP) to filter to a single connected email account — e.g. only mail sent
    from / received on support@company.com. Each returned message includes
    ``gmail_account_id`` and ``gmail_account_email`` so the frontend can label
    and group mail by the email address it belongs to.
    """
    valid_filters = (
        "all", "sent", "received", "opened", "needs_reply", "priority",
        "archived", "snoozed",
    )
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

    # Same guard for the by-address filter, which spans Gmail *and* SMTP. When
    # scoped, the user's own sender-address set (already Gmail+SMTP) is the
    # allowlist; unscoped admins just need the address to exist somewhere.
    normalized_account_email = None
    if account_email is not None:
        normalized_account_email = account_email.strip().lower() or None
    if normalized_account_email is not None:
        if user_gmail_emails is not None:
            if normalized_account_email not in user_gmail_emails:
                raise HTTPException(status_code=404, detail="Email account not found")
        else:
            exists = (
                await db.execute(
                    select(GmailAccount.id)
                    .where(func.lower(GmailAccount.email) == normalized_account_email)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if exists is None:
                exists = (
                    await db.execute(
                        select(SmtpAccount.id)
                        .where(func.lower(SmtpAccount.email) == normalized_account_email)
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if exists is None:
                raise HTTPException(status_code=404, detail="Email account not found")

    service = _get_service()
    result = await service.get_inbox(
        db, page=page, per_page=per_page, filter_type=filter, search=search,
        user_gmail_emails=user_gmail_emails, gmail_account_id=gmail_account_id,
        account_email=normalized_account_email,
        date_from=date_from, date_to=date_to,
        # Archive/snooze/label state is per-user; owner_id is the acting human
        # (None for the legacy/unscoped admin path, which then sees every thread
        # unarchived, un-snoozed, and unlabeled).
        state_user_id=scope.owner_id,
        label_id=label_id,
    )
    return result


@router.get("/analytics", response_model=InboxAnalyticsResponse)
async def get_inbox_analytics(
    days: int = Query(30, ge=1, le=365, description="Trailing window in days"),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Mailbox analytics: volume, response time, unread / needs-reply counts,
    and reply-intent breakdown — scoped to the caller's own mailboxes."""
    user_gmail_emails = None
    if not scope.see_all:
        user_gmail_emails = await _sender_emails_for_user(db, scope.target_user_id)
        if not user_gmail_emails:
            return EmailHistoryService._empty_analytics(days)
    service = _get_service()
    return await service.get_inbox_analytics(
        db, user_gmail_emails=user_gmail_emails, days=days
    )


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


async def _scoped_sender_emails(
    db: AsyncSession, scope: UserScope
) -> Optional[set[str]]:
    """The caller's own sender addresses, or ``None`` for the unscoped admin.

    Mirrors the scoping used by ``get_inbox``: a scoped user may only touch mail
    on their own mailboxes; the legacy/unscoped admin path (``see_all``) applies
    no address filter.
    """
    if scope.see_all:
        return None
    return await _sender_emails_for_user(db, scope.target_user_id)


def _require_state_user(scope: UserScope) -> uuid.UUID:
    """The user id that per-thread archive state is keyed on.

    Archive/snooze/read-override are inherently per-user, so an unscoped
    (legacy API-key) caller with no resolvable user cannot own thread state.
    """
    user_id = scope.owner_id
    if user_id is None:
        raise HTTPException(
            status_code=400,
            detail="Archiving requires an authenticated user.",
        )
    return user_id


async def _set_read(
    db: AsyncSession, scope: UserScope, thread_ids: list[str], is_read: bool
) -> ThreadReadResponse:
    user_emails = await _scoped_sender_emails(db, scope)
    service = _get_service()
    updated = await service.set_threads_read(
        db, thread_ids, is_read=is_read, user_gmail_emails=user_emails
    )
    return ThreadReadResponse(updated=updated, is_read=is_read)


@router.post("/thread/{gmail_thread_id}/read", response_model=ThreadReadResponse)
async def mark_thread_read(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Mark every reply in a thread read (org/mailbox-scoped to the caller)."""
    return await _set_read(db, scope, [gmail_thread_id], True)


@router.post("/thread/{gmail_thread_id}/unread", response_model=ThreadReadResponse)
async def mark_thread_unread(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Mark every reply in a thread unread — the 'mark unread' affordance."""
    return await _set_read(db, scope, [gmail_thread_id], False)


@router.post("/threads/read", response_model=ThreadReadResponse)
async def bulk_mark_read(
    payload: ThreadIdsRequest,
    is_read: bool = Query(True, description="Set read (true) or unread (false)"),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Bulk mark-read (or unread) across a selection of threads."""
    return await _set_read(db, scope, payload.thread_ids, is_read)


@router.post("/thread/{gmail_thread_id}/archive", response_model=ThreadArchiveResponse)
async def archive_thread(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Archive a thread for the caller — drops it from the default inbox view."""
    user_id = _require_state_user(scope)
    service = _get_service()
    updated = await service.set_threads_archived(
        db, [gmail_thread_id], is_archived=True, user_id=user_id
    )
    return ThreadArchiveResponse(updated=updated, is_archived=True)


@router.post("/thread/{gmail_thread_id}/unarchive", response_model=ThreadArchiveResponse)
async def unarchive_thread(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Unarchive a thread for the caller — returns it to the default view."""
    user_id = _require_state_user(scope)
    service = _get_service()
    updated = await service.set_threads_archived(
        db, [gmail_thread_id], is_archived=False, user_id=user_id
    )
    return ThreadArchiveResponse(updated=updated, is_archived=False)


@router.post("/threads/archive", response_model=ThreadArchiveResponse)
async def bulk_archive(
    payload: ThreadIdsRequest,
    is_archived: bool = Query(True, description="Archive (true) or unarchive (false)"),
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Bulk archive (or unarchive) a selection of threads for the caller."""
    user_id = _require_state_user(scope)
    service = _get_service()
    updated = await service.set_threads_archived(
        db, payload.thread_ids, is_archived=is_archived, user_id=user_id
    )
    return ThreadArchiveResponse(updated=updated, is_archived=is_archived)


# ---------------------------------------------------------------------------
# Snooze (per-user; reuses inbox_thread_state.snoozed_until)
# ---------------------------------------------------------------------------


def _normalize_snooze_until(dt: Optional[datetime]) -> Optional[datetime]:
    """Coerce a snooze timestamp to aware UTC (naive is assumed UTC)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@router.post("/thread/{gmail_thread_id}/snooze", response_model=ThreadSnoozeResponse)
async def snooze_thread(
    gmail_thread_id: str,
    payload: ThreadSnoozeRequest,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Snooze a thread until ``snoozed_until`` — it drops out of the default
    inbox and reappears automatically once that time passes."""
    user_id = _require_state_user(scope)
    until = _normalize_snooze_until(payload.snoozed_until)
    service = _get_service()
    updated = await service.set_threads_snoozed(
        db, [gmail_thread_id], until, user_id=user_id
    )
    return ThreadSnoozeResponse(updated=updated, snoozed_until=until)


@router.post("/thread/{gmail_thread_id}/unsnooze", response_model=ThreadSnoozeResponse)
async def unsnooze_thread(
    gmail_thread_id: str,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Un-snooze a thread — returns it to the default view immediately."""
    user_id = _require_state_user(scope)
    service = _get_service()
    updated = await service.set_threads_snoozed(
        db, [gmail_thread_id], None, user_id=user_id
    )
    return ThreadSnoozeResponse(updated=updated, snoozed_until=None)


@router.post("/threads/snooze", response_model=ThreadSnoozeResponse)
async def bulk_snooze(
    payload: BulkThreadSnoozeRequest,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Snooze (or un-snooze, when ``snoozed_until`` is null) a selection."""
    user_id = _require_state_user(scope)
    until = _normalize_snooze_until(payload.snoozed_until)
    service = _get_service()
    updated = await service.set_threads_snoozed(
        db, payload.thread_ids, until, user_id=user_id
    )
    return ThreadSnoozeResponse(updated=updated, snoozed_until=until)


# ---------------------------------------------------------------------------
# Labels (per-user thread tags)
# ---------------------------------------------------------------------------


@router.get("/labels", response_model=list[LabelRead])
async def list_labels(
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """List the caller's inbox labels with per-label thread counts."""
    user_id = _require_state_user(scope)
    service = _get_service()
    return await service.list_labels(db, user_id)


@router.post("/labels", response_model=LabelRead, status_code=201)
async def create_label(
    payload: LabelCreateRequest,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Create a label for the caller. 409 on a duplicate name."""
    user_id = _require_state_user(scope)
    service = _get_service()
    label = await service.create_label(db, user_id, payload.name, payload.color)
    if label is None:
        raise HTTPException(status_code=409, detail="A label with that name already exists")
    return label


@router.patch("/labels/{label_id}", response_model=LabelRead)
async def update_label(
    label_id: uuid.UUID,
    payload: LabelUpdateRequest,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Rename / recolor one of the caller's labels."""
    user_id = _require_state_user(scope)
    service = _get_service()
    label = await service.update_label(
        db, user_id, label_id, name=payload.name, color=payload.color
    )
    if label is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return label


@router.delete("/labels/{label_id}", status_code=204)
async def delete_label(
    label_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Delete one of the caller's labels (its thread attachments cascade)."""
    user_id = _require_state_user(scope)
    service = _get_service()
    ok = await service.delete_label(db, user_id, label_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Label not found")
    return None


@router.post(
    "/thread/{gmail_thread_id}/labels/{label_id}",
    response_model=ThreadLabelToggleResponse,
)
async def add_thread_label(
    gmail_thread_id: str,
    label_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Attach one of the caller's labels to a thread (idempotent)."""
    user_id = _require_state_user(scope)
    service = _get_service()
    result = await service.set_thread_label(
        db, user_id, gmail_thread_id, label_id, attached=True
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return ThreadLabelToggleResponse(
        gmail_thread_id=gmail_thread_id, label_id=label_id, attached=True
    )


@router.delete(
    "/thread/{gmail_thread_id}/labels/{label_id}",
    response_model=ThreadLabelToggleResponse,
)
async def remove_thread_label(
    gmail_thread_id: str,
    label_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: UserScope = Depends(get_scope),
):
    """Detach one of the caller's labels from a thread (idempotent)."""
    user_id = _require_state_user(scope)
    service = _get_service()
    result = await service.set_thread_label(
        db, user_id, gmail_thread_id, label_id, attached=False
    )
    if result is None:
        raise HTTPException(status_code=404, detail="Label not found")
    return ThreadLabelToggleResponse(
        gmail_thread_id=gmail_thread_id, label_id=label_id, attached=False
    )


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
