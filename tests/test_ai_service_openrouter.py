"""Tests for the AIService LLM backend abstraction.

Covers the OpenRouter (OpenAI-compatible) transport, backend auto-selection,
the Anthropic image-block → OpenAI image_url translation, and the graceful
mid-range fallback that keeps lead scoring working when the AI backend is
unavailable (e.g. Anthropic credits depleted). All network calls are mocked so
these run fully offline.
"""

import json

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
