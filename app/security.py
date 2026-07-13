"""Password hashing and JWT token utilities.

Uses passlib[bcrypt] for password hashing and python-jose for JWTs.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

logger = logging.getLogger(__name__)

# --- Password hashing --------------------------------------------------------

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of *plain*."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return ``True`` if *plain* matches *hashed*."""
    return pwd_context.verify(plain, hashed)


# --- JWT tokens ---------------------------------------------------------------

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60        # 1 hour
REFRESH_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days


def get_configured_secret() -> str:
    """Return the configured signing secret, or ``""`` if none is set.

    Accepts any of the configured secrets, in priority order. ``getattr`` keeps
    this resilient to settings objects that omit some of these attributes. Unlike
    :func:`_secret_key`, this does not raise — used by the startup check to decide
    whether the app is allowed to boot.
    """
    settings = get_settings()
    return (
        getattr(settings, "secret_key", "")
        or getattr(settings, "jwt_secret_key", "")
        or getattr(settings, "token_encryption_key", "")
    )


def _secret_key() -> str:
    """Return the JWT signing secret, raising if none is configured.

    There is deliberately no hardcoded fallback: signing tokens with a
    well-known default secret would let anyone mint valid tokens. The app
    refuses to boot without a secret (see ``main.require_secret_key``), so in a
    correctly-configured deployment this never raises.
    """
    key = get_configured_secret()
    if not key:
        raise RuntimeError(
            "No signing secret configured. Set SECRET_KEY (or JWT_SECRET_KEY / "
            "TOKEN_ENCRYPTION_KEY) in the environment."
        )
    return key


def create_access_token(user_id: UUID, role: str, *, expires_delta: Optional[timedelta] = None) -> str:
    """Create a short-lived JWT access token."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, _secret_key(), algorithm=ALGORITHM)


def create_refresh_token(user_id: UUID, *, expires_delta: Optional[timedelta] = None) -> str:
    """Create a long-lived JWT refresh token."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES))
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, _secret_key(), algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises ``JWTError`` on failure."""
    return jwt.decode(token, _secret_key(), algorithms=[ALGORITHM])
