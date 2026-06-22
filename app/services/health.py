"""Client health scoring.

Derives a 0-100 health score (and a healthy / at_risk / churned status) for a
company from its activity signals: how recently it was contacted, how
responsive it is to email, and how often it meets with us. Shared by the
``/companies/{id}/health`` endpoint and the ``/clients/overview`` rollup.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company import Company
from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.interaction import Interaction
from app.schemas.health import (
    ClientHealth,
    HealthComponents,
    HealthSignals,
)

# Component weights (sum to 100).
_W_RECENCY = 40.0
_W_RESPONSE = 30.0
_W_ENGAGEMENT = 30.0

# Status thresholds.
_HEALTHY_AT = 70
_AT_RISK_AT = 40

_MEETING_WINDOW_DAYS = 90


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _recency_points(days_since: Optional[int]) -> float:
    """Score how recently we were in touch (max ``_W_RECENCY``)."""
    if days_since is None:
        return 0.0
    if days_since <= 7:
        return _W_RECENCY
    if days_since <= 14:
        return _W_RECENCY * 0.85
    if days_since <= 30:
        return _W_RECENCY * 0.65
    if days_since <= 60:
        return _W_RECENCY * 0.40
    if days_since <= 90:
        return _W_RECENCY * 0.20
    return 0.0


def _engagement_points(meetings: int) -> float:
    """Score meeting frequency (max ``_W_ENGAGEMENT``); plateaus at 3 meetings."""
    return min(meetings, 3) / 3.0 * _W_ENGAGEMENT


async def compute_company_health(db: AsyncSession, company: Company) -> ClientHealth:
    """Compute a :class:`ClientHealth` for an already-loaded company."""
    now = datetime.now(timezone.utc)

    # --- Email signals (sends + replies) across the company's contacts ---
    email_stats = (
        await db.execute(
            select(
                func.count(EmailSend.id),
                func.count(EmailSend.replied_at),
                func.max(EmailSend.sent_at),
            )
            .join(Contact, EmailSend.contact_id == Contact.id)
            .where(Contact.company_id == company.id)
        )
    ).one()
    emails_sent = email_stats[0] or 0
    replies = email_stats[1] or 0
    last_email_at = _aware(email_stats[2])

    # --- Interaction signals (last touch + meetings in window) ---
    last_interaction_at = _aware(
        (
            await db.execute(
                select(func.max(Interaction.occurred_at)).where(
                    Interaction.company_id == company.id
                )
            )
        ).scalar_one_or_none()
    )

    window_start = now.timestamp() - _MEETING_WINDOW_DAYS * 86400
    meeting_rows = (
        await db.execute(
            select(Interaction.occurred_at).where(
                Interaction.company_id == company.id,
                Interaction.interaction_type == "meeting",
            )
        )
    ).scalars().all()
    meetings_last_90d = 0
    for occurred in meeting_rows:
        occurred_aware = _aware(occurred)
        if occurred_aware is not None and occurred_aware.timestamp() >= window_start:
            meetings_last_90d += 1

    # --- Last contact = most recent of any touchpoint ---
    candidates = [d for d in (last_email_at, last_interaction_at) if d is not None]
    last_contact_at = max(candidates) if candidates else None
    days_since = (now - last_contact_at).days if last_contact_at else None

    response_rate = (replies / emails_sent) if emails_sent else 0.0

    recency = round(_recency_points(days_since), 1)
    responsiveness = round(min(response_rate, 1.0) * _W_RESPONSE, 1)
    engagement = round(_engagement_points(meetings_last_90d), 1)
    score = int(round(recency + responsiveness + engagement))
    score = max(0, min(100, score))

    if score >= _HEALTHY_AT:
        status = "healthy"
    elif score >= _AT_RISK_AT:
        status = "at_risk"
    else:
        status = "churned"

    return ClientHealth(
        company_id=company.id,
        company_name=company.name,
        health_status=status,
        score=score,
        components=HealthComponents(
            recency=recency,
            responsiveness=responsiveness,
            engagement=engagement,
        ),
        signals=HealthSignals(
            last_contact_at=last_contact_at,
            days_since_last_contact=days_since,
            emails_sent=emails_sent,
            replies=replies,
            response_rate=round(response_rate, 3),
            meetings_last_90d=meetings_last_90d,
        ),
        computed_at=now,
    )


async def compute_health_by_id(db: AsyncSession, company_id: uuid.UUID) -> Optional[ClientHealth]:
    """Load a company by id and compute its health, or ``None`` if missing."""
    company = (
        await db.execute(select(Company).where(Company.id == company_id))
    ).scalar_one_or_none()
    if not company:
        return None
    return await compute_company_health(db, company)
