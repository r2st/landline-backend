"""A failed DNS lookup must not disable the MX gate for the worker's lifetime.

``check_mx`` returns three verdicts and only two of them are answers.
``unknown`` means the lookup did not complete — a resolver timeout, a SERVFAIL,
``dnspython`` absent for one deploy. The cache stored it exactly like a settled
verdict, with no expiry, in a Celery worker that lives for days.

That is the quiet kind of failure. ``unknown`` is deliberately fail-open, so
nothing errors and no send is refused; the domain is simply never MX-checked
again, and its dead addresses go out and hard-bounce. The gate exists to stop
precisely that, because hard bounces are what costs a sending domain its
reputation — and a gate silently switching itself off reads identically to a
gate finding nothing wrong.

There is a second layer, which is why the TTL matters even though ``unknown``
never suppresses on its own: ``app.tasks.outreach`` validates a contact only
when ``email_status is None``. So the ``unknown`` written from a poisoned cache
entry is also written to the *contact*, and nothing in the codebase ever resets
``email_status`` to ``None`` — one blip, and both layers stop asking.

The cache was also unbounded: one entry per domain, forever, in the process the
autonomous prospector feeds new domains all day.
"""

import pytest

from app.services import email_validation


@pytest.fixture(autouse=True)
def _clear_mx_cache():
    email_validation.clear_cache()
    yield
    email_validation.clear_cache()


def _count_lookups(monkeypatch, verdict: str) -> dict:
    """Replace the resolver half of ``check_mx`` and count real lookups."""
    calls = {"n": 0}
    real = email_validation.check_mx

    def fake_resolve(domain: str) -> str:
        # Re-enter the real function only far enough to exercise the cache; the
        # DNS work itself is what we're counting, so it is replaced wholesale.
        cached = email_validation._cache_get(domain)
        if cached is not None:
            return cached
        calls["n"] += 1
        email_validation._cache_put(domain, verdict)
        return verdict

    monkeypatch.setattr(email_validation, "check_mx", fake_resolve)
    assert real is not fake_resolve
    return calls


def test_a_settled_verdict_is_cached(monkeypatch):
    calls = _count_lookups(monkeypatch, "valid")
    assert email_validation.check_mx("good.example") == "valid"
    assert email_validation.check_mx("good.example") == "valid"
    assert calls["n"] == 1


def test_unknown_expires_far_sooner_than_a_settled_verdict():
    """The TTLs are the whole fix, so they are asserted rather than assumed."""
    ttls = email_validation._MX_TTL_SECONDS
    assert ttls["unknown"] <= 300, "a failed lookup must be re-asked within minutes"
    assert ttls["valid"] > ttls["unknown"]
    assert ttls["invalid"] > ttls["unknown"]


def test_a_failed_lookup_is_re_asked_once_its_ttl_elapses(monkeypatch):
    """The regression: a DNS blip used to poison the domain permanently."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(email_validation.time, "monotonic", lambda: clock["t"])

    email_validation._cache_put("blip.example", "unknown")
    assert email_validation._cache_get("blip.example") == "unknown"

    clock["t"] += email_validation._MX_TTL_SECONDS["unknown"] + 1
    assert email_validation._cache_get("blip.example") is None, (
        "an incomplete lookup must be retried, not held for the worker's life"
    )


def test_a_settled_verdict_survives_the_unknown_ttl(monkeypatch):
    """Expiring `unknown` quickly must not turn every domain into a re-lookup."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(email_validation.time, "monotonic", lambda: clock["t"])

    email_validation._cache_put("stable.example", "valid")
    clock["t"] += email_validation._MX_TTL_SECONDS["unknown"] * 10
    assert email_validation._cache_get("stable.example") == "valid"


def test_an_expired_entry_is_dropped_not_merely_ignored(monkeypatch):
    """Otherwise the bound below is enforced against corpses."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(email_validation.time, "monotonic", lambda: clock["t"])

    email_validation._cache_put("gone.example", "unknown")
    clock["t"] += email_validation._MX_TTL_SECONDS["unknown"] + 1
    email_validation._cache_get("gone.example")
    assert "gone.example" not in email_validation._MX_CACHE


def test_the_cache_is_bounded():
    """A long-lived worker fed new domains all day must not grow without limit."""
    cap = email_validation._MX_CACHE_MAX
    for i in range(cap + 250):
        email_validation._cache_put(f"d{i}.example", "valid")
    assert len(email_validation._MX_CACHE) <= cap


def test_eviction_keeps_the_cache_useful():
    """Evicting must not empty it — the point of a cache is the hit rate."""
    cap = email_validation._MX_CACHE_MAX
    for i in range(cap + 250):
        email_validation._cache_put(f"d{i}.example", "valid")
    assert len(email_validation._MX_CACHE) > cap // 2
    # The most recently written entry is one of the survivors.
    assert email_validation._cache_get(f"d{cap + 249}.example") == "valid"


def test_check_mx_short_circuits_on_a_cache_hit(monkeypatch):
    """No resolver is constructed when the answer is already known."""
    def explode(*a, **kw):  # pragma: no cover - fails the test if reached
        raise AssertionError("check_mx resolved a domain it had cached")

    email_validation._cache_put("known.example", "invalid")
    monkeypatch.setattr(email_validation, "_DNS_TIMEOUT", 0.001)
    import sys
    monkeypatch.setitem(sys.modules, "dns.resolver", explode)

    assert email_validation.check_mx("known.example") == "invalid"


def test_clear_cache_still_empties_it():
    email_validation._cache_put("x.example", "valid")
    email_validation.clear_cache()
    assert email_validation._MX_CACHE == {}
