"""A marketplace connector may not be pointed inside the perimeter.

Two connectors take their destination from tenant-written configuration —
Slack's ``webhook_url`` credential and Salesforce's ``instance_url`` config —
and both are reachable by any authenticated user through ``POST /integrations``
followed by ``POST /integrations/{id}/actions``. Without a guard that is SSRF
with a settings form in front of it: the request leaves our process, from inside
the VPC, at a host the caller chose.

What these tests pin is that the refusal happens **before the socket**. Each one
installs a transport that fails the test if it is ever reached, so a fix that
merely hid the response body from the caller — leaving the request itself to be
made — does not pass. Blind SSRF is still SSRF: the status code alone maps an
internal network, and a POST to an internal endpoint has already happened by the
time the body is discarded.
"""

from __future__ import annotations

import ipaddress

import httpx
import pytest

from app.services.integrations.providers import (
    SalesforceConnector,
    SlackConnector,
)


class _NeverCalled(httpx.AsyncBaseTransport):
    """A transport that fails the test if the connector reaches the network."""

    def __init__(self):
        self.calls: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        raise AssertionError(f"connector made a forbidden request to {request.url}")


@pytest.fixture
def no_network(monkeypatch):
    """Make every httpx.AsyncClient in the connectors refuse to do real I/O."""
    transport = _NeverCalled()
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return transport


@pytest.fixture
def dns(monkeypatch):
    """Resolve hostnames to whatever the test says, with no real DNS.

    The guard resolves the host itself, so a test that used a real hostname
    would be asserting against whatever that name points at today.
    """
    mapping: dict[str, list[str]] = {}

    def _resolve(host: str):
        try:
            return [ipaddress.ip_address(host)]
        except ValueError:
            pass
        if host not in mapping:
            raise AssertionError(f"test did not stub DNS for {host!r}")
        return [ipaddress.ip_address(ip) for ip in mapping[host]]

    monkeypatch.setattr(
        "app.services.web_scraper._resolve_host_ips", _resolve
    )
    return mapping


# --- Slack -----------------------------------------------------------------

_INTERNAL = [
    # The cloud metadata endpoint — credentials, one unauthenticated GET away.
    "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    # The API's own uvicorn, which nginx publishes to nobody.
    "http://127.0.0.1:8001/api/v1/platform/organizations",
    # Anything else on the private network the box sits in.
    "http://10.0.3.14/admin",
    "http://[::1]:8001/health",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _INTERNAL)
async def test_slack_send_refuses_internal_targets(url, no_network, dns):
    result = await SlackConnector().execute(
        "send_notification", {"text": "hello"}, {}, {"webhook_url": url}
    )
    assert result.ok is False
    assert "not an allowed destination" in result.message
    assert no_network.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _INTERNAL)
async def test_slack_test_connection_refuses_internal_targets(url, no_network, dns):
    """Configuring one must fail too, not just firing it.

    ``POST /integrations`` calls ``test_connection`` and stores the verdict, so
    a webhook that can never fire should not sit in the UI reading "connected".
    """
    result = await SlackConnector().test_connection({}, {"webhook_url": url})
    assert result.ok is False
    assert "not an allowed destination" in result.message
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_slack_refuses_a_hostname_that_resolves_privately(no_network, dns):
    """A public-looking name is not a public destination.

    The check is on the resolved address, not on how the hostname reads — which
    is the whole point, since a name is free and an attacker controls its A
    record.
    """
    dns["hooks.slack.com.evil.example"] = ["169.254.169.254"]
    result = await SlackConnector().execute(
        "send_notification",
        {"text": "hi"},
        {},
        {"webhook_url": "https://hooks.slack.com.evil.example/services/T/B/x"},
    )
    assert result.ok is False
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_slack_refuses_a_non_http_scheme(no_network, dns):
    result = await SlackConnector().execute(
        "send_notification", {"text": "hi"}, {}, {"webhook_url": "file:///etc/passwd"}
    )
    assert result.ok is False
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_slack_still_sends_to_a_public_host(monkeypatch, dns):
    """The guard refuses the private cases and nothing else."""
    dns["hooks.slack.com"] = ["3.33.152.147"]
    seen: dict = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)

    result = await SlackConnector().execute(
        "send_notification",
        {"text": "a lead replied"},
        {"default_channel": "#sales"},
        {"webhook_url": "https://hooks.slack.com/services/T000/B000/xyz"},
    )
    assert result.ok is True
    assert seen["url"] == "https://hooks.slack.com/services/T000/B000/xyz"
    assert b"a lead replied" in seen["body"]


