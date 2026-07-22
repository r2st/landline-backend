"""Notification service — sends alerts on important lead engagement events."""

import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends email notifications for important engagement events."""

    def __init__(self):
        self.settings = get_settings()

    async def notify_reply(self, contact_name: str, company_name: str, contact_email: str):
        """Notify the user when a lead replies."""
        subject = f"{contact_name} from {company_name} replied"
        body = f"""Hey — {contact_name} at {company_name} just replied to your email.

Their follow-up sequence is paused and the company's been moved to Engaged.

Jump into Landline to read it and respond: {contact_email}
"""
        await self._send_notification(subject, body)

    async def notify_open(self, contact_name: str, company_name: str, opens: int = 1):
        """Notify when a lead opens an email (only for significant opens)."""
        if opens < 3:
            return  # Only notify on 3+ opens (shows real interest)

        subject = f"{contact_name} at {company_name} keeps opening your email"
        body = f"""{contact_name} at {company_name} has opened your email {opens} times now. That's a good sign — they're clearly interested.

Might be worth reaching out while it's fresh on their mind. Check Landline for details.
"""
        await self._send_notification(subject, body)

    async def notify_click(self, contact_name: str, company_name: str, url: str):
        """Notify when a lead clicks a link."""
        subject = f"{contact_name} at {company_name} clicked your link"
        body = f"""{contact_name} at {company_name} just clicked a link in your email:
{url}

That's a strong signal. Good time to follow up while they're actively looking. Check Landline for the full picture.
"""
        await self._send_notification(subject, body)

    async def notify_phone_found(
        self, contact_name: str, company_name: str, phone: str
    ):
        """Notify when a warm lead's phone number was auto-enriched.

        Fired only when the enrichment sweep actually found a number for a lead
        that was already showing repeated engagement — the whole point is to hand
        the operator a warm number to dial while interest is fresh.
        """
        subject = f"📞 Phone found for {contact_name} at {company_name}"
        body = f"""{contact_name} at {company_name} has been engaging with your emails, and Dialtone just found a phone number for them:

{phone}

They're warm — a personal call now could move this forward. Open the company in Landline for the full picture.
"""
        await self._send_notification(subject, body)

    async def notify_sender_health_change(
        self,
        account_email: str,
        prior_status: Optional[str],
        new_status: str,
        metrics: Optional[dict] = None,
        db: Optional[AsyncSession] = None,
    ):
        """Notify when a sending account's reputation degrades (feature 3.3).

        Fired only on a *degrading transition* into ``throttled`` / ``critical``
        from a healthier state — the caller guards this so an account that is
        already throttled (or that recovers) does not re-alert on every hourly
        assessment.
        """
        m = metrics or {}
        subject = f"⚠️ Sender reputation: {account_email} is now {new_status}"
        body = (
            f"Dialtone throttled sending account {account_email}: reputation "
            f"transitioned {prior_status or 'unknown'} → {new_status}.\n\n"
            f"Last 7 days — sent: {m.get('sent_7d', '?')}, "
            f"bounce rate: {m.get('bounce_rate_7d', '?')}, "
            f"complaint rate: {m.get('complaint_rate_7d', '?')}, "
            f"open rate: {m.get('open_rate_7d', '?')}.\n\n"
            f"Volume has been shifted to healthier senders automatically. Open the "
            f"Sender Health dashboard in Landline to investigate before this domain's "
            f"reputation drags down the rest.\n"
        )
        await self._send_notification(subject, body, db=db)

    async def notify_domain_health_change(
        self, domain: str, summary: str, details: str = ""
    ):
        """Notify when a sending domain's health changes (Feature 5).

        Fired only on a *transition* (score/band change or a new blacklist
        listing), never on an unchanged daily run — the caller guards this.
        """
        subject = f"⚠️ Domain health: {domain} — {summary}"
        body = (
            f"Dialtone detected a change in the health of sending domain {domain}:\n\n"
            f"{summary}\n"
        )
        if details:
            body += f"\n{details}\n"
        body += "\nOpen the Domain Health dashboard in Landline for the full breakdown and DNS fixes.\n"
        await self._send_notification(subject, body)

    async def _send_notification(
        self, subject: str, body: str, db: Optional[AsyncSession] = None
    ) -> dict:
        """Deliver a notification email to ``settings.notification_email``.

        Sends through the org's active connected sending account (Gmail preferred,
        SMTP fallback) — the same transactional path as the welcome email, so no
        marketing footer or open tracking is attached. Best-effort by contract:
        a missing recipient/account or a transport error is logged and returned as
        a status dict, never raised, because every caller treats notification as a
        side effect that must not break the work that triggered it.

        ``db`` is used when the caller already holds a session; otherwise a
        short-lived Celery session is opened (notifications fire overwhelmingly
        from Celery tasks). The session inherits the caller's org context, so the
        sending account chosen belongs to the right tenant.
        """
        notify_email = self.settings.notification_email
        if not notify_email:
            logger.debug("No notification_email configured, skipping notification")
            return {"status": "skipped", "reason": "no_notification_email"}

        try:
            if db is not None:
                return await self._deliver(db, notify_email, subject, body)
            from app.database_celery import celery_session

            async with celery_session() as own_db:
                return await self._deliver(own_db, notify_email, subject, body)
        except Exception:  # noqa: BLE001 — notification must never break its caller
            logger.exception("Notification delivery failed: [%s]", subject)
            return {"status": "error", "subject": subject}

    async def _deliver(
        self, db: AsyncSession, to_email: str, subject: str, body: str
    ) -> dict:
        """Pick the active sending account and hand the message to its transport."""
        from app.services.email_dispatch import get_active_account, _make_sender

        account = await get_active_account(db)
        if account is None:
            logger.warning(
                "No connected sending account — notification not delivered: [%s]",
                subject,
            )
            return {"status": "no_account", "subject": subject}

        # Plain-text bodies are the source of truth; wrap for a minimal HTML part
        # so it renders as paragraphs rather than one run-on line.
        html_body = "<p>" + body.strip().replace("\n\n", "</p><p>").replace(
            "\n", "<br>"
        ) + "</p>"

        result = await asyncio.to_thread(
            _make_sender(account).send_email,
            to_email=to_email,
            subject=subject,
            body_html=html_body,
            body_text=body,
        )
        logger.info(
            "Notification [%s] → %s via %s (%s)",
            subject, to_email, account.email, result.get("status"),
        )
        return result


def get_notification_service() -> NotificationService:
    return NotificationService()
