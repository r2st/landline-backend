"""A tenant-supplied webhook target is a URL this server fetches.

`webhook_dispatch` POSTs to `WebhookSubscription.target_url` — a string an org
user typed into a form — from inside the production network. Registration
checked that it started with `http://` or `https://` and nothing else, so
`http://169.254.169.254/latest/meta-data/` and `http://127.0.0.1:8001/...` were
both accepted and both delivered to.

`POST /integrations/webhooks/emit` is what turns that from a theoretical
outbound request into a usable one: the same user fires a delivery on demand
with a payload of their choosing, and the response reports each delivery's
status and `response_code`. The request also carries a valid
`X-Landline-Signature` over that body.

The guard being applied here is the one this codebase already wrote — the same
`validate_public_url` the social-listening agent runs on org-supplied RSS feeds.
"""

import ipaddress

import pytest
from sqlalchemy import select

from app.models.integration import WebhookEvent, WebhookSubscription
from app.services.webhook_dispatch import WebhookDispatcher


def _resolve_to(monkeypatch, ip):
    monkeypatch.setattr(
        "app.services.web_scraper._resolve_host_ips",
        lambda host: [ipaddress.ip_address(ip)],
    )


class _RecordingClient:
    """An httpx stand-in that records every request it is asked to make."""

    sent: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        _RecordingClient.sent.append(url)

        class _Resp:
            status_code = 200

        return _Resp()


@pytest.fixture
def recorder(monkeypatch):
    _RecordingClient.sent = []
    monkeypatch.setattr(
        "app.services.webhook_dispatch.httpx.AsyncClient", _RecordingClient
    )
    return _RecordingClient


# --- Registration -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8001/api/v1/platform/organizations",
        "http://localhost/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]:9000/",
    ],
)
@pytest.mark.asyncio
async def test_registering_a_private_target_is_refused(client, url):
    resp = await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": url, "event_types": ["lead.replied"]},
    )
    assert resp.status_code == 400, resp.text
    assert "public" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_a_public_target_still_registers(client, monkeypatch):
    """The control — the guard must not refuse an ordinary receiver."""
    _resolve_to(monkeypatch, "93.184.216.34")
    resp = await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://hooks.example.test/x", "event_types": ["lead.replied"]},
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_a_target_that_does_not_resolve_still_registers(client):
    """Storing a URL is not fetching one.

    A receiver whose DNS is briefly down, or whose record is created after the
    integration is configured, is an ordinary thing to set up. Refusing it here
    would be a config-time failure with nothing to do with private addresses —
    and the delivery path re-checks and fails closed, which is the test below.
    """
    resp = await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://nowhere.invalid/hook", "event_types": ["lead.replied"]},
    )
    assert resp.status_code == 201, resp.text


# --- Delivery ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_delivery_rechecks_dns_and_refuses_a_rebound_host(
    client, db_session, monkeypatch, recorder
):
    """Registration passing is not a permanent verdict on the host.

    DNS is not a property of the stored string: a hostname that resolved to a
    public address when the subscription was created can be re-pointed at the
    metadata endpoint the next day, and the subscription outlives the check that
    admitted it. So the guard runs again at delivery.
    """
    _resolve_to(monkeypatch, "93.184.216.34")
    created = await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://rebind.example.test/hook", "event_types": ["lead.replied"]},
    )
    assert created.status_code == 201, created.text

    # ...and now the attacker re-points it.
    _resolve_to(monkeypatch, "169.254.169.254")

    resp = await client.post(
        "/api/v1/integrations/webhooks/emit",
        json={"event_type": "lead.replied", "payload": {"x": 1}},
    )
    assert resp.status_code == 200, resp.text
    deliveries = resp.json()["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "failed"
    # The point: no request was made at all, not "a request that got a 4xx".
    assert recorder.sent == []


@pytest.mark.asyncio
async def test_a_refused_delivery_is_recorded_with_its_reason(
    db_session, monkeypatch, recorder
):
    """The event row says why, so an operator can see it in the audit trail."""
    _resolve_to(monkeypatch, "127.0.0.1")
    sub = WebhookSubscription(
        target_url="http://internal.example.test/hook",
        event_types=["lead.replied"],
        is_active=True,
    )
    db_session.add(sub)
    await db_session.commit()

    events = await WebhookDispatcher().emit(
        db_session, "lead.replied", {"x": 1}, sub.org_id
    )
    await db_session.commit()

    assert len(events) == 1
    stored = (
        await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.id == events[0].id)
        )
    ).scalar_one()
    assert stored.status == "failed"
    assert "unsafe" in (stored.last_error or "").lower()
    assert stored.attempts == 1
    assert recorder.sent == []


@pytest.mark.asyncio
async def test_a_public_target_is_still_delivered_to(client, monkeypatch, recorder):
    """The delivery-side control. Without it the assertions above prove nothing."""
    _resolve_to(monkeypatch, "93.184.216.34")
    await client.post(
        "/api/v1/integrations/webhooks/subscriptions",
        json={"target_url": "https://ok.example.test/hook", "event_types": ["lead.replied"]},
    )
    resp = await client.post(
        "/api/v1/integrations/webhooks/emit",
        json={"event_type": "lead.replied", "payload": {"x": 1}},
    )
    assert resp.json()["deliveries"][0]["status"] == "delivered"
    assert recorder.sent == ["https://ok.example.test/hook"]
