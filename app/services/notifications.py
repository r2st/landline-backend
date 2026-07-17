"""Notification service — sends alerts on important lead engagement events."""

import logging

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

    async def _send_notification(self, subject: str, body: str):
        """Send notification email to the configured recipient."""
        notify_email = self.settings.notification_email
        if not notify_email:
            logger.debug("No notification_email configured, skipping notification")
            return

        logger.warning(
            "NOTIFICATION (not yet delivered): [%s] to %s — configure email dispatch to enable delivery",
            subject, notify_email,
        )


def get_notification_service() -> NotificationService:
    return NotificationService()
