"""Send outreach email through a configured SMTP server.

Used for sender accounts that aren't connected via Google OAuth — Outlook /
Office 365, custom mail hosts, or any provider exposing authenticated SMTP.

This module is synchronous (``smtplib`` is blocking); async callers should
invoke it via ``asyncio.to_thread`` (see ``email_dispatch``).
"""

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

from app.services import crypto

logger = logging.getLogger(__name__)

# Connections to third-party SMTP hosts can be slow; cap so a wedged host can't
# hang a request/worker indefinitely.
_SMTP_TIMEOUT = 20


class SmtpSender:
    """Sends email through an authenticated SMTP server for one account.

    Construct with a :class:`~app.models.smtp_account.SmtpAccount`. The stored
    password (decrypted) is used to authenticate on each send.
    """

    def __init__(self, account):
        self.account = account
        self.sender_email = account.email
        self.display_name = account.display_name

    def _connect(self) -> smtplib.SMTP:
        password = crypto.decrypt(self.account.password_encrypted)
        context = ssl.create_default_context()
        if self.account.use_ssl:
            server = smtplib.SMTP_SSL(
                self.account.host, self.account.port, timeout=_SMTP_TIMEOUT, context=context
            )
        else:
            server = smtplib.SMTP(self.account.host, self.account.port, timeout=_SMTP_TIMEOUT)
            server.ehlo()
            if self.account.use_tls:
                server.starttls(context=context)
                server.ehlo()
        server.login(self.account.username, password)
        return server

    def verify(self) -> dict:
        """Test that the host/credentials authenticate. Returns {ok, detail}."""
        try:
            server = self._connect()
            try:
                server.noop()
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            return {"ok": True}
        except smtplib.SMTPAuthenticationError as e:
            detail = e.smtp_error.decode(errors="replace") if isinstance(e.smtp_error, bytes) else str(e)
            return {"ok": False, "detail": f"Authentication failed: {detail}"}
        except Exception as e:
            return {"ok": False, "detail": str(e)}

    def _build_message(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str],
        reply_to: Optional[str],
    ) -> MIMEMultipart:
        message = MIMEMultipart("alternative")
        message["To"] = to_email
        message["From"] = formataddr((self.display_name or "", self.sender_email))
        message["Subject"] = subject
        if reply_to:
            message["Reply-To"] = reply_to
        if body_text:
            message.attach(MIMEText(body_text, "plain", "utf-8"))
        message.attach(MIMEText(body_html, "html", "utf-8"))
        return message

    def send_email(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> dict:
        """Send a single email. Mirrors GmailSender.send_email's return shape."""
        try:
            message = self._build_message(to_email, subject, body_html, body_text, reply_to)
            server = self._connect()
            try:
                server.sendmail(self.sender_email, [to_email], message.as_string())
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
            logger.info("SMTP email sent to %s as %s", to_email, self.sender_email)
            return {
                "status": "sent",
                "transport": "smtp",
                "sender": self.sender_email,
            }
        except Exception as e:
            logger.error("Failed to send SMTP email to %s as %s: %s", to_email, self.sender_email, e)
            return {"status": "error", "transport": "smtp", "detail": str(e)}
