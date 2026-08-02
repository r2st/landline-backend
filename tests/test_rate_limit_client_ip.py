"""The rate-limit key has to name the caller, not the proxy in front of them.

Every anonymous request — login, refresh, register — is keyed by client IP, and
behind nginx ``request.client.host`` is the Docker bridge gateway for all of
them. One key for the whole internet turns ``AUTH_RATE_LIMIT`` ("10/minute")
into ten logins a minute *platform-wide*, spendable by any stranger. These tests
pin the two halves of getting that right: read the forwarded chain by position
so it cannot be spoofed, and don't read it at all unless the deployment says a
proxy is there.
"""

import pytest
from starlette.requests import Request

from app.rate_limit import client_ip, org_or_ip_key, resolve_storage_uri


def make_request(headers: dict[str, str] | None = None, peer: str = "172.17.0.1"):
    """A Starlette ``Request`` with a given peer address and headers."""
    raw_headers = [
        (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": raw_headers,
            "client": (peer, 54321),
            "server": ("landline-api", 8000),
        }
    )


# --------------------------------------------------------------------------
# The bug, stated directly
# --------------------------------------------------------------------------


def test_two_callers_behind_the_proxy_get_different_keys(monkeypatch):
    """The regression this file exists for.

    Both requests arrive from the same peer, because both went through nginx.
    If they share a rate-limit key, the first caller to spend the login budget
    locks out the second — and every other customer.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    alice = make_request({"X-Forwarded-For": "203.0.113.7"}, peer="172.17.0.1")
    mallory = make_request({"X-Forwarded-For": "198.51.100.4"}, peer="172.17.0.1")

    assert client_ip(alice) != client_ip(mallory)
    assert client_ip(alice) == "203.0.113.7"


def test_without_the_setting_everyone_shares_the_peer_address(monkeypatch):
    """Unconfigured, the behaviour is the old one — and that is the safe default.

    Trusting a forwarded header by default would be worse than this: see
    :func:`test_a_spoofed_chain_cannot_mint_a_fresh_identity`. A deployment with
    no proxy in front genuinely has the peer as its caller.
    """
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)

    request = make_request({"X-Forwarded-For": "203.0.113.7"}, peer="172.17.0.1")
    assert client_ip(request) == "172.17.0.1"


# --------------------------------------------------------------------------
# Spoofing: position is what is trustworthy, not the header
# --------------------------------------------------------------------------


def test_a_spoofed_chain_cannot_mint_a_fresh_identity(monkeypatch):
    """The reason this reads from the right rather than the left.

    A caller who sends their own ``X-Forwarded-For`` has it *prepended* to
    nginx's — nginx appends the peer it saw. Reading the leftmost entry would
    let an attacker put a new value there on every request and never hit a
    limit at all, which is strictly worse than the shared bucket. The rightmost
    entry is the one nginx wrote and no client can reach.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    for forged in ("1.2.3.4", "9.9.9.9, 8.8.8.8", "not-an-ip"):
        request = make_request(
            {"X-Forwarded-For": f"{forged}, 198.51.100.4"}, peer="172.17.0.1"
        )
        assert client_ip(request) == "198.51.100.4"


def test_two_hops_reads_two_from_the_right(monkeypatch):
    """A CDN in front of nginx appends too, so the trusted position moves left."""
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")

    request = make_request(
        {"X-Forwarded-For": "1.2.3.4, 203.0.113.7, 10.0.0.5"}, peer="172.17.0.1"
    )
    assert client_ip(request) == "203.0.113.7"


