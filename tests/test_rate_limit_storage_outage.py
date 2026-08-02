"""A rate-limit storage outage must not take authentication down with it.

Moving counters off process memory (see ``resolve_storage_uri``) buys correct
counts across the two uvicorn workers and buys a dependency: every
``@limiter.limit`` endpoint now talks to Redis before it does anything else, and
``/auth/login`` is one of them. A limiter that stops limiting is degraded; a
login that returns 500 because the *rate limiter's* backend blinked is down.

slowapi has a ``swallow_errors`` flag for this, so the question is only whether
it works — and it is worth asking rather than assuming, because the same library
has one already-documented trap of exactly this shape (``headers_enabled``,
which 500s successful calls on endpoints that don't declare a ``response``
kwarg). These tests hold the real app to the behaviour, so the answer is pinned
against a slowapi upgrade rather than re-derived from its source.

``conftest`` sets ``limiter.enabled = False`` for the whole session, so anything
here that does not turn it back on passes without touching the limiter at all —
including, tidily, an assertion that a dead Redis does no harm. Hence
:func:`test_the_limiter_is_actually_on`, which is the load-bearing test in the
file: it fails if the fixture ever stops arming the thing under test.
"""

import pytest

from app.rate_limit import AUTH_RATE_LIMIT, limiter

WRONG_LOGIN = {"email": "nobody@example.com", "password": "wrong-password"}
ALLOWED_PER_MINUTE = int(AUTH_RATE_LIMIT.split("/")[0])


@pytest.fixture
def live_limiter():
    """Arm the limiter for one test, and disarm it again afterwards.

    The limiter is a module-level singleton and its counters are process-global,
    so both the flag and the storage have to be put back — otherwise the next
    test in the session inherits a primed window and 429s something unrelated.
    """
    limiter.reset()
    limiter.enabled = True
    try:
        yield limiter
    finally:
        limiter.enabled = False
        limiter._storage_dead = False
        limiter.reset()


@pytest.fixture
def dead_storage(live_limiter):
    """Point the armed limiter at a Redis that refuses connections.

    Port 1 is reserved and unbound, so ``hit()`` raises promptly rather than
    hanging on a connect timeout.
    """
    from limits.storage import storage_from_string
    from limits.strategies import FixedWindowRateLimiter

    original_storage = live_limiter._storage
    original_limiter = live_limiter._limiter

    broken = storage_from_string("redis://127.0.0.1:1/0")
    live_limiter._storage = broken
    live_limiter._limiter = FixedWindowRateLimiter(broken)
    try:
        yield
    finally:
        live_limiter._storage = original_storage
        live_limiter._limiter = original_limiter


async def test_the_limiter_is_actually_on(unauth_client, live_limiter):
    """The control. Without it every other test here is vacuously true.

    ``conftest`` disables the limiter globally, which is the right default — it
    stops unrelated tests that hammer an endpoint from tripping a 429 — and it
    means a rate-limit test that forgets to re-enable it asserts nothing while
    looking exactly like one that works.
    """
    codes = [
        (await unauth_client.post("/api/v1/auth/login", json=WRONG_LOGIN)).status_code
        for _ in range(ALLOWED_PER_MINUTE + 3)
    ]

    assert codes[0] == 401, "the first attempt should reach the handler"
    assert 429 in codes, (
        f"the limiter never tripped over {len(codes)} attempts against a "
        f"{AUTH_RATE_LIMIT} limit: {codes}. It is switched off, so any outage "
        "test alongside this one is passing without contacting storage."
    )


async def test_login_still_answers_when_the_limit_store_is_unreachable(
    unauth_client, dead_storage
):
    """The endpoint may stop being limited. It may not start being broken."""
    response = await unauth_client.post("/api/v1/auth/login", json=WRONG_LOGIN)

    assert response.status_code != 500, (
        "a Redis outage turned /auth/login into a 500 — the rate limiter's "
        "backend became a hard dependency of signing in. Either swallow_errors "
        "stopped working or slowapi changed; storage must go back to memory:// "
        "before it can be worse than the doubled counters it replaced."
    )
    assert response.status_code == 401, "and it still answered what it was asked"


async def test_the_outage_fails_open_rather_than_closed(unauth_client, dead_storage):
    """Every request through the outage is served, not just the first.

    Failing *open* is the deliberate choice: unlimited logins for the duration of
    a Redis outage is a smaller problem than no logins at all, and the password
    check below is untouched either way.
    """
    codes = [
        (await unauth_client.post("/api/v1/auth/login", json=WRONG_LOGIN)).status_code
        for _ in range(ALLOWED_PER_MINUTE + 5)
    ]

    assert set(codes) == {401}, f"expected every attempt to be served: {codes}"


async def test_a_wrong_password_is_still_rejected_during_an_outage(
    unauth_client, dead_storage
):
    """Failing open on the *limit* must not fail open on the credential."""
    for _ in range(3):
        response = await unauth_client.post("/api/v1/auth/login", json=WRONG_LOGIN)
        assert response.status_code == 401


async def test_a_correct_password_still_works_during_an_outage(
    unauth_client, admin_user, dead_storage
):
    """The success path, which is the one that goes unexercised.

    A limiter fault that only breaks *successful* logins is this codebase's
    existing ``headers_enabled`` bug exactly — 401s returned cleanly while the
    right password 500'd. Asserting only on rejections would miss a repeat.
    """
    response = await unauth_client.post(
        "/api/v1/auth/login",
        json={"email": admin_user.email, "password": "adminpass123"},
    )

    assert response.status_code == 200, response.text
    assert "access_token" in response.json()
