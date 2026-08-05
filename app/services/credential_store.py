"""Resolve platform credentials from the database, falling back to the environment.

A row in ``app_credentials`` overrides the environment for its key; no row means
the ``.env`` value stands. That ordering is the whole design — an install with
an empty table behaves exactly as it did before this module existed, and
clearing an override restores the deployed value rather than blanking it.

## Why this hydrates ``Settings`` instead of being a lookup function

Roughly fifteen call sites read ``get_settings().google_client_id`` — in
request handlers, in Celery tasks, and inside synchronous helpers that build
OAuth ``Credentials`` objects. Turning each into an awaited database read would
have meant threading a session into `GmailSender`, `google_calendar`,
`postmaster_oauth` and both ingestion sweeps, several of which have no session
in hand and no reason to grow one.

So the resolution happens **once**, and writes onto the ``Settings`` singleton
that every one of those call sites already reads. `get_settings` is
`lru_cache`d, so there is exactly one instance per process to write to.

``_ENV_BASELINE`` is what makes clearing work. The first hydrate snapshots what
the environment gave us, because by the second hydrate the attribute no longer
holds the environment's value — it holds whatever the previous hydrate wrote.
Without the snapshot, deleting an override would leave the overridden value in
place until the next restart, which is the failure mode you would discover
while trying to back out a bad credential.

## Propagation is not instant, and that is stated rather than hidden

Hydration is per-process. A worker that wrote a credential has it immediately;
the other API workers and the beat worker pick it up on their next refresh
(:func:`app.tasks.credentials.refresh_credentials`, every 5 minutes) or on
restart. For OAuth client credentials that is the right trade — they change
about once a year, and the alternative is a database read on every send.

What it means in practice: after rotating a client secret, an in-flight OAuth
callback on another worker can still use the old one for up to the refresh
interval. `GET /platform/credentials` reports `applied_at` per process so an
operator can see the propagation rather than guess at it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.app_credential import AppCredential
from app.services import crypto

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedCredential:
    """One credential an operator may set from the dashboard."""

    key: str
    #: Attribute on ``Settings`` this value feeds. Also names the env var
    #: (pydantic-settings maps ``GOOGLE_CLIENT_ID`` → ``google_client_id``).
    setting: str
    label: str
    category: str
    #: Secret values are never returned in full — only a masked hint. A client
    #: *id* is not a secret (it appears in the consent URL every user sees), so
    #: showing it in full is what lets an operator confirm which Google project
    #: is live without going to the server.
    secret: bool = True
    help: str = ""


# The allowlist. A key absent from here cannot be written, so the endpoint is a
# credential editor and not an arbitrary settings-write primitive.
MANAGED_CREDENTIALS: tuple[ManagedCredential, ...] = (
    ManagedCredential(
        key="google_client_id",
        setting="google_client_id",
        label="Google OAuth Client ID",
        category="Google",
        secret=False,
        help=(
            "From the Google Cloud project's OAuth 2.0 Client. Changing this "
            "invalidates every stored Gmail refresh token — each mailbox must "
            "be reconnected."
        ),
    ),
    ManagedCredential(
        key="google_client_secret",
        setting="google_client_secret",
        label="Google OAuth Client Secret",
        category="Google",
        help="Paired with the client ID above; both must come from the same project.",
    ),
    ManagedCredential(
        key="openrouter_api_key", setting="openrouter_api_key",
        label="OpenRouter API Key", category="AI providers",
    ),
    ManagedCredential(
        key="gemini_api_key", setting="gemini_api_key",
        label="Gemini API Key", category="AI providers",
    ),
    ManagedCredential(
        key="groq_api_key", setting="groq_api_key",
        label="Groq API Key", category="AI providers",
    ),
    ManagedCredential(
        key="cerebras_api_key", setting="cerebras_api_key",
        label="Cerebras API Key", category="AI providers",
    ),
    ManagedCredential(
        key="anthropic_api_key", setting="anthropic_api_key",
        label="Anthropic API Key", category="AI providers",
    ),
    ManagedCredential(
        key="apollo_api_key", setting="apollo_api_key",
        label="Apollo API Key", category="Enrichment",
    ),
    ManagedCredential(
        key="hunter_api_key", setting="hunter_api_key",
        label="Hunter.io API Key", category="Enrichment",
    ),
)

BY_KEY: dict[str, ManagedCredential] = {c.key: c for c in MANAGED_CREDENTIALS}

# What the environment gave us, captured before anything overwrites it. See the
# module docstring — this is what "clear the override" restores to.
_ENV_BASELINE: dict[str, str] = {}

#: When this process last applied database values. Reported by the API so an
#: operator can see propagation across workers instead of guessing.
_last_applied: Optional[datetime] = None


def _capture_env_baseline() -> None:
    if _ENV_BASELINE:
        return
    settings = get_settings()
    for cred in MANAGED_CREDENTIALS:
        _ENV_BASELINE[cred.key] = getattr(settings, cred.setting, "") or ""


def env_value(key: str) -> str:
    """The environment's value for *key*, whatever is currently applied."""
    _capture_env_baseline()
    return _ENV_BASELINE.get(key, "")