def test_a_chain_shorter_than_the_hop_count_is_clamped(monkeypatch):
    """Over-configured, it must not walk off the end into a client-supplied entry.

    Fewer proxies ran than expected. The leftmost entry is then the earliest one
    still attributable to *a* proxy; taking it is conservative, and taking an
    index past the end would be an IndexError on the login path.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "5")

    request = make_request({"X-Forwarded-For": "203.0.113.7"}, peer="172.17.0.1")
    assert client_ip(request) == "203.0.113.7"


# --------------------------------------------------------------------------
# Degenerate headers — this runs on the unauthenticated path, so it may not raise
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},                                  # proxy configured, header absent
        {"X-Forwarded-For": ""},             # present and empty
        {"X-Forwarded-For": " , , "},        # separators only
    ],
)
def test_a_missing_or_empty_chain_falls_back_to_the_peer(monkeypatch, headers):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    assert client_ip(make_request(headers, peer="172.17.0.1")) == "172.17.0.1"


def test_x_real_ip_is_used_when_there_is_no_forwarded_chain(monkeypatch):
    """nginx overwrites X-Real-IP with the peer it saw, so it is trustworthy too.

    It is the fallback rather than the primary because it says nothing about how
    many hops it survived, whereas the chain's position does.
    """
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    request = make_request({"X-Real-IP": "203.0.113.7"}, peer="172.17.0.1")
    assert client_ip(request) == "203.0.113.7"


def test_x_real_ip_loses_to_the_forwarded_chain(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    request = make_request(
        {"X-Forwarded-For": "203.0.113.7", "X-Real-IP": "1.2.3.4"},
        peer="172.17.0.1",
    )
    assert client_ip(request) == "203.0.113.7"


@pytest.mark.parametrize("raw", ["", "yes", "one", "-1", "1.5"])
def test_an_unusable_hop_count_degrades_to_trusting_nothing(monkeypatch, raw):
    """Fail towards the shared bucket, never towards a spoofable header."""
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", raw)

    request = make_request({"X-Forwarded-For": "203.0.113.7"}, peer="172.17.0.1")
    assert client_ip(request) == "172.17.0.1"


def test_no_client_on_the_scope_does_not_raise(monkeypatch):
    """ASGI does not promise a ``client``; the login path must survive its absence."""
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    scope = dict(make_request().scope)
    scope["client"] = None
    assert client_ip(Request(scope)) == "127.0.0.1"


# --------------------------------------------------------------------------
# The org key still wins where it should
# --------------------------------------------------------------------------


def test_an_authenticated_caller_is_keyed_on_their_org(monkeypatch):
    """Tenants get their own budget; the IP path is only for anonymous callers."""
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    import uuid

    from app.security import create_access_token

    org_id = uuid.UUID("a9910000-0000-4000-8000-000000000001")
    token = create_access_token(uuid.uuid4(), "org_admin", org_id)
    request = make_request(
        {"Authorization": f"Bearer {token}", "X-Forwarded-For": "203.0.113.7"}
    )
    assert org_or_ip_key(request) == f"org:{org_id}"


@pytest.mark.parametrize(
    "auth",
    [
        "Bearer not-a-token",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "",
    ],
)
def test_an_unusable_credential_falls_back_to_the_scoped_ip(monkeypatch, auth):
    """A garbage token must not become one shared "undecodable" bucket either."""
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")

    request = make_request({"Authorization": auth, "X-Forwarded-For": "203.0.113.7"})
    assert org_or_ip_key(request) == "203.0.113.7"


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def test_storage_defaults_to_redis_when_the_app_has_one(monkeypatch):
    """Two uvicorn workers with ``memory://`` enforce every limit twice over."""
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    assert resolve_storage_uri() == "redis://redis:6379/0"


def test_an_explicit_storage_uri_wins(monkeypatch):
    monkeypatch.setenv("RATELIMIT_STORAGE_URI", "redis://elsewhere:6379/2")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379/0")

    assert resolve_storage_uri() == "redis://elsewhere:6379/2"


@pytest.mark.parametrize(
    "redis_url",
    ["", "postgresql://nope", "localhost:6379"],
)
def test_a_non_redis_url_does_not_become_a_storage_uri(monkeypatch, redis_url):
    """``limits`` raises on an unknown scheme, and it raises at import time."""
    monkeypatch.delenv("RATELIMIT_STORAGE_URI", raising=False)
    monkeypatch.setenv("REDIS_URL", redis_url)

    assert resolve_storage_uri() == "memory://"


def test_the_limiter_fails_open_rather_than_taking_login_down():
    """A Redis blip must degrade the limit, not 500 every ``/auth/login``.

    Moving storage off process memory buys correct counts and buys a dependency:
    an unreachable Redis makes every ``@limiter.limit`` endpoint raise. Swallowed,
    that is a limiter temporarily not limiting; unswallowed, it is authentication
    returning 500. ``limits`` logs the exception either way.
    """
    from app.rate_limit import limiter

    assert limiter._swallow_errors is True
