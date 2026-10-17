"""/settings/llm-key — the org's own Gemini key, as saved from Settings → API Keys."""

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.models.user import UserRole
from app.org_context import APPREND_ORG_ID
from app.services import crypto, org_gemini

URL = "/api/v1/settings/llm-key"


@pytest.fixture(autouse=True)
def _crypto_key():
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


@pytest.mark.asyncio
async def test_save_then_read_back_and_discovery_sees_it(client, db_session):
    r = await client.put(URL, json={"api_key": "  AIzaSyTestKey1234  ", "provider": "gemini"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_key"] is True and body["provider"] == "gemini"
    assert "AIzaSyTestKey1234" not in body["key_hint"]

    r = await client.get(URL)
    assert r.status_code == 200
    assert r.json()["has_key"] is True

    # The key Discovery actually uses is the trimmed one.
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) == "AIzaSyTestKey1234"


@pytest.mark.asyncio
async def test_replacing_keeps_a_single_key(client, db_session):
    assert (await client.put(URL, json={"api_key": "first-key-aaaa"})).status_code == 200
    assert (await client.put(URL, json={"api_key": "second-key-bbbb"})).status_code == 200
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) == "second-key-bbbb"


@pytest.mark.asyncio
async def test_delete_removes_the_key(client, db_session):
    await client.put(URL, json={"api_key": "AIzaSyDeleteMe"})
    assert (await client.delete(URL)).status_code == 204
    assert (await client.get(URL)).json()["has_key"] is False
    assert await org_gemini.get_org_gemini_key(db_session, APPREND_ORG_ID) is None


@pytest.mark.asyncio
async def test_blank_key_rejected_and_nothing_stored(client, db_session):
    r = await client.put(URL, json={"api_key": "   "})
    assert r.status_code == 422
    assert (await client.get(URL)).json()["has_key"] is False


@pytest.mark.asyncio
async def test_unknown_provider_rejected(client):
    # Previously stored under "openai" and reported has_key=True, but GET and
    # Discovery only ever read "gemini".
    r = await client.put(URL, json={"api_key": "sk-something", "provider": "openai"})
    assert r.status_code == 422
    assert (await client.get(URL)).json()["has_key"] is False


@pytest.mark.asyncio
async def test_requires_a_bearer_token(unauth_client):
    r = await unauth_client.put(URL, json={"api_key": "AIzaSyNoAuth"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_cannot_save(client, make_user, auth_headers):
    agent = await make_user("agent-llm@landline.app", role=UserRole.AGENT.value)
    r = await client.put(URL, json={"api_key": "AIzaSyAgent"}, headers=auth_headers(agent))
    assert r.status_code == 403
