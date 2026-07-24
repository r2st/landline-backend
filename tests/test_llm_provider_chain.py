"""Tests for the multi-provider LLM fallback chain.

The property under test is the one the product actually depends on: an AI call
must keep producing real copy while any provider is up, and must only reach the
static template when every provider is genuinely unavailable. Both AI outages
this codebase has had were invisible precisely because a dead backend and a
healthy one produced the same observable result — a plausible-looking email —
so these tests assert on *which provider served the request*, not just that
something came back.

All network calls are mocked; these run fully offline.
"""

import pytest

from app.config import Settings
from app.services import ai_service as ai_module
from app.services.ai_service import AIService
from app.services import llm_providers as chain_module
from app.services.llm_providers import (
    CircuitBreaker,
    LLMChainExhausted,
    build_provider_chain,
    chat_completion,
    any_llm_available,
    chain_client,
    has_any_provider_key,
    llm_status,
    reset_breaker,
)


# ---------------------------------------------------------------------------
# A fake httpx client that answers per-host, so a test can say
# "OpenRouter is out of quota but Groq is fine" and mean it.
# ---------------------------------------------------------------------------
class _Response:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self
            )

    def json(self):
        return self._data


class _HostRoutedClient:
    """Answers based on which provider host the request went to.

    ``behaviour`` maps a host substring to either an int status code, the string
    ``"boom"`` (transport error), ``"empty"`` (a 200 with empty content), or a
    reply string to return as the completion.
    """

    behaviour: dict = {}
    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers=None, json=None):
        model = (json or {}).get("model")
        _HostRoutedClient.calls.append({"url": url, "model": model, "json": json,
                                        "headers": headers})
        for host, outcome in _HostRoutedClient.behaviour.items():
            if host in url:
                if outcome == "boom":
                    raise RuntimeError("connection reset")
                if outcome == "empty":
                    return _Response(200, {"choices": [{"message": {"content": ""},
                                                        "finish_reason": "length"}]})
                if isinstance(outcome, int):
                    return _Response(outcome, {"error": {"code": outcome}})
                return _Response(200, {"choices": [{"message": {"content": outcome}}]})
        return _Response(200, {"choices": [{"message": {"content": "default reply"}}]})

    @classmethod
    def hosts_tried(cls) -> list[str]:
        out = []
        for call in cls.calls:
            for host in ("openrouter.ai", "generativelanguage", "groq.com", "cerebras.ai"):
                if host in call["url"]:
                    out.append(host)
        return out


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    _HostRoutedClient.behaviour = {}
    _HostRoutedClient.calls = []
    monkeypatch.setattr(chain_module.httpx, "AsyncClient", _HostRoutedClient)
    reset_breaker()
    yield
    reset_breaker()


