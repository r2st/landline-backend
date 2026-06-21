"""Notification service — sends alerts on important lead engagement events."""

import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config import get_settings

logger = logging.getLogger(__name__)


class NotificationService:
    """Sends email notifications for important engagement events."""

    def __init__(self):
        self.settings = get_settings()

    async def notify_reply(self, contact_name: str, company_name: str, contact_email: str):
        """Notify the user when a lead replies."""
        subject = f"Reply received from {contact_name} at {company_name}"
        body = f"""
A lead has replied to your outreach!

Contact: {contact_name}
Company: {company_name}
Email: {contact_email}

The follow-up sequence has been automatically paused.
Company stage has been advanced to "Engaged".

Log in to Landline to review and respond.
"""
        await self._send_notification(subject, body)

    async def notify_open(self, contact_name: str, company_name: str, opens: int = 1):
        """Notify when a lead opens an email (only for significant opens)."""
        if opens < 3:
            return  # Only notify on 3+ opens (shows real interest)

        subject = f"{contact_name} at {company_name} opened your email {opens} times"
        body = f"""
High engagement detected!

{contact_name} at {company_name} has opened your email {opens} times.
This suggests strong interest — consider following up soon.

Log in to Landline to take action.
"""
        await self._send_notification(subject, body)

    async def notify_click(self, contact_name: str, company_name: str, url: str):
        """Notify when a lead clicks a link."""
        subject = f"{contact_name} at {company_name} clicked a link"
        body = f"""
A lead clicked a link in your email!

Contact: {contact_name}
Company: {company_name}
Link clicked: {url}

This is a strong buying signal. Consider reaching out directly.

Log in to Landline to take action.
"""
        await self._send_notification(subject, body)

    async def _send_notification(self, subject: str, body: str):
        """Send notification email to the configured recipient."""
        notify_email = self.settings.notification_email
        if not notify_email:
            logger.debug("No notification_email configured, skipping notification")
            return

        try:
            # Use the Gmail dispatch if available, otherwise log
            from app.services.email_dispatch import get_dispatch_service
            dispatch = get_dispatch_service()

            # Simple email send
            msg = MIMEMultipart()
            msg["Subject"] = f"[Landline] {subject}"
            msg["To"] = notify_email
            msg.attach(MIMEText(body, "plain"))

            # Try to send via the existing dispatch mechanism
            # For now, just log it — the full implementation would use Gmail API
            logger.info("NOTIFICATION: %s — %s", subject, notify_email)

        except Exception as e:
            logger.error("Failed to send notification: %s", str(e))


def get_notification_service() -> NotificationService:
    return NotificationService()
