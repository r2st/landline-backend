"""Apollo refusing the key or the plan is not a transient error.

Production (2026-10-17): every Discovery search came back as Apollo's

    HTTP 403: {"error":"The api/v1/mixed_companies/search API is not included
    in your Free plan …","error_code":"API_INACCESSIBLE"}

and surfaced as status ``error``, whose UI copy is "try again in a moment, or
loosen your filters" — advice that can never work. A rejected key (401) or a
plan without the endpoint (403) answers identically forever, so it gets its own
status, and the search endpoint says in words what is wrong. The same goes for
no key at all: that must read "Apollo is not configured", not a bare failure.
"""

import httpx
import pytest

from app.routers import discovery as discovery_router
from app.services import enrichment
from app.services.enrichment import ApolloEnrichmentService, ApolloSearchResult

FREE_PLAN_403 = (
    '{"error":"The api/v1/mixed_companies/search API is not included in your '
    'Free plan and is not accessible, even with a master key.",'
    '"error_code":"API_INACCESSIBLE"}'
)


def _service_answering(monkeypatch, status, body):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=body)

    real_client = httpx.AsyncClient

    class _Client(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(enrichment.httpx, "AsyncClient", _Client)
    svc = ApolloEnrichmentService()
    svc.api_key = "test-key"
    return svc, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_or_plan_is_unauthorized_not_error(monkeypatch, status):
    svc, calls = _service_answering(monkeypatch, status, FREE_PLAN_403)
    result = await svc.search_companies(None, industry="Software", per_page=5)
    assert result.status == "unauthorized"
    assert "Free plan" in (result.error or "")
    # A verdict on the account answers the same way every time: no retries.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_bad_request_is_still_a_plain_error(monkeypatch):
    """Control: only 401/403 are the account's fault."""
    svc, _ = _service_answering(monkeypatch, 400, '{"error":"bad filter"}')
    result = await svc.search_companies(None, industry="Software", per_page=5)
    assert result.status == "error"


@pytest.mark.asyncio
async def test_no_key_is_skipped_without_calling_apollo(monkeypatch):
    svc, calls = _service_answering(monkeypatch, 200, "{}")
    svc.api_key = ""
    result = await svc.search_companies(None, industry="Software", per_page=5)
    assert (result.status, result.error) == ("skipped", "no_api_key")
    assert calls == []


def _stub_search(monkeypatch, result):
    class _Svc:
        async def search_companies(self, db, **kwargs):
            return result

    monkeypatch.setattr(discovery_router, "get_enrichment_service", lambda: _Svc())


@pytest.mark.asyncio
async def test_endpoint_says_apollo_is_not_configured(client, monkeypatch):
    _stub_search(monkeypatch, ApolloSearchResult(status="skipped", error="no_api_key"))
    resp = await client.post("/api/v1/discovery/search-companies", json={"industry": "Software"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["search_status"] == "skipped"
    assert "Apollo is not configured" in body["search_error"]


@pytest.mark.asyncio
async def test_endpoint_explains_an_apollo_refusal(client, monkeypatch):
    _stub_search(
        monkeypatch,
        ApolloSearchResult(status="unauthorized", error=f"HTTP 403: {FREE_PLAN_403}"),
    )
    resp = await client.post("/api/v1/discovery/search-companies", json={"industry": "Software"})
    body = resp.json()
    assert body["search_status"] == "unauthorized"
    assert "Apollo refused the request" in body["search_error"]
    assert "Free plan" in body["search_error"]


@pytest.mark.asyncio
async def test_endpoint_has_no_error_on_success(client, monkeypatch):
    _stub_search(monkeypatch, ApolloSearchResult(status="ok"))
    resp = await client.post("/api/v1/discovery/search-companies", json={"industry": "Software"})
    assert resp.json()["search_error"] is None
