"""Per-org Gemini resolution for the Discovery document features."""

import uuid

import pytest
from cryptography.fernet import Fernet
from pydantic import BaseModel

from app.config import get_settings

from app.models.org_llm_key import OrgLlmKey
from app.models.tenant import Organization
from app.org_context import APPREND_ORG_ID
from app.services import crypto, llm_providers, org_gemini
from app.services.llm_providers import LLMChainExhausted


class _Answer(BaseModel):
    companies: list[str]


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _reset_org_breaker():
    org_gemini.reset_breaker()
    yield
    org_gemini.reset_breaker()


async def _other_org(db_session) -> uuid.UUID:
    org = Organization(
        id=uuid.uuid4(), name="Other Co", slug=f"other-{uuid.uuid4().hex[:6]}",
        is_active=True, billing_plan="pro",
    )
    db_session.add(org)
    await db_session.commit()
    return org.id


async def _save_key(db_session, org_id, key):
    db_session.add(OrgLlmKey(
        org_id=org_id, provider="gemini",
        key_encrypted=crypto.encrypt(key), key_hint="••••",
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_no_key_raises_key_missing(db_session):
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) is None
    with pytest.raises(org_gemini.GeminiKeyMissing):
        await org_gemini.generate_json(
            db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
        )


@pytest.mark.asyncio
async def test_uses_org_key_model_and_private_breaker(db_session, monkeypatch):
    await _save_key(db_session, APPREND_ORG_ID, "org-a-secret-key")
    seen = {}

    async def fake_chat(messages, *, providers, breaker, **kw):
        seen["providers"] = providers
        seen["breaker"] = breaker
        return '```json\n{"companies": ["Acme"]}\n```', providers[0].name, providers[0].models[0]

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    out = await org_gemini.generate_json(
        db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
    )
    assert out.companies == ["Acme"]
    (spec,) = seen["providers"]
    assert spec.api_key == "org-a-secret-key"
    # An ordered fallback list, so chat_completion walks past a model that
    # 404s for newer keys instead of failing the whole call.
    assert spec.models == org_gemini.discovery_models()
    assert len(spec.models) > 1 and "gemini-flash-latest" in spec.models
    assert spec.extra_body["response_format"] == {"type": "json_object"}
    assert spec.name == f"gemini-org-{APPREND_ORG_ID}"
    # Never the shared platform breaker.
    assert seen["breaker"] is not llm_providers.get_breaker()


@pytest.mark.asyncio
async def test_keys_are_isolated_per_org(db_session, monkeypatch):
    other = await _other_org(db_session)
    await _save_key(db_session, other, "org-b-secret-key")
    # Org A has no key of its own, and must not borrow org B's.
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) is None
    assert await org_gemini.get_org_gemini_key(db_session, other) == "org-b-secret-key"


@pytest.mark.asyncio
async def test_malformed_answer_is_a_gemini_error(db_session, monkeypatch):
    await _save_key(db_session, APPREND_ORG_ID, "k" * 20)

    async def fake_chat(messages, *, providers, **kw):
        return "not json", "x", "m"

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    with pytest.raises(org_gemini.GeminiError):
        await org_gemini.generate_json(
            db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
        )


@pytest.mark.asyncio
async def test_upstream_failure_message_never_contains_key(db_session, monkeypatch):
    await _save_key(db_session, APPREND_ORG_ID, "super-secret-gemini-key")

    async def fake_chat(messages, *, providers, **kw):
        raise LLMChainExhausted(f"all failed; key={providers[0].api_key}")

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    with pytest.raises(org_gemini.GeminiError) as exc:
        await org_gemini.generate_json(
            db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
        )
    assert "super-secret-gemini-key" not in str(exc.value)


def test_discovery_models_parses_the_comma_list(monkeypatch):
    monkeypatch.setattr(get_settings(), "discovery_gemini_model", " a-flash , ,b-flash ")
    assert org_gemini.discovery_models() == ("a-flash", "b-flash")
    monkeypatch.setattr(get_settings(), "discovery_gemini_model", "")
    assert org_gemini.discovery_models() == ("gemini-flash-latest",)


# --- Groq / OpenRouter fallbacks (From a document) -------------------------


