"""Optional provider keys are read when used, never frozen at import.

A key captured in ``__init__`` (or worse, at module import) is the environment
as it was when the process happened to build the object — wrong for a long-lived
Celery worker, wrong after a config change, and it makes "no key" a property of
startup rather than of the call. These tests pin that the key is resolved on
access, that an unkeyed provider degrades to a skip instead of raising, and that
importing the modules with no keys at all is harmless.
"""

import importlib
import sys

import pytest

from app.config import get_settings
from app.services.enrichment import ApolloEnrichmentService
from app.services.hunter_verification import get_hunter_service


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings is cached; tests here mutate the cached object in place."""
    yield
    get_settings.cache_clear()


def test_apollo_key_is_read_at_access_not_construction(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "apollo_api_key", "")
    svc = ApolloEnrichmentService()
    assert svc.enabled is False

    # Key arrives after the service was built — a snapshot would miss it.
    monkeypatch.setattr(settings, "apollo_api_key", "key-set-later")
    assert svc.api_key == "key-set-later"
    assert svc.enabled is True


def test_hunter_key_is_read_at_access_not_construction(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hunter_api_key", "")
    svc = get_hunter_service()
    assert svc.enabled is False

    monkeypatch.setattr(settings, "hunter_api_key", "hunter-later")
    assert svc.api_key == "hunter-later"
    assert svc.enabled is True


def test_explicit_override_still_wins(monkeypatch):
    """Callers and tests assign ``svc.api_key`` directly — that must keep working."""
    settings = get_settings()
    monkeypatch.setattr(settings, "apollo_api_key", "from-env")

    svc = ApolloEnrichmentService()
    svc.api_key = "explicit"
    assert svc.api_key == "explicit"

    # An assigned empty string is a deliberate "act unkeyed", not "fall back to env".
    svc.api_key = ""
    assert svc.api_key == ""
    assert svc.enabled is False


def test_apollo_without_a_key_builds_headers_instead_of_raising(monkeypatch):
    """Constructing and using the client with no key must be inert, not fatal."""
    settings = get_settings()
    monkeypatch.setattr(settings, "apollo_api_key", "")
    svc = ApolloEnrichmentService()

    assert svc.enabled is False
    assert svc._auth_headers()["X-Api-Key"] == ""


@pytest.mark.asyncio
async def test_hunter_verification_without_a_key_is_a_skip(monkeypatch):
    """Fail-open is the contract: an unkeyed Hunter leaves the contact sendable."""
    import app.services.hunter_verification as hv

    settings = get_settings()
    monkeypatch.setattr(settings, "hunter_api_key", "")
    # Keep the free MX layer out of it — this is about the paid layer's gate.
    monkeypatch.setattr(hv, "check_mx", lambda domain: "valid")

    verdict = await get_hunter_service().verify_email("someone@example.com")
    assert verdict["status"] == "skipped"
    assert verdict["reason"] == "no_api_key"


def test_modules_import_cleanly_with_no_provider_keys(monkeypatch):
    """Importing enrichment/hunter must not depend on any key being present.

    Re-imported from scratch with the environment blank — if either module read
    a key (or built a client) at import time, this is where it would blow up.
    """
    monkeypatch.setenv("APOLLO_API_KEY", "")
    monkeypatch.setenv("HUNTER_API_KEY", "")
    get_settings.cache_clear()

    for name in ("app.services.enrichment", "app.services.hunter_verification"):
        module = importlib.import_module(name)
        reloaded = importlib.reload(module)
        assert reloaded is sys.modules[name]

    assert ApolloEnrichmentService().enabled is False
    assert get_hunter_service().enabled is False


@pytest.mark.asyncio
async def test_provider_status_endpoint_reports_unkeyed_providers(client, monkeypatch):
    """The degradation must be visible without shelling onto the box."""
    settings = get_settings()
    monkeypatch.setattr(settings, "apollo_api_key", "")
    monkeypatch.setattr(settings, "hunter_api_key", "hunter-key")

    resp = await client.get("/api/v1/system/providers")
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()}
    assert by_name["apollo"]["configured"] is False
    assert by_name["hunter"]["configured"] is True
    # A human reading this needs to know what "not configured" costs them.
    assert by_name["apollo"]["degraded_behaviour"]
