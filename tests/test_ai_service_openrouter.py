"""Tests for the AIService LLM backend abstraction.

Covers the OpenRouter (OpenAI-compatible) transport, backend auto-selection,
the Anthropic image-block → OpenAI image_url translation, and the graceful
mid-range fallback that keeps lead scoring working when the AI backend is
unavailable (e.g. Anthropic credits depleted). All network calls are mocked so
these run fully offline.
"""

import json

import httpx
import pytest

from app.config import Settings
from app.services import ai_service as ai_module
from app.services.ai_service import AIService


# ---------------------------------------------------------------------------
# Fake httpx transport for the OpenRouter backend
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _FakeAsyncClient:
    """Records the last POST and returns a canned OpenAI-shaped completion."""

    captured: dict = {}
    reply_content: str = '{"ok": true}'
    raise_on_post: bool = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.captured = {"url": url, "headers": headers, "json": json}
        if _FakeAsyncClient.raise_on_post:
            raise RuntimeError("credit balance is too low")
        return _FakeResponse(
            {"choices": [{"message": {"content": _FakeAsyncClient.reply_content}}]}
        )


@pytest.fixture(autouse=True)
def _reset_fake():
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.reply_content = '{"ok": true}'
    _FakeAsyncClient.raise_on_post = False
    yield


