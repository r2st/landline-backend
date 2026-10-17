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
from dataclasses import dataclass, field
from typing import Generic, Optional, TypeVar

import httpx
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


def discovery_models() -> tuple[str, ...]:
    """``discovery_gemini_model`` as an ordered fallback list."""
    raw = get_settings().discovery_gemini_model or ""
    models = tuple(m.strip() for m in raw.split(",") if m.strip())
    return models or ("gemini-flash-latest",)


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
        models=discovery_models(),
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


@dataclass
class GroundedAnswer(Generic[T]):
    """A grounded Gemini answer plus the web pages Google Search grounded it on."""

    data: T
    sources: list[dict] = field(default_factory=list)
    model: str = ""


def _json_object_text(content: str) -> str:
    """The JSON object inside a grounded answer.

    Grounded calls cannot use JSON mode (Gemini rejects ``responseMimeType``
    alongside the search tool), so the model sometimes wraps the object in a
    sentence or a fence. Take the outermost ``{...}``.
    """
    text = _FENCE.sub("", content or "").strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


def _grounding_sources(candidate: dict) -> list[dict]:
    chunks = ((candidate.get("groundingMetadata") or {}).get("groundingChunks")) or []
    sources: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks:
        web = chunk.get("web") or {}
        uri = web.get("uri")
        if not uri or uri in seen:
            continue
        seen.add(uri)
        sources.append({"title": web.get("title") or "", "url": uri})
    return sources


async def generate_grounded_json(
    db: AsyncSession,
    org_id: uuid.UUID,
    *,
    system: str,
    prompt: str,
    schema: type[T],
    max_tokens: int = 8192,
    temperature: float | None = 0.2,
    timeout: float = 120.0,
) -> GroundedAnswer[T]:
    """Ask the org's Gemini, grounded on Google Search, for JSON matching ``schema``.

    Search grounding is only on the native ``generateContent`` API, so this is
    the one call here that does not go through ``chat_completion``. It keeps the
    same contract: the org's key only, no platform fallback, the private per-org
    breaker, and errors that are safe to show and never carry the key.
    """
    key = await get_org_gemini_key(db, org_id)
    if key is None:
        raise GeminiKeyMissing()

    settings = get_settings()
    name = f"gemini-org-{org_id}"
    if _breaker.is_open(name):
        raise GeminiError(
            "Gemini has failed several times in a row for this workspace — "
            "wait a couple of minutes, then try again."
        )

    generation: dict = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        generation["temperature"] = temperature
    budget = settings.discovery_search_thinking_budget
    if budget is not None and budget >= 0:
        generation["thinkingConfig"] = {"thinkingBudget": budget}
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": generation,
    }
    base = settings.gemini_native_base_url.rstrip("/")
    models = discovery_models()

    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            for i, model in enumerate(models):
                resp = await http.post(
                    f"{base}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                    json=payload,
                )
                # Both are properties of the model, not the key, so the next
                # model is a real chance (same rule as chat_completion): 404 is
                # a model retired or closed to accounts created after a cutoff;
                # 429 on the free tier is a per-model daily allowance (20/day
                # on some models), so a spent model says nothing of its sibling.
                if resp.status_code in (404, 429) and i < len(models) - 1:
                    logger.warning(
                        "Org %s grounded Gemini model %s returned %s — trying %s",
                        org_id, model, resp.status_code, models[i + 1],
                    )
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        _breaker.record_failure(name)
        logger.warning("Org %s grounded Gemini call returned HTTP %s", org_id, status)
        if status == 429:
            raise GeminiError(
                "Gemini rate limit or quota reached for your API key — wait a "
                "minute, or check the key's quota in Google AI Studio."
            ) from exc
        if status == 404:
            raise GeminiError(
                "None of the configured Gemini models is available to your API "
                "key. Ask your administrator to update DISCOVERY_GEMINI_MODEL."
            ) from exc
        if status in (400, 401, 403):
            raise GeminiError(
                "Gemini refused the request. Check that the Gemini API key in "
                "Settings → API Keys is valid and allows Google Search grounding."
            ) from exc
        raise GeminiError("Gemini is unavailable right now — try again shortly.") from exc
    except Exception as exc:  # transport error, timeout, non-JSON body
        _breaker.record_failure(name)
        logger.warning(
            "Org %s grounded Gemini call failed (%s)", org_id, type(exc).__name__
        )
        raise GeminiError("Gemini request failed — try again shortly.") from exc

    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    parts = (candidate.get("content") or {}).get("parts") or []
    content = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    if not content.strip():
        _breaker.record_failure(name)
        logger.warning(
            "Org %s grounded Gemini returned no text (finish_reason=%s, block=%s)",
            org_id,
            candidate.get("finishReason"),
            (data.get("promptFeedback") or {}).get("blockReason"),
        )
        raise GeminiError("Gemini returned an empty answer — try again.")

    _breaker.record_success(name)
    logger.info("Org %s grounded Gemini answer from %s", org_id, model)
    return GroundedAnswer(
        data=parse_json_answer(_json_object_text(content), schema),
        sources=_grounding_sources(candidate),
        model=model,
    )


def reset_breaker() -> None:
    """Tests only."""
    _breaker.reset()
