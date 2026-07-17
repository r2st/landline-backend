"""Cold-email copy generation over the OpenRouter backend.

``ai_email_generator`` used to hold its own hardcoded Anthropic client and gate
on ``anthropic_api_key``. That combination is why Dialtone compose went dark
when the Anthropic credits ran out: the rest of the app failed over to
OpenRouter, this module could not, and every send quietly became a static
template. These tests pin the parts of that story that can regress —

* the active backend's credential is what gates AI vs template (not Anthropic's),
* the system prompt survives the trip on *both* backends, which carry it
  differently,
* email copy gets the larger free model, and that override never leaks a
  meaningless OpenRouter model id onto the Anthropic backend,
* sloppy-but-valid open-model replies (fences, preamble) still parse.

All network calls are mocked, so these run fully offline.
"""

import json
import os

import pytest

from app.config import Settings
from app.services import ai_email_generator as gen_module
from app.services import ai_service as ai_module

LEAD = {
    "first_name": "Dave",
    "company_name": "Precision Molding Co",
    "industry": "plastic injection molding",
    "title": "Plant Manager",
    "city": "Grand Rapids",
}

EMAIL_JSON = json.dumps(
    {"subject": "cavity drift on the night shift", "body": "Hi Dave, quick question about scrap."}
)


# ---------------------------------------------------------------------------
# Fakes
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
    reply_content: str = EMAIL_JSON
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


class _FakeAnthropicMessages:
    """Captures the kwargs the Anthropic SDK would have been called with."""

    captured: dict = {}

    async def create(self, **kwargs):
        _FakeAnthropicMessages.captured = kwargs
        block = type("Block", (), {"text": EMAIL_JSON})()
        return type("Message", (), {"content": [block]})()


class _FakeAnthropicClient:
    def __init__(self, *args, **kwargs):
        self.messages = _FakeAnthropicMessages()


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeAsyncClient.captured = {}
    _FakeAsyncClient.reply_content = EMAIL_JSON
    _FakeAsyncClient.raise_on_post = False
    _FakeAnthropicMessages.captured = {}
    yield


def _configure(monkeypatch, **overrides):
    """Point both modules' get_settings at one Settings and stub the transports."""
    kwargs = dict(
        ai_provider="auto",
        anthropic_api_key="",
        openrouter_api_key="or-test-key",
        openrouter_base_url="https://openrouter.ai/api/v1",
        # Deliberately two DIFFERENT live model ids: the override is only
        # observable if the general model isn't already the email model. The
        # shipped defaults happen to coincide (see app/config.py), so pinning them
        # here would make test_email_uses_the_larger_free_model pass vacuously.
        openrouter_model="meta-llama/llama-3.2-3b-instruct:free",
        openrouter_email_model="meta-llama/llama-3.3-70b-instruct:free",
        secret_key="test-secret-key-not-for-production",
    )
    kwargs.update(overrides)
    settings = Settings(**kwargs)
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(gen_module, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_module.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(ai_module.anthropic, "AsyncAnthropic", _FakeAnthropicClient)
    return settings


# ---------------------------------------------------------------------------
# The regression: an OpenRouter-only deployment must still write AI copy
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generates_over_openrouter_without_any_anthropic_key(monkeypatch):
    """The bug that made compose dark: no Anthropic key must NOT mean templates.

    Fails on the old code, which returned the static fallback here.
    """
    _configure(monkeypatch, anthropic_api_key="")

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is True
    assert result["subject"] == "cavity drift on the night shift"
    assert result["body"] == "Hi Dave, quick question about scrap."
    assert _FakeAsyncClient.captured["url"].endswith("/chat/completions")


@pytest.mark.asyncio
async def test_falls_back_to_template_when_no_backend_is_configured(monkeypatch):
    _configure(monkeypatch, anthropic_api_key="", openrouter_api_key="")

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is False
    # A template had no tone applied to it, so it must not claim one.
    assert result["tone"] is None
    assert _FakeAsyncClient.captured == {}


# ---------------------------------------------------------------------------
# System prompt carriage differs per backend
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_openrouter_carries_system_prompt_as_leading_system_message(monkeypatch):
    _configure(monkeypatch)

    await gen_module.generate_email(LEAD, template_type="first_touch")

    messages = _FakeAsyncClient.captured["json"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == gen_module.SYSTEM_PROMPT
    assert messages[1]["role"] == "user"
    # The lead's details ride in the user turn, not the system prompt.
    assert "Precision Molding Co" in messages[1]["content"]


@pytest.mark.asyncio
async def test_anthropic_carries_system_prompt_as_top_level_kwarg(monkeypatch):
    _configure(monkeypatch, ai_provider="anthropic", anthropic_api_key="sk-ant-test")

    await gen_module.generate_email(LEAD, template_type="first_touch")

    captured = _FakeAnthropicMessages.captured
    assert captured["system"] == gen_module.SYSTEM_PROMPT
    # Anthropic takes system out-of-band — it must not also be a message.
    assert [m["role"] for m in captured["messages"]] == ["user"]


# ---------------------------------------------------------------------------
# Model selection
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_email_uses_the_larger_free_model_not_the_scoring_model(monkeypatch):
    _configure(monkeypatch)

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert _FakeAsyncClient.captured["json"]["model"] == "meta-llama/llama-3.3-70b-instruct:free"
    # The reported model is the one actually used, not a hardcoded string.
    assert result["model"] == "meta-llama/llama-3.3-70b-instruct:free"


@pytest.mark.asyncio
async def test_openrouter_model_override_does_not_leak_onto_anthropic(monkeypatch):
    """An OpenRouter model id would be a 404 on the Anthropic API."""
    _configure(monkeypatch, ai_provider="anthropic", anthropic_api_key="sk-ant-test")

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert _FakeAnthropicMessages.captured["model"] == "claude-sonnet-4-6"
    assert result["model"] == "claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# Open models are sloppier than Claude about "return ONLY JSON"
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        f"```json\n{EMAIL_JSON}\n```",
        f"Here's the email:\n\n{EMAIL_JSON}",
        f"{EMAIL_JSON}\n\nLet me know if you'd like a different angle.",
    ],
    ids=["fenced", "preamble", "trailing-chatter"],
)
async def test_parses_replies_wrapped_in_fences_or_chatter(monkeypatch, reply):
    _configure(monkeypatch)
    _FakeAsyncClient.reply_content = reply

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is True
    assert result["subject"] == "cavity drift on the night shift"