def _settings(**overrides) -> Settings:
    kwargs = dict(
        ai_provider="auto",
        anthropic_api_key="",
        openrouter_api_key="or-key",
        openrouter_model="openai/gpt-oss-20b:free",
        openrouter_reasoning_effort="",
        gemini_api_key="gemini-key",
        groq_api_key="groq-key",
        cerebras_api_key="cerebras-key",
        secret_key="test-secret-key-not-for-production",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _service(monkeypatch, settings: Settings) -> AIService:
    monkeypatch.setattr(ai_module, "get_settings", lambda: settings)
    monkeypatch.setattr(chain_module, "get_breaker", lambda *a, **k: CircuitBreaker(3, 300))
    return AIService()


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------
def test_chain_is_built_in_the_configured_order():
    chain = build_provider_chain(_settings())
    assert [s.name for s in chain] == ["openrouter", "gemini", "groq", "cerebras"]


def test_unkeyed_providers_are_dropped_not_attempted():
    """An unconfigured provider is a config fact, not an outage.

    Attempting it would burn a request slot and trip its own circuit breaker for
    a problem no retry can fix.
    """
    chain = build_provider_chain(_settings(groq_api_key="", cerebras_api_key=""))
    assert [s.name for s in chain] == ["openrouter", "gemini"]


def test_chain_order_is_configurable():
    chain = build_provider_chain(_settings(llm_provider_chain="groq,gemini"))
    assert [s.name for s in chain] == ["groq", "gemini"]


def test_unknown_provider_names_are_ignored():
    chain = build_provider_chain(_settings(llm_provider_chain="groq,nonesuch,gemini"))
    assert [s.name for s in chain] == ["groq", "gemini"]


def test_each_provider_carries_its_own_endpoint_and_model():
    by_name = {s.name: s for s in build_provider_chain(_settings())}
    assert by_name["gemini"].endpoint == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert by_name["gemini"].models == ("gemini-2.0-flash",)
    assert by_name["groq"].endpoint == "https://api.groq.com/openai/v1/chat/completions"
    assert by_name["groq"].models == ("llama-3.3-70b-versatile",)
    assert by_name["cerebras"].endpoint == "https://api.cerebras.ai/v1/chat/completions"
    assert by_name["cerebras"].models == ("llama-3.3-70b",)


def test_has_any_provider_key_sees_a_gemini_only_deployment():
    """The old is_configured asked only about OpenRouter, so this shape — which
    is exactly what a fresh Gemini key looks like before OpenRouter is topped up
    — answered every compose with a static template without trying anything."""
    assert has_any_provider_key(
        _settings(openrouter_api_key="", groq_api_key="", cerebras_api_key="")
    )
    assert not has_any_provider_key(
        _settings(openrouter_api_key="", gemini_api_key="", groq_api_key="",
                  cerebras_api_key="")
    )


# ---------------------------------------------------------------------------
# Failover
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_healthy_provider_serves_and_the_rest_are_never_called():
    _HostRoutedClient.behaviour = {"openrouter.ai": "real copy"}
    content, provider, model = await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    assert (content, provider) == ("real copy", "openrouter")
    assert _HostRoutedClient.hosts_tried() == ["openrouter.ai"]


@pytest.mark.asyncio
async def test_quota_exhausted_openrouter_falls_through_to_gemini():
    """The exact production condition: OpenRouter's free tier answers 429 to
    everything for the rest of the day."""
    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "gemini copy"}
    content, provider, _ = await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    assert (content, provider) == ("gemini copy", "gemini")
    assert _HostRoutedClient.hosts_tried() == ["openrouter.ai", "generativelanguage"]


@pytest.mark.asyncio
async def test_the_chain_walks_all_the_way_to_cerebras():
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429,
        "generativelanguage": 429,
        "groq.com": 503,
        "cerebras.ai": "cerebras copy",
    }
    content, provider, _ = await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    assert (content, provider) == ("cerebras copy", "cerebras")
    assert _HostRoutedClient.hosts_tried() == [
        "openrouter.ai", "generativelanguage", "groq.com", "cerebras.ai",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [401, 402, 429, 500, "boom", "empty"])
async def test_every_flavour_of_provider_failure_crosses_to_the_next(failure):
    """401 (rotated key), 402 (no credits), 429 (quota), 5xx, transport error and
    an empty completion have all taken a provider down here. None of them is a
    reason to give up on a *different* provider."""
    _HostRoutedClient.behaviour = {"openrouter.ai": failure, "generativelanguage": "ok"}
    content, provider, _ = await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    assert (content, provider) == ("ok", "gemini")


@pytest.mark.asyncio
async def test_an_empty_completion_is_a_failure_not_an_answer():
    """Returning "" made the last outage invisible: every caller's JSON parse
    failed and degraded to a template, so a broken model looked exactly like a
    healthy one writing un-personalised copy."""
    _HostRoutedClient.behaviour = {
        "openrouter.ai": "empty",
        "generativelanguage": "empty",
        "groq.com": "empty",
        "cerebras.ai": "empty",
    }
    with pytest.raises(LLMChainExhausted):
        await chat_completion(
            [{"role": "user", "content": "x"}],
            providers=build_provider_chain(_settings()),
            max_tokens=512,
            breaker=CircuitBreaker(3, 300),
        )


@pytest.mark.asyncio
async def test_all_providers_down_raises_so_the_caller_can_use_its_template():
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429, "generativelanguage": 429,
        "groq.com": 429, "cerebras.ai": 429,
    }
    with pytest.raises(LLMChainExhausted):
        await chat_completion(
            [{"role": "user", "content": "x"}],
            providers=build_provider_chain(_settings()),
            max_tokens=512,
            breaker=CircuitBreaker(3, 300),
        )


@pytest.mark.asyncio
async def test_no_configured_provider_raises_rather_than_returning_empty_text():
    with pytest.raises(LLMChainExhausted):
        await chat_completion(
            [{"role": "user", "content": "x"}], providers=[], max_tokens=512
        )


# ---------------------------------------------------------------------------
# Request shape per provider
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_each_provider_gets_its_own_bearer_key():
    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}
    await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    keys = [c["headers"]["Authorization"] for c in _HostRoutedClient.calls]
    assert keys == ["Bearer or-key", "Bearer gemini-key"]


