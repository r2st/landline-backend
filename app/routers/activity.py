"""Unified company activity / timeline feed.

Stitches together every recorded touchpoint for a company — emails sent and
their engagement (opens/clicks/replies), sequence outreach events, lead-score
changes, discovered news, logged interactions/notes, and newly added contacts —
into one chronological stream, newest first.
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.company import Company
from app.models.company_news import CompanyNews
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.models.lead_score import LeadScore
from app.models.outreach import OutreachEnrollment, OutreachEvent
from app.schemas.activity import ACTIVITY_TYPES, ActivityFeed, ActivityItem

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/companies", tags=["activity"])


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a possibly-naive datetime to aware UTC.

    SQLite (tests) returns naive datetimes, Postgres returns aware ones; the feed
    sorts across sources so they must be comparable.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _collect_activities(
    db: AsyncSession, company_id: uuid.UUID
) -> list[ActivityItem]:
    """Gather all activity items for a company across every source table."""
    items: list[ActivityItem] = []

    # --- Emails (and their engagement) sent to this company's contacts ---
    email_rows = await db.execute(
        select(EmailSend, Contact)
        .join(Contact, EmailSend.contact_id == Contact.id)
        .where(Contact.company_id == company_id)
    )
    for send, contact in email_rows.all():
        who = contact.full_name
        base_meta = {
            "email_send_id": str(send.id),
            "contact_id": str(contact.id),
            "contact_name": who,
            "to_email": send.to_email,
            "subject": send.subject,
            "status": send.status,
        }
        if send.sent_at:
            items.append(ActivityItem(
                id=f"email_sent:{send.id}",
                type="email_sent",
                timestamp=_aware(send.sent_at),
                title=f"Email sent to {who}",
                description=send.subject,
                metadata=base_meta,
            ))
        if send.opened_at:
            items.append(ActivityItem(
                id=f"email_opened:{send.id}",
                type="email_opened",
                timestamp=_aware(send.opened_at),
                title=f"{who} opened an email",
                description=send.subject,
                metadata=base_meta,
            ))
        if send.clicked_at:
            items.append(ActivityItem(
                id=f"email_clicked:{send.id}",
                type="email_clicked",
                timestamp=_aware(send.clicked_at),
                title=f"{who} clicked a link",
                description=send.subject,
                metadata=base_meta,
            ))
        if send.replied_at:
            items.append(ActivityItem(
                id=f"reply:{send.id}",
                type="reply",
                timestamp=_aware(send.replied_at),
                title=f"{who} replied",
                description=send.subject,
                metadata=base_meta,
            ))

    # --- Outreach sequence events ---
    event_rows = await db.execute(
        select(OutreachEvent, Contact)
        .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
        .join(Contact, OutreachEnrollment.contact_id == Contact.id)
        .where(Contact.company_id == company_id)
    )
    for event, contact in event_rows.all():
        items.append(ActivityItem(
            id=f"outreach_event:{event.id}",
            type="outreach_event",
            timestamp=_aware(event.occurred_at),
            title=f"Sequence step {event.step_number}: {event.event_type}",
            description=f"{contact.full_name} — {event.event_type}",
            metadata={
                "event_id": str(event.id),
                "enrollment_id": str(event.enrollment_id),
                "contact_id": str(contact.id),
                "contact_name": contact.full_name,
                "event_type": event.event_type,
                "step_number": event.step_number,
            },
        ))

    # --- Lead score changes ---
    score_rows = await db.execute(
        select(LeadScore).where(LeadScore.company_id == company_id)
    )
    for score in score_rows.scalars().all():
        items.append(ActivityItem(
            id=f"score_change:{score.id}",
            type="score_change",
            timestamp=_aware(score.scored_at),
            title=f"{score.score_type.replace('_', ' ').title()} score: {score.fit_score}",
            description=None,
            metadata={
                "score_id": str(score.id),
                "score_type": score.score_type,
                "fit_score": score.fit_score,
                "industry_score": score.industry_score,
                "size_score": score.size_score,
                "location_score": score.location_score,
                "engagement_score": score.engagement_score,
            },
        ))

    # --- News ---
    news_rows = await db.execute(
        select(CompanyNews).where(CompanyNews.company_id == company_id)
    )
    for news in news_rows.scalars().all():
        items.append(ActivityItem(
            id=f"news:{news.id}",
            type="news",
            timestamp=_aware(news.published_at or news.discovered_at),
            title=news.title,
            description=news.summary,
            metadata={
                "news_id": str(news.id),
                "url": news.url,
                "source": news.source,
                "category": news.category,
                "relevance_score": news.relevance_score,
                "is_actionable": news.is_actionable,
            },
        ))

    # --- Logged interactions / notes ---
    interaction_rows = await db.execute(
        select(Interaction).where(Interaction.company_id == company_id)
    )
    for inter in interaction_rows.scalars().all():
        items.append(ActivityItem(
            id=f"note:{inter.id}",
            type="note",
            timestamp=_aware(inter.occurred_at),
            title=inter.subject or inter.interaction_type.replace("_", " ").title(),
            description=inter.notes,
            metadata={
                "interaction_id": str(inter.id),
                "interaction_type": inter.interaction_type,
                "contact_id": str(inter.contact_id) if inter.contact_id else None,
            },
        ))

    # --- Contacts added ---
    contact_rows = await db.execute(
        select(Contact).where(Contact.company_id == company_id)
    )
    for contact in contact_rows.scalars().all():
        items.append(ActivityItem(
            id=f"contact_added:{contact.id}",
            type="contact_added",
            timestamp=_aware(contact.created_at),
            title=f"Contact added: {contact.full_name}",
            description=contact.title,
            metadata={
                "contact_id": str(contact.id),
                "contact_name": contact.full_name,
                "title": contact.title,
                "email": contact.email,
                "source": contact.source,
            },
        ))

    return items


@router.get("/{company_id}/activity", response_model=ActivityFeed)
async def get_company_activity(
    company_id: uuid.UUID,
    activity_type: Optional[str] = Query(
        None, description="Comma-separated activity types to include"
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Chronological (newest-first) activity feed for a company."""
    company = (
        await db.execute(select(Company.id).where(Company.id == company_id))
    ).scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")

    # Parse + validate the type filter.
    wanted: Optional[set[str]] = None
    if activity_type:
        wanted = {t.strip() for t in activity_type.split(",") if t.strip()}
        invalid = wanted - set(ACTIVITY_TYPES)
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown activity type(s): {sorted(invalid)}. "
                       f"Valid types: {list(ACTIVITY_TYPES)}",
            )

    items = await _collect_activities(db, company_id)
    if wanted is not None:
        items = [i for i in items if i.type in wanted]

    # Newest first.
    items.sort(key=lambda i: i.timestamp, reverse=True)

    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start:start + page_size]

    return ActivityFeed(
        company_id=company_id,
        items=page_items,
        total=total,
        page=page,
        page_size=page_size,
    )