def mask(value: str) -> str:
    """A hint that identifies a credential without disclosing it.

    Short values are replaced outright rather than partially shown: revealing
    four of six characters is disclosure, not masking.
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]}"


async def load_overrides(db: AsyncSession) -> dict[str, str]:
    """Every stored override, decrypted.

    A row whose ciphertext will not decrypt is skipped rather than raised on:
    the Fernet key having been rotated must not take down startup, and the
    environment value is a working fallback. It is logged at ERROR because the
    operator's intended credential is not the one in use.
    """
    rows = (await db.execute(select(AppCredential))).scalars().all()
    out: dict[str, str] = {}
    for row in rows:
        if row.key not in BY_KEY:
            # A key retired from the registry. Left in the table (deleting an
            # operator's stored secret is not this function's call) and ignored.
            continue
        try:
            out[row.key] = crypto.decrypt(row.value_encrypted)
        except Exception:
            logger.error(
                "Stored credential %s could not be decrypted — falling back to "
                "the environment value. Was TOKEN_ENCRYPTION_KEY rotated?",
                row.key,
            )
    return out


def apply_overrides(overrides: dict[str, str]) -> None:
    """Write *overrides* onto the ``Settings`` singleton, restoring the rest."""
    global _last_applied
    _capture_env_baseline()
    settings = get_settings()
    for cred in MANAGED_CREDENTIALS:
        value = overrides.get(cred.key)
        # An override present but empty is not an override — an operator who
        # wants the environment value back deletes the row, and a blank string
        # saved by accident should not dark out a working provider key.
        if not value:
            value = _ENV_BASELINE.get(cred.key, "")
        setattr(settings, cred.setting, value)
    _last_applied = datetime.now(timezone.utc)


async def hydrate(db: AsyncSession) -> dict[str, str]:
    """Load overrides and apply them. Called at startup and on a beat."""
    overrides = await load_overrides(db)
    apply_overrides(overrides)
    if overrides:
        logger.info(
            "Applied %d credential override(s) from the database: %s",
            len(overrides), ", ".join(sorted(overrides)),
        )
    return overrides


def last_applied() -> Optional[datetime]:
    return _last_applied


async def set_credential(
    db: AsyncSession, key: str, value: str, updated_by: Optional[str] = None
) -> AppCredential:
    """Store (or replace) one override and apply it to this process.

    Raises ``KeyError`` for a key outside the registry and ``ValueError`` for an
    empty value — "set it to nothing" is ambiguous between *clear the override*
    and *there is no credential*, and the caller means the former, which is
    :func:`clear_credential`.
    """
    if key not in BY_KEY:
        raise KeyError(key)
    value = (value or "").strip()
    if not value:
        raise ValueError("value must not be empty")

    row = (
        await db.execute(select(AppCredential).where(AppCredential.key == key))
    ).scalar_one_or_none()  # unique on `key` — see the model
    if row is None:
        row = AppCredential(key=key)
        db.add(row)
    row.value_encrypted = crypto.encrypt(value)
    row.updated_by = updated_by
    await db.commit()
    await db.refresh(row)

    await hydrate(db)
    # Never the value, not even at DEBUG: logs are shipped and retained.
    logger.info("Credential %s updated by %s", key, updated_by or "unknown")
    return row


async def clear_credential(db: AsyncSession, key: str, cleared_by: Optional[str] = None) -> bool:
    """Drop the override for *key*, restoring the environment value."""
    if key not in BY_KEY:
        raise KeyError(key)
    result = await db.execute(delete(AppCredential).where(AppCredential.key == key))
    await db.commit()
    await hydrate(db)
    removed = bool(result.rowcount)
    if removed:
        logger.info("Credential %s cleared by %s", key, cleared_by or "unknown")
    return removed
