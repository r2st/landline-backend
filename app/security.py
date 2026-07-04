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


def _secret_key() -> str:
    settings = get_settings()
    key = settings.secret_key or settings.token_encryption_key or "landline-dev-secret-change-me"
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
