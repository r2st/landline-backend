"""Amazon SES email sending service."""

import logging
from typing import Optional

from jinja2.sandbox import SandboxedEnvironment

from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class EmailSender:
    """Sends emails via Amazon SES with template rendering."""

    def __init__(self):
        self.sender = settings.ses_sender_email
        self._client = None

    @property
    def client(self):
        """Lazy-load boto3 SES client to avoid import-time failures in test environments."""
        if self._client is None:
            import boto3
            self._client = boto3.client(
                "ses",
                region_name=settings.aws_region,
                aws_access_key_id=settings.aws_access_key_id or None,
                aws_secret_access_key=settings.aws_secret_access_key or None,
            )
        return self._client

    def render_template(self, template_str: str, context: dict) -> str:
        """Render a Jinja2 template string with the given context.

        Uses SandboxedEnvironment to prevent server-side template injection.
        """
        env = SandboxedEnvironment()
        tmpl = env.from_string(template_str)
        return tmpl.render(**context)

    def _build_unsubscribe_footer(self, contact_email: str) -> str:
        """CAN-SPAM compliant unsubscribe footer with signed token (no PII in URL)."""
        serializer = URLSafeTimedSerializer(settings.secret_key or "landline-unsubscribe-secret")
        token = serializer.dumps(contact_email, salt="unsubscribe")
        unsubscribe_url = f"https://landline.apprendtechnologies.com/unsubscribe?token={token}"
        return (
            "\n\n—\n"
            "Apprend Technologies\n"
            f"Don't want these emails? Reply UNSUBSCRIBE or click: "
            f"{unsubscribe_url}\n"
            "Toronto, ON, Canada"
        )

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
        reply_to: Optional[str] = None,
        include_unsubscribe: bool = True,
    ) -> dict:
        """Send an email via SES.

        Returns dict with 'message_id' on success or 'error' on failure.
        """
        if not settings.aws_access_key_id or not settings.aws_secret_access_key:
            logger.warning("AWS SES credentials not configured — email not sent")
            return {"status": "skipped", "reason": "no_ses_credentials"}

        if include_unsubscribe:
            footer = self._build_unsubscribe_footer(to_email)
            if body_text:
                body_text += footer
            body_html += footer.replace("\n", "<br>")

        destination = {"ToAddresses": [to_email]}
        message = {
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {
                "Html": {"Data": body_html, "Charset": "UTF-8"},
            },
        }
        if body_text:
            message["Body"]["Text"] = {"Data": body_text, "Charset": "UTF-8"}

        try:
            response = self.client.send_email(
                Source=self.sender,
                Destination=destination,
                Message=message,
                ReplyToAddresses=[reply_to] if reply_to else [],
            )
            message_id = response["MessageId"]
            logger.info("Email sent to %s — MessageId: %s", to_email, message_id)
            return {"status": "sent", "message_id": message_id}

        except Exception as e:
            # Handle boto3 ClientError specifically if boto3 is available, and
            # fall back to a generic error dict for any other failure. Both
            # paths must return (never re-raise) so callers get a consistent
            # result shape instead of an unhandled 500.
            error_response = getattr(e, "response", None)
            if error_response and "Error" in error_response:
                error_code = error_response["Error"]["Code"]
                error_msg = error_response["Error"]["Message"]
                logger.error("SES error sending to %s: [%s] %s", to_email, error_code, error_msg)
                return {"status": "error", "error_code": error_code, "detail": error_msg}
            logger.error("Failed to send email to %s: %s", to_email, str(e))
            return {"status": "error", "detail": str(e)}

    def send_sequence_email(
        self,
        to_email: str,
        subject_template: str,
        body_template: str,
        context: dict,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Render templates and send a sequence email."""
        subject = self.render_template(subject_template, context)
        body_html = self.render_template(body_template, context)
        body_text = self.render_template(body_template, context)

        return self.send_email(
            to_email=to_email,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            reply_to=reply_to,
        )


def get_email_sender() -> EmailSender:
    return EmailSender()
