"""Send rate limiter — prevents spam by enforcing sending limits."""

import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.config import get_settings
from app.models.outreach import OutreachEvent, OutreachEnrollment
from app.models.contact import Contact

logger = logging.getLogger(__name__)


class SendLimiter:
    """Enforces email sending limits to prevent spam flagging."""

    def __init__(self):
        self.settings = get_settings()

    async def can_send_to_contact(self, db: AsyncSession, contact_email: str) -> tuple[bool, str]:
        """Check if we can send to this contact. Returns (allowed, reason)."""
        now = datetime.now(timezone.utc)

        # Check monthly cap per contact
        thirty_days_ago = now - timedelta(days=30)
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(Contact.email == contact_email)
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= thirty_days_ago)
        )
        monthly_count = result.scalar() or 0
        if monthly_count >= self.settings.max_emails_per_contact_per_month:
            return False, f"Monthly limit reached for {contact_email} ({monthly_count}/{self.settings.max_emails_per_contact_per_month})"

        # Check minimum gap between emails to same contact
        result = await db.execute(
            select(OutreachEvent.occurred_at)
            .join(OutreachEnrollment, OutreachEvent.enrollment_id == OutreachEnrollment.id)
            .join(Contact, OutreachEnrollment.contact_id == Contact.id)
            .where(Contact.email == contact_email)
            .where(OutreachEvent.event_type == "sent")
            .order_by(OutreachEvent.occurred_at.desc())
            .limit(1)
        )
        last_sent = result.scalar_one_or_none()
        if last_sent:
            min_gap = timedelta(days=self.settings.min_days_between_followups)
            if now - last_sent < min_gap:
                days_remaining = (min_gap - (now - last_sent)).days + 1
                return False, f"Too soon to email {contact_email} again (wait {days_remaining} more days)"

        return True, "OK"

    def within_send_window(self) -> tuple[bool, str]:
        """Whether the current time is inside the configured send window.

        Cold email that arrives at 3 AM (or Sunday) reads as a bot and depresses
        reply rates. We restrict sends to business hours on weekdays in the
        configured timezone. Returns (allowed, reason).
        """
        if not self.settings.send_window_enabled:
            return True, "send window disabled"

        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.settings.send_window_timezone)
        except Exception:
            # Unknown tz name → fail open rather than block all sending.
            return True, "send window tz unavailable"

        now_local = datetime.now(tz)
        if self.settings.send_window_weekdays_only and now_local.weekday() >= 5:
            return False, f"outside send window (weekend, {now_local:%a %H:%M} {self.settings.send_window_timezone})"

        start = self.settings.send_window_start_hour
        end = self.settings.send_window_end_hour
        if not (start <= now_local.hour < end):
            return False, (
                f"outside send window ({now_local:%H:%M} not in "
                f"{start:02d}:00–{end:02d}:00 {self.settings.send_window_timezone})"
            )
        return True, f"within send window ({now_local:%a %H:%M})"

    async def can_send_today(self, db: AsyncSession) -> tuple[bool, str]:
        """Check if we have hit the daily sending limit."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= today_start)
        )
        today_count = result.scalar() or 0

        daily_limit = self._get_daily_limit()

        if today_count >= daily_limit:
            warmup_note = " Warmup mode active." if self.settings.warmup_enabled else ""
            return False, f"Daily limit reached ({today_count}/{daily_limit}).{warmup_note}"

        return True, f"OK ({today_count}/{daily_limit} sent today)"

    def _get_daily_limit(self) -> int:
        """Get current daily limit, accounting for warmup period."""
        if not self.settings.warmup_enabled or not self.settings.warmup_start_date:
            return self.settings.max_emails_per_day

        try:
            start = datetime.fromisoformat(self.settings.warmup_start_date).replace(tzinfo=timezone.utc)
            days_active = (datetime.now(timezone.utc) - start).days

            if days_active < 7:
                return self.settings.warmup_daily_limit_week1
            elif days_active < 14:
                return self.settings.warmup_daily_limit_week2
            elif days_active < 21:
                return self.settings.warmup_daily_limit_week3
            else:
                # Warmup complete
                return self.settings.max_emails_per_day
        except (ValueError, TypeError):
            return self.settings.max_emails_per_day

    async def get_send_stats(self, db: AsyncSession) -> dict:
        """Get current sending statistics for display."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        thirty_days_ago = now - timedelta(days=30)

        # Today's sends
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= today_start)
        )
        today_count = result.scalar() or 0

        # 30-day sends
        result = await db.execute(
            select(func.count(OutreachEvent.id))
            .where(OutreachEvent.event_type == "sent")
            .where(OutreachEvent.occurred_at >= thirty_days_ago)
        )
        monthly_count = result.scalar() or 0

        daily_limit = self._get_daily_limit()

        return {
            "today_sent": today_count,
            "daily_limit": daily_limit,
            "daily_remaining": max(0, daily_limit - today_count),
            "monthly_sent": monthly_count,
            "warmup_active": self.settings.warmup_enabled and bool(self.settings.warmup_start_date),
            "warmup_week": self._get_warmup_week(),
            "max_per_contact_monthly": self.settings.max_emails_per_contact_per_month,
            "min_days_between_followups": self.settings.min_days_between_followups,
        }

    async def account_capacity(self, db: AsyncSession, account) -> dict:
        """Deliverability-aware daily capacity for a single sending account (feature 3.3).

        The static daily cap is scaled by the account's ``throttle_factor`` from
        its ``SenderHealth`` row (1.0 when unknown), so a struggling account is
        allowed fewer sends and volume shifts to healthier accounts.
        """
        from app.models.sender_health import SenderHealth

        transport = "smtp" if type(account).__name__ == "SmtpAccount" else "gmail"
        row = (
            await db.execute(
                select(SenderHealth).where(
                    SenderHealth.account_type == transport,
                    SenderHealth.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        throttle = row.throttle_factor if row is not None else 1.0
        base = self.settings.max_emails_per_day
        effective = max(1, round(base * throttle))
        used = account.daily_send_count or 0
        remaining = max(0, effective - used)
        return {
            "throttle_factor": throttle,
            "reputation_score": row.reputation_score if row is not None else None,
            "health_status": row.status if row is not None else "unknown",
            "base_daily_limit": base,
            "effective_daily_limit": effective,
            "used_today": used,
            "remaining_today": remaining,
            "can_send": remaining > 0 and (row is None or row.status != "critical"),
        }

    def _get_warmup_week(self) -> int | None:
        if not self.settings.warmup_enabled or not self.settings.warmup_start_date:
            return None
        try:
            start = datetime.fromisoformat(self.settings.warmup_start_date).replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - start).days
            return min(days // 7 + 1, 4)  # Cap at week 4 (warmup complete)
        except (ValueError, TypeError):
            return None


def get_send_limiter() -> SendLimiter:
    return SendLimiter()
