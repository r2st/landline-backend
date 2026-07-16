"""Dialtone Priority Inbox — the operator-facing surface for classified replies
(Feature 1 — Reply Classification).

Lives under the ``/dialtone`` namespace alongside the activity feed. Endpoints:

* ``GET  /dialtone/inbox``                 — classified replies, filtered + paginated.
* ``GET  /dialtone/inbox/stats``           — per-category counts for header badges.
* ``GET  /dialtone/inbox/{reply_id}``      — full detail (+ lazy draft for hot leads).
* ``POST /dialtone/inbox/{reply_id}/reclassify`` — operator override + re-route.
* ``POST /dialtone/inbox/{reply_id}/read`` — mark read.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.contact import Contact
from app.models.email_reply import EmailReply
from app.models.email_template import EmailSend
from app.schemas.replies import (
    InboxList,
    InboxReply,
    InboxReplyDetail,
    InboxStats,
    ReclassifyRequest,
)
from app.services.ai_service import REPLY_CATEGORIES, get_ai_service
from app.tasks.replies import route_auto_action

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dialtone", tags=["dialtone-inbox"])

# Categories an operator may reclassify to (the 5 routing categories + unknown).
_RECLASSIFY_CATEGORIES = set(REPLY_CATEGORIES)


def _to_inbox_reply(reply: EmailReply, contact: Optional[Contact], company: Optional[Company]) -> InboxReply:
    return InboxReply(
        id=str(reply.id),
        reply_classification=reply.reply_classification,
        classification_confidence=reply.classification_confidence,
        priority_flag=bool(reply.priority_flag),
        handled_action=reply.handled_action,
        classified_at=reply.classified_at,
        is_read=bool(reply.is_read),
        from_email=reply.from_email,
        subject=reply.subject,
        snippet=reply.snippet,
        received_at=reply.received_at,
        contact_id=str(reply.contact_id) if reply.contact_id else None,
        contact_name=contact.full_name if contact is not None else None,
        company_id=str(reply.company_id) if reply.company_id else None,
        company_name=company.name if company is not None else None,
        classification_meta=reply.classification_meta or {},
    )


@router.get("/inbox", response_model=InboxList)
async def list_inbox(
    category: Optional[str] = Query(None, description="Filter by classification category"),
    priority_only: bool = Query(False, description="Only priority-flagged (hot) replies"),
    unread_only: bool = Query(False, description="Only unread replies"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Priority inbox: classified replies, newest-first, filtered + paginated."""
    if category is not None and category not in _RECLASSIFY_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{category}'. Valid: {sorted(_RECLASSIFY_CATEGORIES)}",
        )

    filters = []
    if category is not None:
        filters.append(EmailReply.reply_classification == category)
    if priority_only:
        filters.append(EmailReply.priority_flag.is_(True))
    if unread_only:
        filters.append(EmailReply.is_read.is_(False))

    count_q = select(func.count()).select_from(EmailReply)
    for f in filters:
        count_q = count_q.where(f)
    total = (await db.execute(count_q)).scalar() or 0

    q = (
        select(EmailReply, Contact, Company)
        .outerjoin(Contact, EmailReply.contact_id == Contact.id)
        .outerjoin(Company, EmailReply.company_id == Company.id)
        .order_by(EmailReply.received_at.desc())
    )
    for f in filters:
        q = q.where(f)
    q = q.offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(q)).all()
    items = [_to_inbox_reply(reply, contact, company) for reply, contact, company in rows]
    return InboxList(items=items, total=total, page=page, page_size=page_size)


@router.get("/inbox/stats", response_model=InboxStats)
async def inbox_stats(db: AsyncSession = Depends(get_db)):
    """Counts by category (+ priority / unread / unclassified) for the badges."""
    by_category: dict[str, int] = {c: 0 for c in REPLY_CATEGORIES}
    rows = (
        await db.execute(
            select(EmailReply.reply_classification, func.count())
            .group_by(EmailReply.reply_classification)
        )
    ).all()
    unclassified = 0
    total = 0
    for classification, count in rows:
        total += count
        if classification is None:
            unclassified += count
        elif classification in by_category:
            by_category[classification] = count
        else:
            by_category[classification] = by_category.get(classification, 0) + count

    priority = (
        await db.execute(
            select(func.count()).select_from(EmailReply).where(EmailReply.priority_flag.is_(True))
        )
    ).scalar() or 0
    unread = (
        await db.execute(
            select(func.count()).select_from(EmailReply).where(EmailReply.is_read.is_(False))
        )
    ).scalar() or 0

    return InboxStats(
        by_category=by_category,
        total=total,
        unclassified=unclassified,
        priority=priority,
        unread=unread,
    )


