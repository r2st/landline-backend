"""Google OAuth 2.0 helper for connecting a user's Gmail account.

Drives the authorization-code flow used to obtain a long-lived refresh token
that lets Landline send mail as the connecting user via the Gmail API.

Scopes requested:
    openid / email / profile  — to identify which account connected
    gmail.send                — to send mail on the user's behalf (send-only)

State is a signed, time-limited token (encrypted with the same Fernet key as
stored credentials) so the callback can be validated without server-side
session storage.
"""

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import get_settings
from app.services import crypto

logger = logging.getLogger(__name__)

# Gmail send + read-only scopes plus the minimal identity scopes needed to know who
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
_REVOKE_URI = "https://oauth2.googleapis.com/revoke"

# Maximum age of an OAuth ``state`` token, in seconds.
_STATE_TTL = 600


class OAuthConfigError(RuntimeError):
    """Raised when Google OAuth env vars are not configured."""


class OAuthError(RuntimeError):
    """Raised when the OAuth exchange fails."""


@dataclass
class ConnectedAccount:
    """Result of a successful OAuth exchange."""

    email: str
    google_sub: str
    display_name: Optional[str]
    refresh_token: Optional[str]
    access_token: str
    expires_in: int
    scopes: str


def is_configured() -> bool:
    s = get_settings()
    return bool(s.google_client_id and s.google_client_secret)


def _require_config():
    s = get_settings()
    if not (s.google_client_id and s.google_client_secret):
        raise OAuthConfigError(
            "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and "
            "GOOGLE_CLIENT_SECRET (see deploy/GOOGLE-OAUTH-SETUP.md)."
        )
    return s


def issue_state() -> str:
    """Create a signed, time-limited state token for CSRF protection."""
    payload = json.dumps({"n": uuid.uuid4().hex, "t": int(time.time())})
    return crypto.encrypt(payload)


def verify_state(state: str) -> bool:
    """Validate a state token issued by :func:`issue_state`."""
    if not state:
        return False
    try:
        payload = json.loads(crypto.decrypt(state))
    except Exception:
        return False
    issued = payload.get("t", 0)
    return (time.time() - issued) <= _STATE_TTL


def build_authorization_url(state: str) -> str:
    """Build the Google consent-screen URL to redirect the user to."""
    s = _require_config()
    from urllib.parse import urlencode

    params = {
        "client_id": s.google_client_id,
        "redirect_uri": s.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        # offline + consent guarantees we receive a refresh_token even on
        # re-connect of an already-authorized account.
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{_AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str) -> ConnectedAccount:
    """Exchange an authorization code for tokens and the user's identity."""
    s = _require_config()
    with httpx.Client(timeout=15) as client:
        token_resp = client.post(
            _TOKEN_URI,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": s.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("Token exchange failed: %s", token_resp.text)
            raise OAuthError(f"Token exchange failed: {token_resp.status_code}")
        tok = token_resp.json()

        access_token = tok["access_token"]
        userinfo_resp = client.get(
            _USERINFO_URI,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_resp.status_code != 200:
            logger.error("Userinfo fetch failed: %s", userinfo_resp.text)
            raise OAuthError("Could not fetch Google account profile")
        info = userinfo_resp.json()

    email = info.get("email")
    sub = info.get("sub")
    if not email or not sub:
        raise OAuthError("Google did not return an email/sub for the account")

    return ConnectedAccount(
        email=email,
        google_sub=sub,
        display_name=info.get("name"),
        refresh_token=tok.get("refresh_token"),
        access_token=access_token,
        expires_in=int(tok.get("expires_in", 3600)),
        scopes=tok.get("scope", " ".join(SCOPES)),
    )


def refresh_access_token(refresh_token: str) -> tuple[str, int]:
    """Exchange a refresh token for a fresh access token. Returns (token, expires_in)."""
    s = _require_config()
    with httpx.Client(timeout=15) as client:
        resp = client.post(
            _TOKEN_URI,
            data={
                "refresh_token": refresh_token,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "grant_type": "refresh_token",
            },
        )
    if resp.status_code != 200:
        logger.error("Access-token refresh failed: %s", resp.text)
        raise OAuthError("Failed to refresh access token — re-connect may be required")
    data = resp.json()
    return data["access_token"], int(data.get("expires_in", 3600))


def revoke_token(token: str) -> bool:
    """Best-effort revocation of a token at Google. Returns True on success."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                _REVOKE_URI,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return resp.status_code == 200
    except Exception as exc:  # revocation is best-effort
        logger.warning("Token revocation failed: %s", exc)
        return False
