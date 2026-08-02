"""Tests for the security-hardening fixes (gap analysis C2, C4, H1, H4, H6).

Covers:
  * C2 — auth fails closed (no fallback secret, boot check, protected routes).
  * C4 — SSRF guard in the web scraper + auth on the scrape endpoint.
  * H1 — previously-unprotected routers now require authentication.
  * H4 — rate limiting on /auth/* and AI endpoints.
  * H6 — Swagger/OpenAPI docs disabled in production.
"""

import importlib

import pytest
from httpx import AsyncClient

import app.config
import app.main
import app.security as security
from app.main import require_secret_key
from app.rate_limit import AUTH_RATE_LIMIT, limiter
from app.services.web_scraper import UnsafeURLError, validate_public_url


# ─────────────────────────────── C2: fail closed ────────────────────────────

def test_secret_key_has_no_hardcoded_fallback(monkeypatch):
    """_secret_key must raise (not return a default) when nothing is configured."""
    monkeypatch.setattr(security, "get_configured_secret", lambda: "")
    with pytest.raises(RuntimeError):
        security._secret_key()


def test_secret_key_returned_when_configured(monkeypatch):
    monkeypatch.setattr(security, "get_configured_secret", lambda: "a-real-secret")
    assert security._secret_key() == "a-real-secret"


def test_boot_refuses_without_secret(monkeypatch):
    """The app must refuse to start when no signing secret is configured."""
    monkeypatch.setattr(app.main, "get_configured_secret", lambda: "")
    with pytest.raises(RuntimeError):
        require_secret_key()


def test_boot_allowed_with_secret(monkeypatch):
    monkeypatch.setattr(app.main, "get_configured_secret", lambda: "secret")
    require_secret_key()  # should not raise