async def _load_reply_context(
    db: AsyncSession, reply_id: uuid.UUID
) -> tuple[EmailReply, Optional[Contact], Optional[Company]]:
    reply = (
        await db.execute(select(EmailReply).where(EmailReply.id == reply_id))
    ).scalar_one_or_none()
    if reply is None:
        raise HTTPException(status_code=404, detail="Reply not found")
    contact = None
    if reply.contact_id is not None:
        contact = (
            await db.execute(select(Contact).where(Contact.id == reply.contact_id))
        ).scalar_one_or_none()
    company = None
    if reply.company_id is not None:
        company = (
            await db.execute(select(Company).where(Company.id == reply.company_id))
        ).scalar_one_or_none()
    elif contact is not None:
        company = (
            await db.execute(select(Company).where(Company.id == contact.company_id))
        ).scalar_one_or_none()
    return reply, contact, company


@router.get("/inbox/{reply_id}", response_model=InboxReplyDetail)
async def get_inbox_reply(reply_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Full reply detail. For ``interested`` replies, lazily draft a suggested
    response via ``analyze_reply`` (expensive, so only on demand for hot leads)."""
    reply, contact, company = await _load_reply_context(db, reply_id)
    base = _to_inbox_reply(reply, contact, company)

    detail = InboxReplyDetail(
        **base.model_dump(),
        body_text=reply.body_text,
        body_html=reply.body_html,
        email_send_id=str(reply.email_send_id) if reply.email_send_id else None,
    )

    if reply.reply_classification == "interested":
        # Pull the original send (if linked) for drafting context.
        original_subject = reply.subject or ""
        original_body = ""
        if reply.email_send_id is not None:
            send = (
                await db.execute(select(EmailSend).where(EmailSend.id == reply.email_send_id))
            ).scalar_one_or_none()
            if send is not None:
                original_subject = send.subject or original_subject
                original_body = getattr(send, "body_html", "") or getattr(send, "body", "") or ""
        try:
            detail.suggested_draft = await get_ai_service().analyze_reply(
                reply_body=reply.body_text or reply.snippet or "",
                original_subject=original_subject,
                original_body=original_body,
                contact_name=contact.full_name if contact else "",
                company_name=company.name if company else "",
            )
        except Exception as exc:  # pragma: no cover - drafting is best-effort
            logger.info("suggested draft failed for reply %s: %s", reply.id, exc)

    return detail


@router.post("/inbox/{reply_id}/reclassify", response_model=InboxReplyDetail)
async def reclassify_reply(
    reply_id: uuid.UUID,
    payload: ReclassifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """Operator override: set a new category, record the correction (training
    signal), and re-run the auto-action for the new category."""
    category = payload.category.strip().lower()
    if category not in _RECLASSIFY_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{category}'. Valid: {sorted(_RECLASSIFY_CATEGORIES)}",
        )

    reply, contact, company = await _load_reply_context(db, reply_id)

    previous = reply.reply_classification
    # Record the operator correction as a training signal.
    overrides = list((reply.classification_meta or {}).get("manual_overrides", []))
    overrides.append({"from": previous, "to": category})
    reply.classification_meta = {
        **(reply.classification_meta or {}),
        "source": "operator",
        "manual_overrides": overrides,
    }
    reply.reply_classification = category
    reply.classification_confidence = 1.0
    if payload.priority_flag is not None:
        reply.priority_flag = payload.priority_flag

    # Re-run the auto-action for the corrected category. Build a minimal result
    # carrying the stored return_date/referral so OOO/wrong_person still route.
    meta = reply.classification_meta or {}
    result = {
        "return_date": meta.get("return_date"),
        "referral": meta.get("referral") or {"name": None, "email": None},
    }
    handled = await route_auto_action(
        db, reply, contact, company, category, result
    )
    reply.handled_action = handled

    await db.flush()
    reply, contact, company = await _load_reply_context(db, reply_id)
    base = _to_inbox_reply(reply, contact, company)
    return InboxReplyDetail(
        **base.model_dump(),
        body_text=reply.body_text,
        body_html=reply.body_html,
        email_send_id=str(reply.email_send_id) if reply.email_send_id else None,
    )


@router.post("/inbox/{reply_id}/read", response_model=InboxReply)
async def mark_reply_read(reply_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Mark a reply read."""
    reply, contact, company = await _load_reply_context(db, reply_id)
    reply.is_read = True
    await db.flush()
    return _to_inbox_reply(reply, contact, company)