# --- Salesforce ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["fetch_contacts", "sync_contact"])
async def test_salesforce_refuses_an_internal_instance_url(action, no_network, dns):
    result = await SalesforceConnector().execute(
        action,
        {"email": "a@b.com"},
        {"instance_url": "http://127.0.0.1:8001"},
        {"access_token": "tok"},
    )
    assert result.ok is False
    assert "not an allowed destination" in result.message
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_salesforce_cursor_cannot_relocate_the_request(no_network, dns):
    """A cursor is a path. Anything else is a different host.

    ``base + cursor`` with a cursor of ``@host/`` makes the configured instance
    into userinfo and sends the bearer token to ``host`` instead. The caller
    supplies this value directly (``CrmImportRequest.cursor``), so the fact that
    Salesforce is where a *legitimate* cursor comes from is not a constraint on
    what arrives here.
    """
    dns["acme.my.salesforce.com"] = ["96.43.144.10"]
    result = await SalesforceConnector().execute(
        "fetch_contacts",
        {"cursor": "@169.254.169.254/latest/meta-data/"},
        {"instance_url": "https://acme.my.salesforce.com"},
        {"access_token": "tok"},
    )
    assert result.ok is False
    assert "must be a path" in result.message
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_salesforce_absolute_cursor_is_refused(no_network, dns):
    result = await SalesforceConnector().execute(
        "fetch_contacts",
        {"cursor": "https://evil.example/steal"},
        {"instance_url": "https://acme.my.salesforce.com"},
        {"access_token": "tok"},
    )
    assert result.ok is False
    assert "must be a path" in result.message
    assert no_network.calls == []


@pytest.mark.asyncio
async def test_salesforce_fetch_still_works_over_a_public_instance(monkeypatch, dns):
    dns["acme.my.salesforce.com"] = ["96.43.144.10"]
    seen: dict = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "records": [
                    {"Id": "00Q1", "Email": "lead@acme.test", "LastName": "Nkemelu"}
                ],
                "nextRecordsUrl": "/services/data/v59.0/query/01g-2000",
            },
        )

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)

    result = await SalesforceConnector().execute(
        "fetch_contacts",
        {"limit": 50},
        {"instance_url": "https://acme.my.salesforce.com/"},
        {"access_token": "tok"},
    )
    assert result.ok is True
    assert seen["url"].startswith(
        "https://acme.my.salesforce.com/services/data/v59.0/query"
    )
    assert result.data["records"][0]["email"] == "lead@acme.test"
    # A real cursor — a path — is accepted and passed back for the next page.
    assert result.data["cursor"] == "/services/data/v59.0/query/01g-2000"


@pytest.mark.asyncio
async def test_salesforce_follows_a_real_cursor(monkeypatch, dns):
    dns["acme.my.salesforce.com"] = ["96.43.144.10"]
    seen: dict = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"records": []})

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)

    result = await SalesforceConnector().execute(
        "fetch_contacts",
        {"cursor": "/services/data/v59.0/query/01g-2000"},
        {"instance_url": "https://acme.my.salesforce.com"},
        {"access_token": "tok"},
    )
    assert result.ok is True
    assert seen["url"] == (
        "https://acme.my.salesforce.com/services/data/v59.0/query/01g-2000"
    )


# --- The connectors that are not configurable stay unaffected ---------------


@pytest.mark.asyncio
async def test_fixed_host_connectors_need_no_dns_stub(monkeypatch):
    """HubSpot's host is a constant, so no tenant can move it.

    Worth pinning: the guard is deliberately *not* applied to the fixed-host
    connectors, and this is what says that is a decision rather than an
    oversight. If someone later makes HubSpot's base URL configurable, this test
    keeps passing — which is exactly why the guard belongs at the point a URL
    stops being a constant, and why that is called out in the module docstring.
    """
    from app.services.integrations.providers import HubSpotConnector

    seen: dict = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(201, json={"id": "123"})

    transport = httpx.MockTransport(_handler)
    original = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = transport
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)

    result = await HubSpotConnector().execute(
        "sync_contact", {"email": "x@y.test"}, {}, {"access_token": "tok"}
    )
    assert result.ok is True
    assert seen["url"].startswith("https://api.hubapi.com/")
