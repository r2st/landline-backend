"""Google Calendar integration (feature 2.5).

Creates calendar events on a connected user's Google Calendar using the same
OAuth grant used for Gmail sending (the ``calendar.events`` scope was added to
``google_oauth.SCOPES``). Credentials are rebuilt from the account's encrypted
refresh token exactly as ``smart_reply`` does for Gmail.

Best-effort by design: an account connected before the calendar scope was added
won't have consented to it, so ``create_event`` returns ``{"error": ...}`` rather
than raising, and callers fall back to offering a scheduling link.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import get_settings
from app.models.gmail_account import GmailAccount
from app.services import crypto, google_oauth

logger = logging.getLogger(__name__)

_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _build_credentials(account: GmailAccount) -> Credentials:
    """Rebuild OAuth credentials from an account's encrypted tokens."""
    settings = get_settings()
    refresh_token = crypto.decrypt(account.refresh_token_encrypted)
    access_token = None
    if account.access_token_encrypted:
        try:
            access_token = crypto.decrypt(account.access_token_encrypted)
        except Exception:
            access_token = None
    return Credentials(
        token=access_token,
        refresh_token=refresh_token,
        token_uri=_TOKEN_URI,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=google_oauth.SCOPES,
    )


def create_event(
    account: GmailAccount,
    *,
    summary: str,
    start: datetime,
    end: Optional[datetime] = None,
    attendee_emails: Optional[list[str]] = None,
    description: str = "",
    duration_minutes: int = 30,
) -> dict:
    """Create a calendar event on the account owner's primary calendar.

    Returns ``{"event_id", "html_link", "start", "end"}`` on success or
    ``{"error": ...}`` on failure (never raises). Runs the blocking Google client
    synchronously — call it from a thread/executor if used on the event loop.
    """
    if end is None:
        end = start + timedelta(minutes=duration_minutes)
    try:
        creds = _build_credentials(account)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if attendee_emails:
            body["attendees"] = [{"email": e} for e in attendee_emails if e]
        event = (
            service.events()
            .insert(calendarId="primary", body=body, sendUpdates="all")
            .execute()
        )
        return {
            "event_id": event.get("id"),
            "html_link": event.get("htmlLink"),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    except Exception as e:  # noqa: BLE001 — best-effort; caller falls back to a link
        logger.warning("Calendar event creation failed for %s: %s", account.email, e)
        return {"error": str(e)}
