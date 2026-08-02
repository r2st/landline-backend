"""Shared slowapi rate limiter.

Kept in its own module so routers can import the limiter without creating an
import cycle through ``app.main``. Endpoints opt in with the
``@limiter.limit(...)`` decorator and must take a ``request: Request`` param.

Authenticated requests are keyed on the caller's org; anonymous ones on the
client IP. Getting "the client IP" right behind a reverse proxy is the whole
subject of :func:`client_ip` below, and getting it wrong is not a degraded
limit but a platform-wide one. Storage defaults to Redis when the app has a
Redis, because in-memory counters are per-worker and the API runs two workers.
"""

import logging
import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

logger = logging.getLogger(__name__)

# Sensible defaults; can be overridden per-endpoint at the decorator.
AUTH_RATE_LIMIT = "10/minute"      # login / refresh — throttle credential stuffing
AI_RATE_LIMIT = "30/minute"        # AI endpoints — throttle expensive LLM calls


def _trusted_proxy_hops() -> int:
    """How many reverse proxies sit in front of this process.

    Read per-call rather than captured at import so tests and a redeploy can
    change it without reconstructing the limiter. It is a tiny env lookup on a
    path that is already doing JWT decoding.
    """
    raw = os.getenv("TRUSTED_PROXY_HOPS", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        logger.error(
            "TRUSTED_PROXY_HOPS=%r is not an integer; treating as 0. Rate limits "
            "will be keyed on the peer address, which behind a proxy is one "
            "shared value for every caller.",
            raw,
        )
        return 0


def client_ip(request: Request) -> str:
    """The address to charge this request to, given ``TRUSTED_PROXY_HOPS``.

    ``request.client.host`` is the peer that opened the socket. That is the
    caller only when nothing is in front of the app. In production nginx
    proxies to ``127.0.0.1:8001``, which is a Docker published port, so the
    peer every request presents is the bridge gateway — **one address, shared
    by the entire internet**. Uvicorn ships a ``ProxyHeadersMiddleware`` that
    would fix this, but its ``forwarded_allow_ips`` defaults to ``127.0.0.1``
    and the gateway is not that, so it declines to rewrite anything and the
    default is silent about it.

    What that costs is not a loose limit but an inverted one. ``AUTH_RATE_LIMIT``
    is 10/minute, and with a single key it is 10 logins per minute *for the
    whole platform*: any unauthenticated stranger can spend the budget and lock
    every customer out of signing in. A control installed to stop credential
    stuffing becomes the cheapest denial of service the API offers.

    The fix cannot be "read ``X-Forwarded-For``", because that header is
    attacker-controlled: trusting its leftmost entry lets anyone mint a fresh
    rate-limit identity per request and removes the limit altogether — strictly
    worse than the shared bucket. What is trustworthy is position. Each proxy
    *appends* the peer it saw, so with one hop in front (nginx's
    ``$proxy_add_x_forwarded_for``) the **last** entry is the address nginx
    itself observed and the only one no client could have written. With ``n``
    hops, that is the entry ``n`` from the right; everything to its left is
    hearsay.

    Hence the count rather than a boolean, and hence the default of 0 — trust
    nothing unless the deployment says how much is in front of it. A too-low
    setting degrades to the shared bucket, which is safe; a too-high one starts
    reading client-supplied entries, so it is clamped to the entries the header
    actually has.
    """
    hops = _trusted_proxy_hops()
    if hops:
        forwarded = request.headers.get("X-Forwarded-For", "")
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if chain:
            # Clamp: a chain shorter than the configured hop count means fewer
            # proxies ran than expected, so the leftmost entry is the earliest
            # one still attributable to a proxy rather than to the caller.
            return chain[-min(hops, len(chain))]
        # nginx sets X-Real-IP to the peer *it* saw and overwrites any inbound
        # copy, so it is trustworthy on the same terms as the last XFF entry —
        # and it is what is there when a proxy forwards without appending.
        real_ip = request.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip
    return get_remote_address(request)


def org_or_ip_key(request: Request) -> str:
    """Rate-limit key: the caller's organization, falling back to client IP.

    In a multi-tenant SaaS, limits should be per *tenant*, not per IP — a whole
    org behind one office NAT shouldn't share a single budget, and one noisy
    tenant shouldn't be throttled by another. The org is read from the Bearer
    token's ``org_id`` claim (decoded cheaply, no DB hit). Anonymous requests
    (login/register — which carry no token) fall back to the client IP, which is
    exactly what you want for credential-stuffing protection. Platform admins
    (``org_id`` is null) also fall back to IP.

    The fallback is the interesting half: it is the branch every unauthenticated
    request takes, so it is the one protecting login. See :func:`client_ip`.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            from app.security import decode_token

            org_id = decode_token(auth[7:]).get("org_id")
            if org_id:
                return f"org:{org_id}"
        except Exception:  # invalid/expired token → fall back to IP
            pass
    return client_ip(request)


def resolve_storage_uri() -> str:
    """Where the counters live. Redis if there is one, memory if there isn't.

    ``memory://`` is per-process, and the API runs ``uvicorn --workers 2``, so
    in-memory counters mean each limit is enforced twice over — a caller gets
    roughly ``2 * limit`` and which worker they land on decides whether the
    request that trips it is the eleventh or the twentieth. Celery does not
    share them at all.

    The app already requires Redis (Celery broker, result backend), so the
    previous default of ``memory://`` was not "no Redis available", it was
    "nobody set the second variable naming the Redis we already have".
    ``RATELIMIT_STORAGE_URI`` still wins when set — it exists so limits can be
    pointed at a different database or instance.
    """
    explicit = os.getenv("RATELIMIT_STORAGE_URI")
    if explicit:
        return explicit
    redis_url = os.getenv("REDIS_URL")
    if redis_url and redis_url.startswith(("redis://", "rediss://", "unix://")):
        return redis_url
    return "memory://"


class RateLimitStateMiddleware:
    """Seed ``request.state.view_rate_limit`` so a storage outage can be survived.

    ``swallow_errors`` below is necessary and not sufficient, and the gap is only
    visible on the success path. When ``limits`` raises — Redis unreachable —
    slowapi catches it, logs, and returns *without* having reached the line that
    sets ``request.state.view_rate_limit``. Its endpoint wrapper then reads that
    attribute unconditionally, before any ``headers_enabled`` check::

        self._inject_headers(kwargs.get("response"), request.state.view_rate_limit)

    so the swallowed error comes back as ``AttributeError: 'State' object has no
    attribute 'view_rate_limit'`` — a 500, which is the outcome swallowing was
    supposed to prevent.

    What makes it worth a middleware rather than a comment is *which* requests
    it hits. A raised ``HTTPException`` unwinds before that line, so during a
    Redis outage every rejected login returns a clean 401 and only a **correct**
    password 500s. That is this codebase's ``headers_enabled`` bug again, to the
    line: failures fine, success broken, and invisible to any test that only
    checks that bad credentials are refused.

    Pre-seeding the attribute is a one-key dict write per request and leaves
    normal operation untouched — slowapi overwrites it with the real limit on
    every request that reaches storage successfully.

    Pure ASGI rather than ``BaseHTTPMiddleware``: this runs on every request and
    has no reason to wrap the response cycle in an anyio task group.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Starlette's ``request.state`` is a view onto ``scope["state"]``,
            # so writing it here is what the endpoint wrapper reads later.
            scope.setdefault("state", {})["view_rate_limit"] = None
        await self.app(scope, receive, send)


# headers_enabled must stay False: slowapi injects the X-RateLimit-* headers by
# reaching for a ``response: Response`` kwarg on the endpoint, and raises if it
# isn't there. Endpoints returning a pydantic model (the norm here) don't declare
# one, so enabling this 500s every *successful* call while failures still return
# cleanly — a login that rejects wrong passwords with 401 but 500s on the right
# one. Limits themselves are enforced independently of these headers.
#
# swallow_errors is the deliberate half of moving storage to Redis. Without it a
# Redis blip turns every rate-limited endpoint — login included — into a 500,
# i.e. an outage in the storage backend becomes an outage in authentication. A
# limiter that fails open is degraded; one that fails closed on /auth/login is
# down. slowapi logs the exception, so the degradation is visible rather than
# silent. It only holds together with RateLimitStateMiddleware installed — see
# that class for the half of the outage swallowing does not cover.
limiter = Limiter(
    key_func=org_or_ip_key,
    storage_uri=resolve_storage_uri(),
    headers_enabled=False,
    swallow_errors=True,
)