@pytest.mark.asyncio
async def test_reasoning_effort_is_sent_to_openrouter_only():
    """``reasoning`` is an OpenRouter extension. Gemini/Groq/Cerebras would
    reject or ignore it, and the budget problem it solves is OpenRouter's."""
    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}
    await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(_settings(openrouter_reasoning_effort="low")),
        max_tokens=512,
        breaker=CircuitBreaker(3, 300),
    )
    assert _HostRoutedClient.calls[0]["json"]["reasoning"] == {"effort": "low"}
    assert "reasoning" not in _HostRoutedClient.calls[1]["json"]


@pytest.mark.asyncio
async def test_system_prompt_becomes_a_leading_system_message():
    _HostRoutedClient.behaviour = {"openrouter.ai": "ok"}
    await chat_completion(
        [{"role": "user", "content": "write copy"}],
        providers=build_provider_chain(_settings()),
        max_tokens=512,
        system="You are Suman.",
        breaker=CircuitBreaker(3, 300),
    )
    messages = _HostRoutedClient.calls[0]["json"]["messages"]
    assert messages[0] == {"role": "system", "content": "You are Suman."}
    assert messages[1]["content"] == "write copy"


@pytest.mark.asyncio
async def test_image_requests_skip_text_only_providers():
    """Groq and Cerebras run text-only Llama here. Handing them an image yields
    a confidently wrong extraction rather than an error — worse than skipping,
    because a garbage business-card read looks exactly like a real one."""
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429,
        "generativelanguage": 429,
        "groq.com": "should never be asked",
        "cerebras.ai": "should never be asked",
    }
    messages = [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            {"type": "text", "text": "read this card"},
        ],
    }]
    with pytest.raises(LLMChainExhausted):
        await chat_completion(
            messages,
            providers=build_provider_chain(_settings()),
            max_tokens=512,
            breaker=CircuitBreaker(3, 300),
        )
    assert _HostRoutedClient.hosts_tried() == ["openrouter.ai", "generativelanguage"]
    # The image survived translation into the OpenAI block form on the way.
    blocks = _HostRoutedClient.calls[0]["json"]["messages"][0]["content"]
    assert blocks[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def test_breaker_opens_after_the_threshold_and_not_before():
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=300)
    breaker.record_failure("groq")
    breaker.record_failure("groq")
    assert not breaker.is_open("groq")
    breaker.record_failure("groq")
    assert breaker.is_open("groq")


def test_a_success_resets_the_consecutive_failure_count():
    """Consecutive, not cumulative: a provider that fails twice, recovers, then
    fails twice again is flaky, not down."""
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=300)
    breaker.record_failure("groq")
    breaker.record_failure("groq")
    breaker.record_success("groq")
    breaker.record_failure("groq")
    breaker.record_failure("groq")
    assert not breaker.is_open("groq")


def test_breaker_closes_itself_once_the_cooldown_elapses(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(chain_module.time, "monotonic", lambda: clock["t"])
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=300)

    breaker.record_failure("groq")
    assert breaker.is_open("groq")

    clock["t"] += 299
    assert breaker.is_open("groq")

    clock["t"] += 2  # past the 300s cooldown
    assert not breaker.is_open("groq")


@pytest.mark.asyncio
async def test_an_open_breaker_skips_the_provider_entirely():
    """The point of the breaker: a provider that is out of quota for the day
    must stop costing a full timeout on every email of a batch send."""
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=300)
    providers = build_provider_chain(_settings())
    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}

    await chat_completion([{"role": "user", "content": "x"}], providers=providers,
                          max_tokens=512, breaker=breaker)
    assert breaker.is_open("openrouter")

    _HostRoutedClient.calls = []
    content, provider, _ = await chat_completion(
        [{"role": "user", "content": "x"}], providers=providers,
        max_tokens=512, breaker=breaker,
    )
    assert (content, provider) == ("ok", "gemini")
    assert _HostRoutedClient.hosts_tried() == ["generativelanguage"]


@pytest.mark.asyncio
async def test_a_recovered_provider_is_used_again_after_the_cooldown(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(chain_module.time, "monotonic", lambda: clock["t"])
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=300)
    providers = build_provider_chain(_settings())

    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}
    await chat_completion([{"role": "user", "content": "x"}], providers=providers,
                          max_tokens=512, breaker=breaker)
    assert breaker.is_open("openrouter")

    clock["t"] += 301
    _HostRoutedClient.behaviour = {"openrouter.ai": "quota reset"}
    content, provider, _ = await chat_completion(
        [{"role": "user", "content": "x"}], providers=providers,
        max_tokens=512, breaker=breaker,
    )
    assert (content, provider) == ("quota reset", "openrouter")


