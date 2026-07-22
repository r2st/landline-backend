"""Smart-reply endpoints for the Dialtone inbox.

Two surfaces the ``smart_reply`` service previously had no HTTP entry point for:

* ``POST /dialtone/smart-reply/process`` — run the smart-reply sweep on demand
  (read inbound replies across connected accounts, classify, draft, and
  auto-send where enabled). Normally driven by Celery; this lets an operator
  trigger it manually.
* ``POST /dialtone/inbox/{reply_id}/approve-send`` — dispatch an operator-approved
  (and possibly edited) suggested draft back to the person who replied, in-thread.
  This is what turns the inbox's ``suggested_draft`` into a real outbound reply.
"""

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.models.user import User
from app.schemas.replies import ApproveSendRequest, ApproveSendResult
from app.services import email_dispatch
from app.services.smart_reply import get_smart_reply_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dialtone", tags=["dialtone-smart-reply"])


@router.post("/smart-reply/process")
async def process_smart_replies(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Run the smart-reply sweep now: read replies across connected accounts,
    classify with AI, draft responses, and auto-send where enabled.

    Returns a per-reply summary so the operator can see what was processed.

    Requires authentication: ``get_current_user`` binds the caller's org as the
    request context, so the sweep (connected-account lookup, enrollment/contact
    matching) is filtered to the caller's own organization by the global
    read filter — never every tenant's mailboxes at once.
    """
    service = get_smart_reply_service()
    results = await service.process_replies(db)
    auto_sent = sum(1 for r in results if r.get("auto_sent"))
    return {
        "processed": len(results),
        "auto_sent": auto_sent,
        "items": results,
    }


@router.post("/inbox/{reply_id}/approve-send", response_model=ApproveSendResult)
async def approve_and_send(
    reply_id: uuid.UUID,
    payload: ApproveSendRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send an operator-approved reply to the person who replied, in-thread.

    Routes through ``email_dispatch.send_outreach_email`` — the single choke
    point that enforces the global kill switch and the do-not-contact suppression
    list — so an approved reply can still be blocked by an active pause. Threads
    under the original conversation when we have its Gmail thread/message ids.
    On a successful send an ``EmailSend`` row is recorded and the reply is marked
    read + handled.

    Tenant isolation: the reply must belong to the caller's organization.
    ``get_current_user`` binds the org context (so the global read filter also
    applies), and we additionally filter ``EmailReply.org_id`` explicitly — a
    reply id from another tenant returns 404, never someone else's inbox.
    """
    reply_query = select(EmailReply).where(EmailReply.id == reply_id)
    # Belt-and-suspenders alongside the global org filter: an explicit predicate
    # so a cross-tenant reply id is a 404 even for a platform admin (org_id=None,
    # which the global filter deliberately does not scope).
    if current_user.org_id is not None:
        reply_query = reply_query.where(EmailReply.org_id == current_user.org_id)
    reply = (await db.execute(reply_query)).scalar_one_or_none()
    if reply is None:
        raise HTTPException(status_code=404, detail="Reply not found")

    to_email = (reply.from_email or "").strip()
    if not to_email:
        raise HTTPException(status_code=400, detail="Reply has no sender address to respond to")

    # Original send carries the best threading context + the sending identity.
    original: EmailSend | None = None
    if reply.email_send_id is not None:
        original = (
            await db.execute(select(EmailSend).where(EmailSend.id == reply.email_send_id))
        ).scalar_one_or_none()

    subject = payload.subject or f"Re: {reply.subject or ''}".strip()
    thread_id = reply.gmail_thread_id or (original.gmail_thread_id if original else None)
    # Thread our reply under the message we're responding to.
    in_reply_to = reply.gmail_message_id or (
        original.provider_message_id if original else None
    )
    references = original.provider_message_id if original else None
    sender_account_id = original.sender_account_id if original else reply.gmail_account_id

    body_html = payload.body_html or payload.body

    result = await email_dispatch.send_outreach_email(
        db,
        to_email=to_email,
        subject=subject,
        body_html=body_html,
        sender_account_id=sender_account_id,
        # A human-approved 1:1 reply — no marketing unsubscribe footer.
        include_unsubscribe=False,
        thread_id=thread_id,
        in_reply_to=in_reply_to,
        references=references,
    )

    if result.get("status") == "sent":
        db.add(EmailSend(
            id=uuid.uuid4(),
            to_email=to_email,
            subject=subject,
            status="sent",
            sent_at=datetime.now(timezone.utc),
            contact_id=reply.contact_id,
            from_email=result.get("from_email") or result.get("sender"),
            sender_account_id=result.get("sender_account_id") or sender_account_id,
        ))
        reply.is_read = True
        reply.handled_action = "approved_reply_sent"
        await db.commit()

    return ApproveSendResult(
        status=result.get("status", "error"),
        detail=result.get("reason") or result.get("detail"),
        to_email=to_email,
        transport=result.get("transport"),
        sender=result.get("sender"),
        reply_id=str(reply.id),
    )