@pytest.mark.asyncio
async def test_protected_route_rejects_missing_credentials(unauth_client: AsyncClient):
    """A non-public route with no API key / token → 401 (fail closed)."""
    resp = await unauth_client.get("/api/v1/companies/")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_protected_route_rejects_garbage_token(unauth_client: AsyncClient):
    resp = await unauth_client.get(
        "/api/v1/companies/", headers={"Authorization": "Bearer not-a-real-jwt"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_public_paths_still_open(unauth_client: AsyncClient):
    """Health check must remain reachable without credentials."""
    assert (await unauth_client.get("/health")).status_code == 200
    assert (await unauth_client.get("/api/v1/health")).status_code == 200


@pytest.mark.asyncio
async def test_deactivated_user_rejected_on_scoped_route(
    unauth_client: AsyncClient, make_user, auth_headers
):
    """A token for a deactivated user must be rejected even on a get_scope route
    (previously it silently fell back to the unscoped 'see all' path)."""
    user = await make_user("deactivated@landline.app", is_active=False)
    resp = await unauth_client.get("/api/v1/tasks", headers=auth_headers(user))
    assert resp.status_code == 401


# ─────────────────────────────── C4: SSRF guard ─────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata endpoint
        "http://10.0.0.5/",
        "http://192.168.1.10/",
        "http://172.16.5.5/",
        "http://[::1]/",            # IPv6 loopback
        "https://0.0.0.0/",         # unspecified
        "ftp://example.com/",       # non-http(s) scheme
        "http:///nohost",           # missing host
    ],
)
def test_validate_public_url_rejects_unsafe(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


def test_validate_public_url_allows_public_ip():
    # A literal public IP does not require DNS and must pass.
    assert validate_public_url("http://8.8.8.8/") == "http://8.8.8.8/"


@pytest.mark.asyncio
async def test_scrape_endpoint_requires_auth(unauth_client: AsyncClient):
    resp = await unauth_client.post(
        "/api/v1/discovery/scrape-website", json={"domain": "example.com"}
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_scrape_endpoint_blocks_private_host(client: AsyncClient):
    """Scraping a private/metadata host is rejected before any fetch."""
    resp = await client.post(
        "/api/v1/discovery/scrape-website", json={"domain": "169.254.169.254"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body and "reject" in body["error"].lower()


# ─────────────────────────── H1: consistent auth ────────────────────────────

@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/companies/",
        "/api/v1/dashboard",
        "/api/v1/news",
        "/api/v1/reports/overview",
        "/api/v1/pipeline",
        "/api/v1/groups",
        "/api/v1/tactics",
        "/api/v1/clients",
        "/api/v1/knowledge",
        "/api/v1/investors",
        "/api/v1/suggestions",
    ],
)
@pytest.mark.asyncio
async def test_previously_open_routers_require_auth(unauth_client: AsyncClient, path):
    """These routers were unauthenticated before H1 — must now return 401
    (not 200) without a token. 404 would mean the route path is wrong."""
    resp = await unauth_client.get(path)
    assert resp.status_code == 401, f"{path} returned {resp.status_code}, expected 401"


@pytest.mark.asyncio
async def test_outreach_webhook_rejects_unsigned_anonymous_post(unauth_client: AsyncClient):
    """The event webhook takes a credential — a signature or a JWT, not neither.

    This test used to assert the opposite (``!= 401``), on the reasoning that the
    enrollment foreign key was guard enough for an anonymous SES callback. The FK
    proves the enrollment exists, which is not the same as proving the caller may
    write to the send ledger: ``event_type='sent'`` rows are what
    ``SendLimiter._db_sent_today`` counts against the daily cap, and ``bounced``
    rows are what the kill switch reads. Full reasoning on
    ``require_signed_webhook_or_user``; the signed and JWT paths are covered in
    ``test_outreach_webhook_auth.py``.

    Note the 401 must arrive *before* the 404 for the missing enrollment — an
    unauthenticated caller should not be able to use this endpoint to test
    whether an enrollment id is real.
    """
    resp = await unauth_client.post(
        "/api/v1/outreach/events",
        json={
            "enrollment_id": "00000000-0000-0000-0000-000000000000",
            "event_type": "opened",
            "step_number": 1,
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_outreach_list_requires_auth(unauth_client: AsyncClient):
    """Non-webhook outreach routes are protected."""
    resp = await unauth_client.get("/api/v1/outreach/sequences")
    assert resp.status_code == 401


# ─────────────────────────────── H4: rate limit ─────────────────────────────

@pytest.mark.asyncio
async def test_login_is_rate_limited(unauth_client: AsyncClient):
    """Exceeding the auth rate limit returns 429."""
    limit = int(AUTH_RATE_LIMIT.split("/")[0])
    limiter.reset()
    limiter.enabled = True
    try:
        statuses = []
        for _ in range(limit + 1):
            r = await unauth_client.post(
                "/api/v1/auth/login",
                json={"email": "nobody@landline.app", "password": "x"},
            )
            statuses.append(r.status_code)
        assert 429 in statuses, f"expected a 429 within {limit + 1} requests, got {statuses}"
        assert statuses[-1] == 429
    finally:
        limiter.enabled = False
        limiter.reset()


@pytest.mark.asyncio
async def test_successful_login_under_active_limiter(unauth_client: AsyncClient, admin_user):
    """Correct credentials return 200 while the limiter is live.

    The rest of the H4 coverage only ever sends *wrong* credentials, which raise
    before slowapi post-processes the return value — so the success path went
    unexercised with the limiter on, and shipped a 500 that only ever hit users
    who typed the right password.
    """
    limiter.reset()
    limiter.enabled = True
    try:
        resp = await unauth_client.post(
            "/api/v1/auth/login",
            json={"email": "admin@landline.app", "password": "adminpass123"},
        )
        assert resp.status_code == 200, resp.text
        assert "access_token" in resp.json()
    finally:
        limiter.enabled = False
        limiter.reset()


@pytest.mark.asyncio
async def test_ai_endpoint_is_rate_limited(client: AsyncClient, monkeypatch):
    """AI endpoints are rate limited. The AI service is stubbed so no external
    call is made; we only care that the limiter trips."""
    from app.rate_limit import AI_RATE_LIMIT

    class _FakeAI:
        async def improve_email(self, **kwargs):
            return {"subject": "s", "body": "b"}

    monkeypatch.setattr("app.routers.ai.get_ai_service", lambda: _FakeAI())

    limit = int(AI_RATE_LIMIT.split("/")[0])
    limiter.reset()
    limiter.enabled = True
    try:
        statuses = []
        for _ in range(limit + 1):
            r = await client.post(
                "/api/v1/ai/improve-email",
                json={"subject": "hi", "body": "there", "instruction": "shorten"},
            )
            statuses.append(r.status_code)
        # The calls before the limit trips must genuinely succeed; asserting only
        # on the final 429 would let a broken success path pass unnoticed.
        assert statuses[:limit] == [200] * limit, statuses
        assert statuses[-1] == 429
    finally:
        limiter.enabled = False
        limiter.reset()


# ─────────────────────────────── H6: docs gating ────────────────────────────

@pytest.mark.asyncio
async def test_docs_available_in_non_production(unauth_client: AsyncClient):
    """In the (non-production) test env, interactive docs are served and public."""
    assert app.main.app.docs_url == "/docs"
    assert (await unauth_client.get("/openapi.json")).status_code == 200


def test_docs_disabled_in_production(monkeypatch):
    """Rebuilding the app with app_env=production must disable /docs, /redoc,
    and /openapi.json."""
    monkeypatch.setenv("APP_ENV", "production")
    app.config.get_settings.cache_clear()
    try:
        importlib.reload(app.main)
        assert app.main.app.docs_url is None
        assert app.main.app.redoc_url is None
        assert app.main.app.openapi_url is None
    finally:
        # Restore the development app so later tests use the normal instance.
        monkeypatch.undo()
        app.config.get_settings.cache_clear()
        importlib.reload(app.main)