@pytest.mark.asyncio
async def test_all_breakers_open_raises_rather_than_silently_returning_nothing():
    breaker = CircuitBreaker(threshold=1, cooldown_seconds=300)
    for name in ("openrouter", "gemini", "groq", "cerebras"):
        breaker.record_failure(name)
    with pytest.raises(LLMChainExhausted):
        await chat_completion(
            [{"role": "user", "content": "x"}],
            providers=build_provider_chain(_settings()),
            max_tokens=512,
            breaker=breaker,
        )


# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_status_reports_who_served_and_who_is_tripped(monkeypatch):
    settings = _settings(cerebras_api_key="")
    monkeypatch.setattr(chain_module, "get_settings", lambda: settings, raising=False)
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}
    await chat_completion(
        [{"role": "user", "content": "x"}],
        providers=build_provider_chain(settings),
        max_tokens=512,
        breaker=chain_module.get_breaker(settings),
    )

    status = llm_status()
    assert [p["provider"] for p in status["configured_chain"]] == [
        "openrouter", "gemini", "groq",
    ]
    assert status["unconfigured"] == ["cerebras"]
    assert status["requests_served"] == {"gemini": 1}
    assert status["requests_failed"] == {"openrouter": 1}
    assert status["last_provider"] == "gemini"
    assert status["last_model"] == "gemini-2.0-flash"
    assert "or-key" not in str(status) and "gemini-key" not in str(status)


# ---------------------------------------------------------------------------
# AIService integration — the callers see the chain, not the provider
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_providers_endpoint_reports_the_chain_and_needs_auth(client, unauth_client):
    resp = await unauth_client.get("/api/v1/ai/providers")
    assert resp.status_code == 401

    resp = await client.get("/api/v1/ai/providers")
    assert resp.status_code == 200
    body = resp.json()
    assert "configured_chain" in body
    assert "circuit_breakers" in body
    assert "last_provider" in body


@pytest.mark.asyncio
async def test_ai_service_composes_over_gemini_when_openrouter_is_spent(monkeypatch):
    """The whole point, end to end: OpenRouter out of quota, Anthropic out of
    credits, and the email still gets written by an LLM."""
    svc = _service(monkeypatch, _settings(anthropic_api_key=""))
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429,
        "generativelanguage": '{"subject": "quick question", "body": "real copy"}',
    }
    result = await svc.complete_json("write me an email")
    assert result["body"] == "real copy"
    assert svc.last_provider == "gemini"
    assert svc.last_model == "gemini-2.0-flash"


@pytest.mark.asyncio
async def test_reply_classification_survives_an_openrouter_outage(monkeypatch):
    """Reply routing is the other half. Degrading it to keyword-only silently
    turns every ambiguous reply into 'unknown' and buries real interest."""
    svc = _service(monkeypatch, _settings(anthropic_api_key=""))
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429,
        "generativelanguage":
            '{"category": "interested", "confidence": 0.9, "reason": "wants a call"}',
    }
    result = await svc.classify_reply(
        subject="Re: quoting", body_text="Sure, can you do Thursday?"
    )
    assert result["category"] == "interested"
    assert result["source"] == "ai"
    assert svc.last_provider == "gemini"


@pytest.mark.asyncio
async def test_ai_service_still_crosses_to_anthropic_after_the_whole_chain(monkeypatch):
    """The Anthropic hop is the last LLM before the template, not the first."""
    settings = _settings(anthropic_api_key="sk-ant-test")
    svc = _service(monkeypatch, settings)
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429, "generativelanguage": 429,
        "groq.com": 429, "cerebras.ai": 429,
    }

    async def _fake_anthropic(messages, **kwargs):
        return "anthropic copy"

    monkeypatch.setattr(svc, "_anthropic_completion", _fake_anthropic)
    content = await svc._chat_completion([{"role": "user", "content": "x"}])
    assert content == "anthropic copy"
    assert svc.last_provider == "anthropic"


