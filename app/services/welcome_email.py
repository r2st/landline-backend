"""Transactional welcome email for admin-created user accounts.

When an admin provisions a new account we email the person their login
credentials so they can sign in and change the temporary password. This is a
transactional message (no marketing footer, no unsubscribe) sent from the
primary connected sending account — Gmail preferred, then SMTP, per
:func:`app.services.email_dispatch.get_active_account`.

Sending is best-effort: if no sending account is configured the caller is told
so it can surface a warning, but account creation is never blocked.
"""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.user import User
from app.services.email_dispatch import get_active_account, _make_sender

logger = logging.getLogger(__name__)


def _render_welcome(user: User, temp_password: str, login_url: str, app_name: str) -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for the welcome email."""
    subject = f"Welcome to {app_name} — your account is ready"

    html_body = (
        f"<p>Hi {user.name},</p>"
        f"<p>An administrator has created a {app_name} account for you. "
        f"Here are your login details:</p>"
        f"<p style=\"margin:16px 0;padding:12px 16px;background:#f5f6f8;"
        f"border-radius:8px;font-family:monospace\">"
        f"<strong>Email:</strong> {user.email}<br>"
        f"<strong>Temporary password:</strong> {temp_password}"
        f"</p>"
        f"<p>Sign in here: <a href=\"{login_url}\">{login_url}</a></p>"
        f"<p><strong>For your security, you'll be asked to change this "
        f"temporary password the first time you sign in.</strong></p>"
        f"<p>Welcome aboard,<br>The {app_name} team</p>"
    )

    text_body = (
        f"Hi {user.name},\n\n"
        f"An administrator has created a {app_name} account for you. "
        f"Here are your login details:\n\n"
        f"  Email: {user.email}\n"
        f"  Temporary password: {temp_password}\n\n"
        f"Sign in here: {login_url}\n\n"
        f"For your security, you'll be asked to change this temporary password "
        f"the first time you sign in.\n\n"
        f"Welcome aboard,\n"
        f"The {app_name} team"
    )

    return subject, html_body, text_body


async def send_welcome_email(
    db: AsyncSession, user: User, temp_password: str
) -> bool:
    """Email a newly-created user their login credentials.

    Returns ``True`` if the message was handed to a transport successfully,
    ``False`` if no sending account is configured or the send failed. Never
    raises — account creation must not be blocked by email problems.
    """
    settings = get_settings()

    try:
        account = await get_active_account(db)
        if account is None:
            logger.warning(
                "No connected sending account configured — welcome email to %s "
                "not sent",
                user.email,
            )
            return False

        login_url = settings.frontend_url.rstrip("/") + "/login"
        subject, html_body, text_body = _render_welcome(
            user, temp_password, login_url, settings.app_name
        )

        sender = _make_sender(account)
        result = await asyncio.to_thread(
            sender.send_email,
            to_email=user.email,
            subject=subject,
            body_html=html_body,
            body_text=text_body,
        )

        if result.get("status") == "sent":
            logger.info(
                "Welcome email sent to %s via %s (%s)",
                user.email,
                account.email,
                result.get("transport", "?"),
            )
            return True

        logger.warning(
            "Welcome email to %s was not sent (status=%s): %s",
            user.email,
            result.get("status"),
            result.get("detail") or result.get("reason"),
        )
        return False

    except Exception:  # noqa: BLE001 — best-effort, must never block creation
        logger.exception("Failed to send welcome email to %s", user.email)
        return False