def _openrouter_service(monkeypatch, **overrides):
    kwargs = dict(
        ai_provider="auto",
        anthropic_api_key="",
        openrouter_api_key="or-test-key",
        openrouter_base_url="https://openrouter.ai/api/v1",
        openrouter_model="meta-llama/llama-3.1-8b-instruct:free",
        secret_key="test-secret-key-not-for-production",
    )
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeAsyncClient)
    return AIService()


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------
def test_auto_selects_openrouter_when_key_present(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    assert svc.provider == "openrouter"
    assert svc.model == "meta-llama/llama-3.1-8b-instruct:free"


def test_auto_selects_anthropic_without_key(monkeypatch):
    settings = Settings(ai_provider="auto", anthropic_api_key="sk-ant", openrouter_api_key="")
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    svc = AIService()
    assert svc.provider == "anthropic"
    assert svc.model == "claude-sonnet-4-6"


def test_explicit_provider_override_forces_anthropic(monkeypatch):
    # Key present but operator pins Anthropic.
    svc = _openrouter_service(monkeypatch, ai_provider="anthropic")
    assert svc.provider == "anthropic"


# ---------------------------------------------------------------------------
# OpenRouter transport shape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_posts_openai_compatible_request(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    _FakeAsyncClient.reply_content = "hello there"

    text = await svc._chat("say hi", max_tokens=32, temperature=0.5)

    assert text == "hello there"
    cap = _FakeAsyncClient.captured
    assert cap["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert cap["headers"]["Authorization"] == "Bearer or-test-key"
    body = cap["json"]
    assert body["model"] == "meta-llama/llama-3.1-8b-instruct:free"
    assert body["max_tokens"] == 32
    assert body["temperature"] == 0.5
    assert body["messages"] == [{"role": "user", "content": "say hi"}]


@pytest.mark.asyncio
async def test_score_lead_parses_openrouter_json(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    _FakeAsyncClient.reply_content = json.dumps(
        {"score": 82, "grade": "A", "reasoning": "great fit", "priority": "high"}
    )

    result = await svc.score_lead(
        company_name="Acme Plastics",
        industry="plastics",
        employee_count=120,
        location="Ohio, US",
    )

    assert "error" not in result
    assert result["score"] == 82
    assert result["grade"] == "A"


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_score_lead_degrades_to_midrange_on_backend_failure(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    _FakeAsyncClient.raise_on_post = True  # simulate provider outage / no credits

    result = await svc.score_lead(
        company_name="Acme Plastics",
        industry="plastics",
        employee_count=120,
        location="Ohio, US",
    )

    # No "error" key so the auto-scoring batch still records the lead.
    assert "error" not in result
    assert result["score"] == 50
    assert result["grade"] == "C"
    assert result["degraded"] is True


@pytest.mark.asyncio
async def test_score_lead_degrades_when_no_score_field(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    _FakeAsyncClient.reply_content = json.dumps({"unexpected": "shape"})

    result = await svc.score_lead(
        company_name="Acme", industry="plastics", employee_count=None, location="US"
    )
    assert result["degraded"] is True
    assert result["score"] == 50


# ---------------------------------------------------------------------------
# Vision message translation
# ---------------------------------------------------------------------------
def test_to_openai_message_translates_image_block():
    msg = {
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}},
            {"type": "text", "text": "read this"},
        ],
    }
    out = AIService._to_openai_message(msg)
    assert out["role"] == "user"
    assert out["content"][0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAA"},
    }
    assert out["content"][1] == {"type": "text", "text": "read this"}


def test_to_openai_message_passes_through_plain_string():
    out = AIService._to_openai_message({"role": "user", "content": "hi"})
    assert out == {"role": "user", "content": "hi"}


# ---------------------------------------------------------------------------
# Anthropic backend still routes through the shared helper
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_anthropic_backend_chat(monkeypatch):
    settings = Settings(ai_provider="anthropic", anthropic_api_key="sk-ant")
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    svc = AIService()

    class _Block:
        text = "anthropic reply"

    class _Msg:
        content = [_Block()]

    async def fake_create(**kwargs):
        assert kwargs["model"] == "claude-sonnet-4-6"
        return _Msg()

    monkeypatch.setattr(svc.client.messages, "create", fake_create)

    text = await svc._chat("hello")
    assert text == "anthropic reply"


@pytest.mark.asyncio
async def test_score_lead_degrades_on_anthropic_credit_exhaustion(monkeypatch):
    """The real production outage: Anthropic backend, NO OpenRouter key, and
    ``messages.create`` raising "credit balance is too low". ``score_lead`` must
    swallow it and return the neutral mid-range score so the Dialtone
    auto-scoring batch keeps recording leads instead of scoring 0/N.
    """
    settings = Settings(ai_provider="anthropic", anthropic_api_key="sk-ant", openrouter_api_key="")
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    svc = AIService()
    assert svc.provider == "anthropic"  # no OpenRouter fallback available

    async def fake_create(**kwargs):
        # Mirror the Anthropic SDK error seen in production logs.
        raise RuntimeError("Your credit balance is too low to access the Anthropic API")

    monkeypatch.setattr(svc.client.messages, "create", fake_create)

    result = await svc.score_lead(
        company_name="Acme Plastics",
        industry="plastics",
        employee_count=120,
        location="Ohio, US",
    )

    # No "error" key -> the enrichment task persists it and increments `scored`.
    assert "error" not in result
    assert result["score"] == 50
    assert result["grade"] == "C"
    assert result["priority"] == "medium"
    assert result["degraded"] is True


# ---------------------------------------------------------------------------
# Config-default model guard (regression: OpenRouter 404 on analyze_thread)
# ---------------------------------------------------------------------------
# The production symptom was conversation.analyze_thread 404ing on every call:
# get_ai_service() built an AIService on the OpenRouter backend using the config
# default openrouter_model, and that default id had been retired by OpenRouter.
# A retired id 404s at request time, which the mocked suite above cannot see, so
# these offline guards pin the defaults away from every id known to have been
# retired and caused this exact outage. When OpenRouter retires the current id,
# add it here and bump the default in app/config.py (verify against
# https://openrouter.ai/api/v1/models first).
_RETIRED_OPENROUTER_MODELS = frozenset(
    {
        "meta-llama/llama-3.1-8b-instruct:free",
        "meta-llama/llama-3.2-3b-instruct:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "openai/gpt-oss-120b:free",
    }
)


def test_default_openrouter_models_are_not_retired():
    """Neither config default may be an id OpenRouter has retired (→ 404)."""
    defaults = Settings(secret_key="test-secret-key-not-for-production")
    assert defaults.openrouter_model not in _RETIRED_OPENROUTER_MODELS, (
        f"default openrouter_model {defaults.openrouter_model!r} is a retired "
        "OpenRouter id — it 404s every scoring / analyze_thread call"
    )
    assert defaults.openrouter_email_model not in _RETIRED_OPENROUTER_MODELS, (
        f"default openrouter_email_model {defaults.openrouter_email_model!r} is a "
        "retired OpenRouter id — cold-email compose silently falls back to a template"
    )


def test_ai_service_uses_config_default_model_on_openrouter(monkeypatch):
    """get_ai_service()'s backend (used by conversation.analyze_thread) sends the
    config default openrouter_model to OpenRouter — the fixed, served id, never a
    retired one."""
    defaults = Settings(secret_key="test-secret-key-not-for-production")
    svc = _openrouter_service(monkeypatch, openrouter_model=defaults.openrouter_model)
    assert svc.provider == "openrouter"
    assert svc.model == defaults.openrouter_model
    assert svc.model not in _RETIRED_OPENROUTER_MODELS


# ---------------------------------------------------------------------------
# Free-tier 429 fallback
# ---------------------------------------------------------------------------
# A ":free" model answers 429 once the account's shared daily quota is spent.
# With no fallback that silently dropped every AI call back to its degradation
# path — which is how the outreach engine shipped un-personalised mail-merge copy
# for weeks while every dashboard read "healthy".
class _StatusResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=httpx.Request("POST", "https://x"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._data


class _SequencedClient:
    """Answers per-model: ``responses`` maps model id → status code."""

    responses: dict = {}
    models_tried: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        model = json["model"]
        _SequencedClient.models_tried.append(model)
        status = _SequencedClient.responses.get(model, 200)
        if status != 200:
            return _StatusResponse(status)
        return _StatusResponse(
            200, {"choices": [{"message": {"content": "composed copy"}}]}
        )


@pytest.fixture
def _sequenced():
    """Reset the per-model transport. Install it *after* ``_openrouter_service``,
    which patches ``AsyncClient`` itself and would otherwise clobber this."""
    _SequencedClient.responses = {}
    _SequencedClient.models_tried = []
    return _SequencedClient


def _install_sequenced(monkeypatch):
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _SequencedClient)


def test_model_chain_is_primary_then_fallbacks(monkeypatch):
    svc = _openrouter_service(
        monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini, anthropic/claude-haiku-4.5"
    )
    assert svc._model_chain() == [
        "meta-llama/llama-3.1-8b-instruct:free",
        "openai/gpt-4o-mini",
        "anthropic/claude-haiku-4.5",
    ]


def test_model_chain_dedupes_and_ignores_blanks(monkeypatch):
    svc = _openrouter_service(
        monkeypatch,
        openrouter_fallback_models="meta-llama/llama-3.1-8b-instruct:free, ,openai/gpt-4o-mini",
    )
    # The primary listed again must not be retried after it already 429'd.
    assert svc._model_chain() == [
        "meta-llama/llama-3.1-8b-instruct:free",
        "openai/gpt-4o-mini",
    ]


def test_model_chain_is_just_the_primary_when_unconfigured(monkeypatch):
    svc = _openrouter_service(monkeypatch)
    assert svc._model_chain() == ["meta-llama/llama-3.1-8b-instruct:free"]


@pytest.mark.asyncio
async def test_429_on_free_model_falls_through_to_the_paid_one(monkeypatch, _sequenced):
    svc = _openrouter_service(monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini")
    _install_sequenced(monkeypatch)
    _sequenced.responses = {"meta-llama/llama-3.1-8b-instruct:free": 429}

    out = await svc._chat_completion([{"role": "user", "content": "write a subject"}])

    assert out == "composed copy"
    assert _sequenced.models_tried == [
        "meta-llama/llama-3.1-8b-instruct:free",
        "openai/gpt-4o-mini",
    ]


@pytest.mark.asyncio
async def test_5xx_also_falls_through(monkeypatch, _sequenced):
    svc = _openrouter_service(monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini")
    _install_sequenced(monkeypatch)
    _sequenced.responses = {"meta-llama/llama-3.1-8b-instruct:free": 503}

    assert await svc._chat_completion([{"role": "user", "content": "x"}]) == "composed copy"


@pytest.mark.asyncio
async def test_4xx_that_is_not_429_raises_without_burning_the_fallback(
    monkeypatch, _sequenced
):
    """A bad request is our bug — retrying a paid model would just cost money."""
    svc = _openrouter_service(monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini")
    _install_sequenced(monkeypatch)
    _sequenced.responses = {"meta-llama/llama-3.1-8b-instruct:free": 400}

    with pytest.raises(httpx.HTTPStatusError):
        await svc._chat_completion([{"role": "user", "content": "x"}])
    assert _sequenced.models_tried == ["meta-llama/llama-3.1-8b-instruct:free"]


@pytest.mark.asyncio
async def test_every_model_rate_limited_raises_for_the_callers_degradation_path(
    monkeypatch, _sequenced
):
    svc = _openrouter_service(monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini")
    _install_sequenced(monkeypatch)
    _sequenced.responses = {
        "meta-llama/llama-3.1-8b-instruct:free": 429,
        "openai/gpt-4o-mini": 429,
    }

    with pytest.raises(httpx.HTTPStatusError):
        await svc._chat_completion([{"role": "user", "content": "x"}])
    assert len(_sequenced.models_tried) == 2
