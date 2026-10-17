"""Gemini calls billed to the org's own key, for the Discovery document features.

The org's key lives Fernet-encrypted in ``org_llm_keys`` (managed through
``/settings/llm-key``). These features deliberately do **not** fall back to the
platform LLM chain: with no org key the caller gets :class:`GeminiKeyMissing`,
which the routers turn into ``409 gemini_key_required`` so the UI can send the
user to Settings → API Keys.

Calls go through :func:`llm_providers.chat_completion` (rate limiting, retry,
empty-completion handling) with a single per-org provider and a breaker that is
private to this module, so one org's bad key can never trip the shared platform
``gemini`` breaker for everyone else.

The key never leaves this module: it is not logged, not returned, and not
included in the errors raised here.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.org_llm_key import OrgLlmKey
from app.services import crypto
from app.services.llm_providers import (
    KNOWN_PROVIDERS,
    CircuitBreaker,
    LLMChainExhausted,
    ProviderSpec,
    chat_completion,
)

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Keyed by the per-org provider name, so orgs never share breaker state.
_breaker = CircuitBreaker(threshold=3, cooldown_seconds=120.0)


class GeminiKeyMissing(Exception):
    """The org has not saved a Gemini API key."""


class GeminiError(Exception):
    """Gemini could not produce a usable answer. ``str()`` is safe to show users."""


async def get_org_gemini_key(db: AsyncSession, org_id: uuid.UUID) -> Optional[str]:
    """Decrypt the org's Gemini key, or ``None`` when it has none (or it's unreadable)."""
    row = (
        await db.execute(
            select(OrgLlmKey).where(
                OrgLlmKey.org_id == org_id, OrgLlmKey.provider == "gemini"
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        key = crypto.decrypt(row.key_encrypted).strip()
    except Exception:
        logger.error("Org %s Gemini key could not be decrypted", org_id)
        return None
    return key or None


async def has_org_gemini_key(db: AsyncSession, org_id: uuid.UUID) -> bool:
    return (await get_org_gemini_key(db, org_id)) is not None


def gemini_provider(org_id: uuid.UUID, api_key: str) -> ProviderSpec:
    settings = get_settings()
    extra: dict = {"response_format": {"type": "json_object"}}
    effort = (settings.discovery_gemini_reasoning_effort or "").strip()
    if effort:
        extra["reasoning_effort"] = effort
    return ProviderSpec(
        name=f"gemini-org-{org_id}",
        base_url=settings.gemini_base_url or KNOWN_PROVIDERS["gemini"]["base_url"],
        api_key=api_key,
        models=(settings.discovery_gemini_model,),
        extra_body=extra,
        supports_vision=True,
    )


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


def parse_json_answer(content: str, schema: type[T]) -> T:
    """Parse a model answer into ``schema``, tolerating a ```json fence."""
    text = _FENCE.sub("", content or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GeminiError("Gemini returned malformed JSON — try again.") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise GeminiError("Gemini's answer did not match the expected format — try again.") from exc


async def generate_json(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    system: str,
    prompt: str,
    schema: type[T],
    max_tokens: int = 8192,
    temperature: float | None = 0.2,
    timeout: float = 90.0,
) -> T:
    """Ask the org's Gemini for JSON matching ``schema``.

    Raises :class:`GeminiKeyMissing` when the org has no key and
    :class:`GeminiError` when Gemini fails or answers off-contract.
    """
    key = await get_org_gemini_key(db, org_id)
    if key is None:
        raise GeminiKeyMissing()
    try:
        content, _provider, model = await chat_completion(
            [{"role": "user", "content": prompt}],
            providers=[gemini_provider(org_id, key)],
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            timeout=timeout,
            breaker=_breaker,
        )
    except LLMChainExhausted as exc:
        # The message can carry the upstream status line; log it, but give the
        # user a fixed sentence (it never contains the key either way).
        logger.warning("Org %s Gemini call failed: %s", org_id, exc)
        raise GeminiError(
            "Gemini request failed. Check that the Gemini API key in "
            "Settings → API Keys is valid and has quota, then try again."
        ) from exc
    logger.info("Org %s Gemini answer from %s", org_id, model)
    return parse_json_answer(content, schema)


def reset_breaker() -> None:
    """Tests only."""
    _breaker.reset()
