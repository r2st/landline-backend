"""Password hashing and JWT helpers.

Passwords are hashed with bcrypt (via passlib). Access/refresh tokens are
signed JWTs (HS256) using ``settings.jwt_secret_key`` (which falls back to
``settings.secret_key`` when unset).

Tokens are stateless: they carry the user id (``sub``), role, and a ``type``
claim (``access`` or ``refresh``) so a refresh token can't be used where an
access token is expected. The frontend keeps the access token in memory only.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from passlib.context import CryptContext

from app.config import get_settings

# bcrypt via passlib. bcrypt truncates silently at 72 bytes; passlib handles the
# scheme details for us.
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


# ─── Password hashing ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """Return a bcrypt hash for ``password``."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True when ``password`` matches the stored ``password_hash``."""
    try:
        return pwd_context.verify(password, password_hash)
    except (ValueError, TypeError):
        # Malformed/empty hash — treat as a non-match rather than raising.
        return False


# ─── JWT tokens ──────────────────────────────────────────────────────────────

def _jwt_secret() -> str:
    settings = get_settings()
    return settings.jwt_secret_key or settings.secret_key or "landline-dev-secret-change-me"


def _create_token(
    *,
    subject: str,
    role: str,
    token_type: str,
    expires_delta: timedelta,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, _jwt_secret(), algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID | str, role: str) -> str:
    settings = get_settings()
    return _create_token(
        subject=str(user_id),
        role=role,
        token_type=ACCESS_TOKEN_TYPE,
        expires_delta=timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(user_id: uuid.UUID | str, role: str) -> str:
    settings = get_settings()
    return _create_token(
        subject=str(user_id),
        role=role,
        token_type=REFRESH_TOKEN_TYPE,
        expires_delta=timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT, returning its claims.

    Raises ``jwt.PyJWTError`` (or a subclass) when the token is invalid or
    expired — callers translate that into a 401.
    """
    settings = get_settings()
    return jwt.decode(token, _jwt_secret(), algorithms=[settings.jwt_algorithm])
