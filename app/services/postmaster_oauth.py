"""Google OAuth 2.0 helper for connecting Google Postmaster Tools.

Mirrors :mod:`app.services.google_oauth` but requests the read-only Postmaster
scope instead of Gmail send/read. Reuses the same Google client credentials
(``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET``) and a dedicated redirect URI so
the callback can be routed to the Postmaster router.

The connected account must have verified its sending domains at
https://postmaster.google.com — the API only returns data for verified domains
owned by the authenticated account.
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

# Read-only access to Postmaster Tools, plus minimal identity scopes to know
# which Google account connected.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/postmaster.readonly",
]

_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"
_REVOKE_URI = "https://oauth2.googleapis.com/revoke"

_STATE_TTL = 600


class OAuthConfigError(RuntimeError):
    """Raised when Google OAuth env vars are not configured."""


class OAuthError(RuntimeError):
    """Raised when the OAuth exchange fails."""


@dataclass
class ConnectedAccount:
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


def _redirect_uri(s) -> str:
    """Postmaster's own callback URI, derived from the Gmail one if unset.

    Falls back to swapping ``/gmail/oauth/callback`` for the Postmaster path so a
    deployment that only configured the Gmail redirect still works.
    """
    configured = getattr(s, "postmaster_oauth_redirect_uri", "") or ""
    if configured:
        return configured
    return s.google_oauth_redirect_uri.replace(
        "/gmail/oauth/callback", "/postmaster/oauth/callback"
    )


def issue_state(user_id: Optional[str] = None) -> str:
    """Create a signed, time-limited state token for CSRF protection."""
    payload = {"n": uuid.uuid4().hex, "t": int(time.time())}
    if user_id is not None:
        payload["u"] = str(user_id)
    return crypto.encrypt(json.dumps(payload))


def read_state(state: str) -> Optional[dict]:
    """Decode + validate a state token, returning its payload or ``None``."""
    if not state:
        return None
    try:
        payload = json.loads(crypto.decrypt(state))
    except Exception:
        return None
    if (time.time() - payload.get("t", 0)) > _STATE_TTL:
        return None
    return payload


def build_authorization_url(state: str) -> str:
    """Build the Google consent-screen URL for connecting Postmaster Tools."""
    s = _require_config()
    from urllib.parse import urlencode

    params = {
        "client_id": s.google_client_id,
        "redirect_uri": _redirect_uri(s),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{_AUTH_URI}?{urlencode(params)}"


def exchange_code(code: str) -> ConnectedAccount:
    """Exchange an authorization code for tokens and the account identity."""
    s = _require_config()
    with httpx.Client(timeout=15) as client:
        token_resp = client.post(
            _TOKEN_URI,
            data={
                "code": code,
                "client_id": s.google_client_id,
                "client_secret": s.google_client_secret,
                "redirect_uri": _redirect_uri(s),
                "grant_type": "authorization_code",
            },
        )
        if token_resp.status_code != 200:
            logger.error("Postmaster token exchange failed: %s", token_resp.text)
            raise OAuthError(f"Token exchange failed: {token_resp.status_code}")
        tok = token_resp.json()

        access_token = tok["access_token"]
        userinfo_resp = client.get(
            _USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
        )
        if userinfo_resp.status_code != 200:
            logger.error("Postmaster userinfo fetch failed: %s", userinfo_resp.text)
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
    """Exchange a refresh token for a fresh access token. Returns (token, ttl)."""
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
        logger.error("Postmaster access-token refresh failed: %s", resp.text)
        raise OAuthError("Failed to refresh access token — re-connect may be required")
    data = resp.json()
    return data["access_token"], int(data.get("expires_in", 3600))


def revoke_token(token: str) -> bool:
    """Best-effort revocation at Google. Returns True on success."""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                _REVOKE_URI,
                data={"token": token},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        return resp.status_code == 200
    except Exception as exc:
        logger.warning("Postmaster token revocation failed: %s", exc)
        return False