@pytest.mark.asyncio
async def test_a_gemini_only_deployment_is_configured_and_serves(monkeypatch):
    """Auto-selection must see a chain provider that isn't OpenRouter."""
    svc = _service(
        monkeypatch,
        _settings(openrouter_api_key="", groq_api_key="", cerebras_api_key=""),
    )
    assert svc.provider == "openrouter"  # i.e. the OpenAI-compatible chain
    assert svc.is_configured
    _HostRoutedClient.behaviour = {"generativelanguage": '{"ok": true}'}
    assert await svc.complete_json("x") == {"ok": True}
    assert svc.last_provider == "gemini"


@pytest.mark.asyncio
async def test_total_outage_degrades_to_the_callers_fallback(monkeypatch):
    """Last resort, and only the last resort: complete_json reports the error so
    ai_email_generator returns its static template."""
    svc = _service(monkeypatch, _settings(anthropic_api_key=""))
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429, "generativelanguage": 429,
        "groq.com": 429, "cerebras.ai": 429,
    }
    result = await svc.complete_json("write me an email")
    assert "error" in result


# ---------------------------------------------------------------------------
# The Anthropic-shaped adapter
# ---------------------------------------------------------------------------
# Nine services (sales coach, orchestrator, meeting/relationship/competitive
# intelligence, sentiment, script generator, investor outreach) call
# ``client.messages.create(...)`` directly and read ``message.content[0].text``.
# They were Anthropic-only, so they are the features that went *fully* dark when
# those credits ran out rather than degrading to a template. ``chain_client``
# keeps that call shape and serves it from the chain; these tests pin both
# halves of that claim — the shape, and the fact that the chain really ran.
@pytest.mark.asyncio
async def test_adapter_serves_from_the_chain_in_the_anthropic_shape(monkeypatch):
    settings = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    _HostRoutedClient.behaviour = {"openrouter.ai": "written by openrouter"}

    client = chain_client("")
    message = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": "hi"}],
    )

    # The exact expression every adapted call site uses.
    assert message.content[0].text == "written by openrouter"
    assert message.provider == "openrouter"
    assert _HostRoutedClient.hosts_tried() == ["openrouter.ai"]


@pytest.mark.asyncio
async def test_adapter_crosses_providers_instead_of_going_dark(monkeypatch):
    """The whole point: an exhausted OpenRouter must not take these features out."""
    settings = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    _HostRoutedClient.behaviour = {
        "openrouter.ai": 429,
        "generativelanguage": 429,
        "groq.com": "written by groq",
    }

    message = await chain_client("").messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": "hi"}],
    )

    assert message.content[0].text == "written by groq"
    assert message.provider == "groq"


@pytest.mark.asyncio
async def test_adapter_never_forwards_the_claude_model_id_to_other_providers(monkeypatch):
    """A Claude id would 404 on Gemini/Groq — each provider must use its own."""
    settings = _settings()
    monkeypatch.setattr("app.config.get_settings", lambda: settings)
    _HostRoutedClient.behaviour = {"openrouter.ai": 429, "generativelanguage": "ok"}

    await chain_client("").messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=[{"role": "user", "content": "hi"}],
    )

    sent = [c["model"] for c in _HostRoutedClient.calls]
    assert "claude-sonnet-4-6" not in sent
    assert sent == ["openai/gpt-oss-20b:free", "gemini-2.0-flash"]


@pytest.mark.asyncio
async def test_adapter_raises_when_nothing_is_configured(monkeypatch):
    """No provider and no Anthropic key must raise, so the caller degrades."""
    settings = _settings(
        openrouter_api_key="", gemini_api_key="", groq_api_key="",
        cerebras_api_key="", anthropic_api_key="",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: settings)

    with pytest.raises(LLMChainExhausted):
        await chain_client("").messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            messages=[{"role": "user", "content": "hi"}],
        )


def test_any_llm_available_sees_a_chain_only_deployment():
    """The adapted services gated on the Anthropic key; that gate had to widen.

    A box carrying only a Groq key has working AI. Reporting it unavailable
    would leave the feature fixed and still dark — the same class of silent
    degradation the chain exists to end.
    """
    chain_only = _settings(anthropic_api_key="", openrouter_api_key="",
                           gemini_api_key="", cerebras_api_key="")
    assert any_llm_available(chain_only) is True

    anthropic_only = _settings(anthropic_api_key="sk-ant", openrouter_api_key="",
                               gemini_api_key="", groq_api_key="", cerebras_api_key="")
    assert any_llm_available(anthropic_only) is True

    nothing = _settings(anthropic_api_key="", openrouter_api_key="",
                        gemini_api_key="", groq_api_key="", cerebras_api_key="")
    assert any_llm_available(nothing) is False
