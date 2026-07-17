"""Organization self-service aggregation (Phase 3 — org management).

Computes the resource counts and monthly trends the ``/org/usage`` dashboard
shows for the caller's *own* organization: contacts, active members, emails
sent (this period and all-time), AI metering, and a last-N-months trend.

Emails sent are counted from **both** send-tracking tables — ``email_sends``
(engagement/manual sends) and ``outreach_events`` of type ``sent`` (sequence
sends) — because Landline records sends in both places depending on the path
(see the dual-send-tracking note in the activity log). Counts are always
filtered explicitly by ``org_id`` rather than relying on the global org read
filter, so they are correct regardless of the caller's bound context.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.contact import Contact
from app.models.email_template import EmailSend
from app.models.outreach import OutreachEvent
from app.models.tenant import TenantUsage
from app.models.user import User


def _month_start(dt: datetime) -> datetime:
    """First instant of *dt*'s month, in UTC."""
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _add_month(dt: datetime) -> datetime:
    """First instant of the month after *dt*'s month."""
    year, month = dt.year, dt.month
    if month == 12:
        return dt.replace(year=year + 1, month=1)
    return dt.replace(month=month + 1)


def _months_back(now: datetime, count: int) -> list[datetime]:
    """The month-start instants for the last *count* months, oldest first.

    Includes the current month, so ``count=6`` yields this month and the five
    before it.
    """
    months = [_month_start(now)]
    for _ in range(count - 1):
        # Step to the previous month by landing on the last day of it.
        last_of_prev = months[0] - timedelta(days=1)
        months.insert(0, _month_start(last_of_prev))
    return months


class OrgService:
    """Read-only aggregation for a single organization's usage dashboard."""

    async def _count_emails(
        self,
        db: AsyncSession,
        org_id: uuid.UUID,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> int:
        """Emails sent for *org_id* across both send-tracking tables.

        A half-open ``[since, until)`` window is applied when bounds are given.
        """
        send_q = select(func.count(EmailSend.id)).where(EmailSend.org_id == org_id)
        evt_q = select(func.count(OutreachEvent.id)).where(
            OutreachEvent.org_id == org_id, OutreachEvent.event_type == "sent"
        )
        if since is not None:
            send_q = send_q.where(EmailSend.sent_at >= since)
            evt_q = evt_q.where(OutreachEvent.occurred_at >= since)
        if until is not None:
            send_q = send_q.where(EmailSend.sent_at < until)
            evt_q = evt_q.where(OutreachEvent.occurred_at < until)
        sends = (await db.execute(send_q)).scalar_one()
        events = (await db.execute(evt_q)).scalar_one()
        return int(sends or 0) + int(events or 0)

    async def _count_ai_calls(
        self,
        db: AsyncSession,
        org_id: uuid.UUID,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> int:
        q = select(func.count(TenantUsage.id)).where(TenantUsage.tenant_id == org_id)
        if since is not None:
            q = q.where(TenantUsage.created_at >= since)
        if until is not None:
            q = q.where(TenantUsage.created_at < until)
        return int((await db.execute(q)).scalar_one() or 0)

    async def resource_counts(
        self, db: AsyncSession, org_id: uuid.UUID, since: datetime
    ) -> dict:
        """Contacts, active members, and email counts for *org_id*."""
        contacts = (
            await db.execute(
                select(func.count(Contact.id)).where(Contact.org_id == org_id)
            )
        ).scalar_one()
        members = (
            await db.execute(
                select(func.count(User.id)).where(
                    User.org_id == org_id, User.is_active.is_(True)
                )
            )
        ).scalar_one()
        emails_this_month = await self._count_emails(db, org_id, since=since)
        emails_total = await self._count_emails(db, org_id)
        return {
            "contacts": int(contacts or 0),
            "members": int(members or 0),
            "emails_sent_this_month": emails_this_month,
            "emails_sent_total": emails_total,
        }

    async def monthly_trend(
        self, db: AsyncSession, org_id: uuid.UUID, now: datetime, months: int = 6
    ) -> list[dict]:
        """Emails sent and AI calls per month for the last *months* months."""
        trend = []
        for start in _months_back(now, months):
            end = _add_month(start)
            trend.append(
                {
                    "month": f"{start.year:04d}-{start.month:02d}",
                    "emails_sent": await self._count_emails(db, org_id, start, end),
                    "ai_calls": await self._count_ai_calls(db, org_id, start, end),
                }
            )
        return trend


_org_service: Optional[OrgService] = None


def get_org_service() -> OrgService:
    global _org_service
    if _org_service is None:
        _org_service = OrgService()
    return _org_service
