"""Symmetric encryption for OAuth tokens stored at rest.

Gmail refresh/access tokens are long-lived credentials that let Landline send
mail as a connected user, so they are never stored in plaintext. We encrypt
them with Fernet (AES-128-CBC + HMAC) using ``TOKEN_ENCRYPTION_KEY``.

Generate a key once and set it in the environment:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings

logger = logging.getLogger(__name__)


class TokenCryptoError(RuntimeError):
    """Raised when token encryption/decryption cannot be performed."""


@lru_cache()
def _fernet() -> Fernet:
    settings = get_settings()
    key = settings.token_encryption_key.strip()
    if not key:
        raise TokenCryptoError(
            "TOKEN_ENCRYPTION_KEY is not set — cannot encrypt/decrypt OAuth tokens. "
            "Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenCryptoError(
            "TOKEN_ENCRYPTION_KEY is not a valid Fernet key (expected a 32-byte "
            "url-safe base64 string)."
        ) from exc


def encrypt(plaintext: str) -> str:
    """Encrypt a string, returning a url-safe token string."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt`."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise TokenCryptoError(
            "Failed to decrypt token — the TOKEN_ENCRYPTION_KEY may have changed."
        ) from exc