@pytest.mark.asyncio
async def test_chain_is_gemini_then_groq_then_openrouter(db_session, monkeypatch):
    # Saved out of order on purpose: the chain order is fixed, not insertion order.
    await _save_key(db_session, APPREND_ORG_ID, "or-key")
    await db_session.execute(
        OrgLlmKey.__table__.update()
        .where(OrgLlmKey.org_id == APPREND_ORG_ID)
        .values(provider="openrouter")
    )
    await db_session.commit()
    await _save_key(db_session, APPREND_ORG_ID, "gemini-key")
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="groq",
        key_encrypted=crypto.encrypt("groq-key"), key_hint="••••",
    ))
    await db_session.commit()

    seen = {}

    async def fake_chat(messages, *, providers, **kw):
        seen["providers"] = providers
        return '{"companies": ["Acme"]}', providers[1].name, providers[1].models[0]

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    out = await org_gemini.generate_json(
        db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
    )
    assert out.companies == ["Acme"]
    gem, groq, orouter = seen["providers"]
    assert [p.name.split("-org-")[0] for p in seen["providers"]] == ["gemini", "groq", "openrouter"]
    assert (gem.api_key, groq.api_key, orouter.api_key) == ("gemini-key", "groq-key", "or-key")
    assert "api.groq.com" in groq.endpoint and "openrouter.ai" in orouter.endpoint
    assert groq.extra_body == {"response_format": {"type": "json_object"}}
    assert "llama-3.3-70b-versatile" in groq.models
    assert "response_format" not in orouter.extra_body
    assert all(m.endswith(":free") for m in orouter.models)
    assert orouter.headers.get("X-Title")


@pytest.mark.asyncio
async def test_groq_only_org_runs_document_extraction(db_session, monkeypatch):
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="groq",
        key_encrypted=crypto.encrypt("groq-key"), key_hint="••••",
    ))
    await db_session.commit()
    assert await org_gemini.has_org_llm_key(db_session, APPREND_ORG_ID)
    assert not await org_gemini.has_org_gemini_key(db_session, APPREND_ORG_ID)

    async def fake_chat(messages, *, providers, **kw):
        assert [p.name.split("-org-")[0] for p in providers] == ["groq"]
        return 'Sure:\n{"companies": ["Acme"]}', providers[0].name, providers[0].models[0]

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    out = await org_gemini.generate_json(
        db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
    )
    assert out.companies == ["Acme"]


@pytest.mark.asyncio
async def test_all_providers_failing_names_them_without_keys(db_session, monkeypatch):
    await _save_key(db_session, APPREND_ORG_ID, "gemini-secret")
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="groq",
        key_encrypted=crypto.encrypt("groq-secret"), key_hint="••••",
    ))
    await db_session.commit()

    async def fake_chat(*a, **kw):
        raise LLMChainExhausted("429 for gemini-secret / groq-secret")

    monkeypatch.setattr(org_gemini, "chat_completion", fake_chat)
    with pytest.raises(org_gemini.GeminiError) as exc:
        await org_gemini.generate_json(
            db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
        )
    msg = str(exc.value)
    assert "Gemini, Groq" in msg
    assert "secret" not in msg


@pytest.mark.asyncio
async def test_a_failed_gemini_reaches_groq_through_the_real_chain(db_session, monkeypatch):
    """chat_completion itself walks Gemini's models, then moves to Groq."""
    await _save_key(db_session, APPREND_ORG_ID, "gemini-key")
    db_session.add(OrgLlmKey(
        org_id=APPREND_ORG_ID, provider="groq",
        key_encrypted=crypto.encrypt("groq-key"), key_hint="••••",
    ))
    await db_session.commit()
    import httpx

    urls = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def post(self, url, headers=None, json=None):
            urls.append((url, json["model"]))
            req = httpx.Request("POST", url)
            if "generativelanguage" in url:
                return httpx.Response(429, json={"error": {}}, request=req)
            return httpx.Response(200, request=req, json={
                "choices": [{"message": {"content": '{"companies": ["Acme"]}'}}]
            })

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", _Client)
    out = await org_gemini.generate_json(
        db_session, APPREND_ORG_ID, system="s", prompt="p", schema=_Answer
    )
    assert out.companies == ["Acme"]
    gemini_models = [m for u, m in urls if "generativelanguage" in u]
    assert gemini_models == list(org_gemini.discovery_models())
    assert "api.groq.com" in urls[-1][0]


@pytest.mark.asyncio
async def test_duplicate_key_rows_do_not_500_and_newest_wins(db_session):
    # org_llm_keys has no unique (org_id, provider) constraint.
    from datetime import datetime, timedelta, timezone

    old = OrgLlmKey(org_id=APPREND_ORG_ID, provider="gemini",
                    key_encrypted=crypto.encrypt("old-key"), key_hint="••••")
    new = OrgLlmKey(org_id=APPREND_ORG_ID, provider="gemini",
                    key_encrypted=crypto.encrypt("new-key"), key_hint="••••")
    db_session.add_all([old, new])
    await db_session.flush()
    now = datetime.now(timezone.utc)
    old.updated_at = now - timedelta(days=1)
    new.updated_at = now
    await db_session.commit()
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) == "new-key"
