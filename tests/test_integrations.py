"""Tests for the integration marketplace (feature 4.2).

Covers the connector registry + catalog, the standardized connector interface
(auth validation, action dispatch, graceful failure), credential encryption at
rest, the marketplace API, and the outbound webhook framework.
"""

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.services.integrations import get_connector, known_providers, marketplace_catalog
from app.services.integrations.base import ConnectorResult
from app.services.integrations.providers import SlackConnector


@pytest.fixture(autouse=True)
def _encryption_key():
    """Integration credentials are encrypted at rest — provide a Fernet key."""
    settings = get_settings()
    prev = settings.token_encryption_key
    settings.token_encryption_key = Fernet.generate_key().decode()
    # crypto caches the Fernet instance via lru_cache — clear it.
    from app.services import crypto

    crypto._fernet.cache_clear()
    yield
    settings.token_encryption_key = prev
    crypto._fernet.cache_clear()


# --- Connector interface (async) ------------------------------------------


@pytest.mark.asyncio
async def test_slack_test_connection_requires_webhook():
    c = SlackConnector()
    fail = await c.test_connection({}, {})
    assert fail.ok is False
    ok = await c.test_connection({}, {"webhook_url": "https://hooks.slack.test/x"})
    assert ok.ok is True


@pytest.mark.asyncio
async def test_connector_rejects_unsupported_action():
    c = SlackConnector()
    result = await c.execute("nonexistent", {}, {}, {})
    assert result.ok is False
    assert "does not support" in result.message


@pytest.mark.asyncio
async def test_slack_send_notification_missing_creds_fails_gracefully():
    c = SlackConnector()
    result = await c.execute("send_notification", {"text": "hi"}, {}, {})
    assert result.ok is False  # no webhook_url → failure, not an exception


@pytest.mark.asyncio
async def test_slack_send_notification_success(monkeypatch):
    c = SlackConnector()

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp()

    monkeypatch.setattr("app.services.integrations.providers.httpx.AsyncClient", _Client)
    result = await c.execute(
        "send_notification", {"text": "deal won"}, {}, {"webhook_url": "https://hooks.slack.test/x"}
    )
    assert result.ok is True


# --- Marketplace API -------------------------------------------------------


@pytest.mark.asyncio
async def test_marketplace_lists_all_providers(client):
    resp = await client.get("/api/v1/integrations/marketplace")
    assert resp.status_code == 200
    providers = {i["provider"] for i in resp.json()["integrations"]}
    assert {"slack", "google_calendar", "hubspot", "salesforce", "clearbit", "zoominfo"} <= providers


@pytest.mark.asyncio
async def test_create_integration_encrypts_credentials(client, db_engine):
    resp = await client.post(
        "/api/v1/integrations",
        json={
            "provider": "slack",
            "config": {"default_channel": "#sales"},
            "credentials": {"webhook_url": "https://hooks.slack.test/abc"},
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # The secret value is never echoed back — only which keys are set.
    assert data["configured_credentials"] == ["webhook_url"]
    assert data["status"] == "connected"
    assert "webhook_url" not in str(data.get("config"))

    # And it is stored as ciphertext, not plaintext, in the DB.
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.models.integration import Integration

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        row = (await s.execute(select(Integration))).scalar_one()
        assert row.credentials["webhook_url"] != "https://hooks.slack.test/abc"


@pytest.mark.asyncio
async def test_create_integration_rejects_unknown_provider(client):
    resp = await client.post("/api/v1/integrations", json={"provider": "nope"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_integration_conflicts(client):
    await client.post("/api/v1/integrations", json={"provider": "hubspot", "credentials": {"access_token": "t"}})
    dup = await client.post("/api/v1/integrations", json={"provider": "hubspot", "credentials": {"access_token": "t"}})
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_list_and_delete_integration(client):
    created = await client.post(
        "/api/v1/integrations", json={"provider": "clearbit", "credentials": {"api_key": "k"}}
    )
    iid = created.json()["id"]

    listed = await client.get("/api/v1/integrations")
    assert any(i["id"] == iid for i in listed.json())

    deleted = await client.delete(f"/api/v1/integrations/{iid}")
    assert deleted.status_code == 200
    listed2 = await client.get("/api/v1/integrations")
    assert all(i["id"] != iid for i in listed2.json())


@pytest.mark.asyncio
async def test_run_action_missing_creds_returns_failure(client):
    created = await client.post(
        "/api/v1/integrations", json={"provider": "clearbit", "credentials": {}}
    )
    iid = created.json()["id"]
    resp = await client.post(
        f"/api/v1/integrations/{iid}/actions",
        json={"action": "enrich_company", "params": {"domain": "acme.com"}},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False  # no api_key configured


# --- Webhook framework -----------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_subscription_crud(client):
    created = await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://example.test/hook", "event_types": ["lead.replied"], "secret": "shh"},
    )
    assert created.status_code == 201
    sub_id = created.json()["id"]

    listed = await client.get("/api/v1/integrations/webhooks/subscriptions")
    body = listed.json()
    assert len(body) == 1
    assert body[0]["has_secret"] is True

    deleted = await client.delete(f"/api/v1/integrations/webhooks/subscriptions/{sub_id}")
    assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_webhook_subscription_rejects_bad_url(client):
    resp = await client.post(
        "/api/v1/integrations/webhooks/subscriptions", json={"target_url": "ftp://bad"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_emit_webhook_delivers_and_records(client, monkeypatch):
    # Subscribe to an event.
    await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://example.test/hook", "event_types": ["lead.replied"]},
    )

    # Stub the HTTP delivery.
    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            return _Resp()

    monkeypatch.setattr("app.services.webhook_dispatch.httpx.AsyncClient", _Client)

    resp = await client.post(
        "/api/v1/integrations/webhooks/emit",
        json={"event_type": "lead.replied", "payload": {"company": "Acme"}},
    )
    assert resp.status_code == 200
    deliveries = resp.json()["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_emit_skips_non_matching_subscriptions(client, monkeypatch):
    await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://example.test/hook", "event_types": ["deal.won"]},
    )
    resp = await client.post(
        "/api/v1/integrations/webhooks/emit",
        json={"event_type": "lead.replied", "payload": {}},
    )
    # Subscription only wants deal.won → no delivery for lead.replied.
    assert resp.json()["deliveries"] == []


# --- Pure sync unit tests (kept last — see test ordering note) -------------


def test_registry_and_catalog():
    assert set(known_providers()) == {
        "slack", "google_calendar", "hubspot", "salesforce", "clearbit", "zoominfo"
    }
    assert get_connector("unknown") is None
    catalog = marketplace_catalog()
    slack = next(c for c in catalog if c["provider"] == "slack")
    assert slack["auth_type"] == "webhook"
    assert "send_notification" in slack["supported_actions"]


def test_connector_result_helpers():
    ok = ConnectorResult.success("done", id=1)
    assert ok.ok is True and ok.data["id"] == 1
    bad = ConnectorResult.failure("nope")
    assert bad.ok is False