@pytest.mark.asyncio
async def test_unparseable_reply_falls_back_to_template(monkeypatch):
    _configure(monkeypatch)
    _FakeAsyncClient.reply_content = "I'd be happy to help you write that email!"

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is False


@pytest.mark.asyncio
async def test_empty_body_falls_back_rather_than_sending_a_blank_email(monkeypatch):
    _configure(monkeypatch)
    _FakeAsyncClient.reply_content = json.dumps({"subject": "hi", "body": "   "})

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is False
    assert result["body"]


@pytest.mark.asyncio
async def test_transport_failure_falls_back_to_template(monkeypatch):
    """e.g. OpenRouter free-tier rate limit or an outage mid-send."""
    _configure(monkeypatch)
    _FakeAsyncClient.raise_on_post = True

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    assert result["ai_generated"] is False
    assert result["demo_link"]


# ---------------------------------------------------------------------------
# Tone survives the backend swap (the tone enum is meaningless if it doesn't)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tone_guidance_reaches_the_model_over_openrouter(monkeypatch):
    _configure(monkeypatch)

    result = await gen_module.generate_email(LEAD, template_type="first_touch", tone="empathetic")

    user_turn = _FakeAsyncClient.captured["json"]["messages"][1]["content"]
    assert gen_module.TONE_GUIDANCE["empathetic"] in user_turn
    assert result["tone"] == "empathetic"


# ---------------------------------------------------------------------------
# Live smoke test — opt-in, needs a real key and network
# ---------------------------------------------------------------------------
# Everything above mocks the transport, which means none of it can see the one
# failure that actually took compose down: a configured ":free" model id that
# OpenRouter no longer serves. A retired id 404s at request time and degrades to
# a static template, so the mocked suite stays green while every real send is a
# template. This is the only test that can catch that, and it needs a real key.
#
#   OPENROUTER_API_KEY=sk-or-... pytest tests/test_email_generator_openrouter.py -k live
#
# Worth running after any change to openrouter_model / openrouter_email_model,
# and periodically — OpenRouter retires free variants without notice.

_LIVE = pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="live OpenRouter test — set OPENROUTER_API_KEY to run",
)


@_LIVE
@pytest.mark.asyncio
async def test_live_configured_models_are_actually_served(monkeypatch):
    """Both configured model ids must exist AND return usable email JSON."""
    defaults = Settings(secret_key="test-secret-key-not-for-production")
    settings = Settings(
        ai_provider="openrouter",
        anthropic_api_key="",
        openrouter_api_key=os.environ["OPENROUTER_API_KEY"],
        openrouter_model=defaults.openrouter_model,
        openrouter_email_model=defaults.openrouter_email_model,
        secret_key="test-secret-key-not-for-production",
    )
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(gen_module, "get_settings", lambda: settings)

    result = await gen_module.generate_email(LEAD, template_type="first_touch")

    # ai_generated False here means the real call failed and we silently served a
    # template — the exact production symptom this whole change exists to fix.
    assert result["ai_generated"] is True, (
        f"live call to {settings.openrouter_email_model} fell back to a template — "
        "the model id may have been retired, or the free-tier quota is exhausted"
    )
    assert result["subject"] and result["body"]
