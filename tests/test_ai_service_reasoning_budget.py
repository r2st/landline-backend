"""A reasoning model must not spend the whole token budget thinking.

Production, 2026-07-24: every AI-composed email was silently falling back to the
static mail-merge. The cause was not an outage and not a bad prompt — OpenRouter
bills a reasoning model's chain-of-thought against the same ``max_tokens``
budget as its visible answer, and emits the reasoning *first*. Against this
repo's cold-email system prompt at the configured ``max_tokens=800``, the
default model (openai/gpt-oss-20b) spent 797 tokens reasoning and returned:

    finish_reason="length", content="", reasoning_tokens=797

``_chat_completion`` then did ``content or ""`` and returned an empty string as
if it were an answer. Every caller's JSON parse failed and degraded to a
template, so a completely broken model was indistinguishable from a healthy one
writing un-personalised copy. Measured fix: ``reasoning.effort="low"`` brings the
trace to ~105 tokens and returns 614 characters of usable JSON.

Two behaviours are pinned here, and they are independent:
  * the *request* carries a reasoning budget and a max_tokens floor;
  * an empty answer is treated as a failure, not laundered into a success.
"""

import httpx
import pytest

from app.config import Settings
from app.services import ai_service as ai_module
from app.services.ai_service import AIService


class _FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", "http://x"), response=self
            )

    def json(self):
        return self._data


def _completion(content, *, finish_reason="stop", reasoning_tokens=0):
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ],
        "usage": {"completion_tokens_details": {"reasoning_tokens": reasoning_tokens}},
    }


class _FakeAsyncClient:
    """Returns a queued response per call and records every request body."""

    responses: list = []
    requests: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeAsyncClient.requests.append(json)
        if _FakeAsyncClient.responses:
            return _FakeAsyncClient.responses.pop(0)
        return _FakeResponse(_completion('{"ok": true}'))


@pytest.fixture(autouse=True)
def _reset():
    _FakeAsyncClient.responses = []
    _FakeAsyncClient.requests = []
    yield


def _service(monkeypatch, **overrides):
    kwargs = dict(
        ai_provider="openrouter",
        anthropic_api_key="",
        openrouter_api_key="or-test-key",
        openrouter_model="openai/gpt-oss-20b:free",
        secret_key="test-secret-key-not-for-production",
    )
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeAsyncClient)
    return AIService()


# ---------------------------------------------------------------------------
# The request carries a reasoning budget
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_request_sends_a_reasoning_effort(monkeypatch):
    svc = _service(monkeypatch)
    await svc._chat("hi", max_tokens=2000)
    assert _FakeAsyncClient.requests[0]["reasoning"] == {"effort": "low"}


@pytest.mark.asyncio
async def test_reasoning_can_be_disabled_for_a_non_reasoning_model(monkeypatch):
    """A model with no chain-of-thought has no budget to cap; omit the param."""
    svc = _service(monkeypatch, openrouter_reasoning_effort="")
    await svc._chat("hi", max_tokens=2000)
    assert "reasoning" not in _FakeAsyncClient.requests[0]


@pytest.mark.asyncio
async def test_max_tokens_is_floored_so_the_trace_has_room(monkeypatch):
    """A caller asking for 256 would spend the whole allowance thinking."""
    svc = _service(monkeypatch, openrouter_min_max_tokens=1024)
    await svc._chat("hi", max_tokens=256)
    assert _FakeAsyncClient.requests[0]["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_floor_never_lowers_a_generous_caller(monkeypatch):
    svc = _service(monkeypatch, openrouter_min_max_tokens=1024)
    await svc._chat("hi", max_tokens=4096)
    assert _FakeAsyncClient.requests[0]["max_tokens"] == 4096


# ---------------------------------------------------------------------------
# An empty answer is a failure, not an answer
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_content_is_not_returned_as_success(monkeypatch):
    """The exact production shape: reasoning ate the budget, content is "".

    Returning "" here is what made the outage invisible for weeks — it reached
    the caller as a parse failure and degraded to a static template, which looks
    exactly like normal operation.
    """
    svc = _service(monkeypatch, openrouter_fallback_models="")
    _FakeAsyncClient.responses = [
        _FakeResponse(_completion("", finish_reason="length", reasoning_tokens=797))
    ]
    with pytest.raises(RuntimeError, match="empty content"):
        await svc._chat("hi", max_tokens=800)


@pytest.mark.asyncio
async def test_whitespace_only_content_is_also_a_failure(monkeypatch):
    svc = _service(monkeypatch, openrouter_fallback_models="")
    _FakeAsyncClient.responses = [_FakeResponse(_completion("   \n  "))]
    with pytest.raises(RuntimeError, match="empty content"):
        await svc._chat("hi", max_tokens=800)


@pytest.mark.asyncio
async def test_empty_content_falls_through_to_the_next_model(monkeypatch):
    """An exhausted-by-reasoning model is exactly what fallback is for."""
    svc = _service(
        monkeypatch, openrouter_fallback_models="openai/gpt-4o-mini"
    )
    _FakeAsyncClient.responses = [
        _FakeResponse(_completion("", finish_reason="length", reasoning_tokens=797)),
        _FakeResponse(_completion('{"subject": "s", "body": "b"}')),
    ]
    out = await svc._chat("hi", max_tokens=800)
    assert out == '{"subject": "s", "body": "b"}'
    assert [r["model"] for r in _FakeAsyncClient.requests] == [
        "openai/gpt-oss-20b:free",
        "openai/gpt-4o-mini",
    ]


@pytest.mark.asyncio
async def test_real_content_still_returns_unchanged(monkeypatch):
    """The guard must not disturb the healthy path."""
    svc = _service(monkeypatch)
    _FakeAsyncClient.responses = [_FakeResponse(_completion("hello there"))]
    assert await svc._chat("hi", max_tokens=800) == "hello there"


# ---------------------------------------------------------------------------
# The regression this whole file exists for
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_email_generation_survives_a_reasoning_model(monkeypatch):
    """End to end: the copywriter gets JSON back, not a template fallback.

    ``generate_email`` reports ``ai_generated: False`` when it falls back, which
    is precisely what production was returning for every send.
    """
    from app.services import ai_email_generator as gen

    svc = _service(monkeypatch)
    monkeypatch.setattr(gen, "AIService", lambda *a, **k: svc)
    _FakeAsyncClient.responses = [
        _FakeResponse(
            _completion(
                '{"subject": "cavity 4 drift", "body": "Hi Dave,\\n\\nwho notices '
                'first when a cavity starts drifting?"}',
                reasoning_tokens=105,
            )
        )
    ]

    out = await gen.generate_email(
        {"first_name": "Dave", "company_name": "Acme Molding",
         "industry": "plastic injection molding"},
        template_type="first_touch",
    )

    assert out["ai_generated"] is True, "fell back to the static template"
    assert out["subject"] == "cavity 4 drift"
    assert "cavity" in out["body"]
