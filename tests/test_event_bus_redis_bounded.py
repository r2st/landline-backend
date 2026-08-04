"""The event bus's "best-effort" streaming had no deadline.

``EventBus.publish`` does two things: an ``XADD`` onto a Redis stream, described
throughout the module as optional and best-effort, and an in-process dispatch to
the workflow engine, which is the half that has to happen. The optional half was
the one that could hang.

Every other Redis client in this codebase — ``send_limiter``, ``send_pacing``,
``cache``, ``api_key_auth`` — is built with ``socket_connect_timeout=0.5`` and
``socket_timeout=0.5``, for the same reason each time: a fallback that waits
forever is not a fallback. ``event_bus`` was built with neither. An unreachable
*host* still failed fast (connection refused is immediate), which is what made
this survive — the failure it did not cover is a host that completes the TCP
handshake and then stops answering: a wedged Redis, a failover mid-flight, a
firewall dropping an established connection. There ``ping()`` and ``xadd()``
wait on a socket with no deadline.

And this is not a background path. ``publish`` is awaited inline by the workflow
``emit`` endpoint and by every internal event a request raises, so the optional
work is what holds the request open.

Asserted against the constructor arguments rather than by hanging a socket: the
bug is a missing option, and a timing test for it would either be slow or lie.
"""

import pytest

from app.services import event_bus as event_bus_module
from app.services.event_bus import EventBus


class _FakeRedis:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def ping(self):
        return True


@pytest.mark.asyncio
async def test_the_client_is_built_with_bounded_socket_timeouts(monkeypatch):
    """A hung Redis must fail in under a second, not hold the request."""
    captured = {}

    def _fake_from_url(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return _FakeRedis(**kwargs)

    # Patched on the real submodule, not over ``sys.modules``. ``import
    # redis.asyncio as aioredis`` binds through ``getattr(redis, "asyncio")``
    # whenever the parent package already carries the attribute — which it does
    # as soon as anything else in the suite has imported it — so a
    # ``sys.modules`` substitution is silently bypassed and the *real*
    # ``from_url`` runs. Standalone the fallback path is taken and the
    # substitution works, so the seam only fails in a full run, ordered.
    import redis.asyncio as real_aioredis

    monkeypatch.setattr(real_aioredis, "from_url", _fake_from_url)

    bus = EventBus()
    client = await bus._get_redis()

    assert client is not None, "the fake client should have been accepted"
    assert captured["socket_connect_timeout"] == 0.5
    assert captured["socket_timeout"] == 0.5


@pytest.mark.asyncio
async def test_a_dead_redis_still_lets_the_event_dispatch(monkeypatch):
    """The half that matters survives the half that doesn't.

    Streaming is optional; running the workflows an event triggers is not. A
    publish whose Redis is gone must still return a dispatch result, with
    ``streamed`` saying plainly that it didn't reach the stream.
    """
    bus = EventBus()

    async def _no_redis():
        return None

    monkeypatch.setattr(bus, "_get_redis", _no_redis)

    dispatched = {}

    class _FakeEngine:
        async def dispatch_event(self, db, event_type, payload, org_id):
            dispatched["event_type"] = event_type
            return {"workflows_matched": 0, "runs": []}

    import app.services.workflow_engine as we

    monkeypatch.setattr(we, "get_workflow_engine", lambda: _FakeEngine())

    result = await bus.publish(None, "contact.created", {"id": "x"})

    assert result["streamed"] is False
    assert result["stream_id"] is None
    assert dispatched["event_type"] == "contact.created"


def test_every_redis_client_in_the_codebase_is_bounded():
    """The convention, enforced rather than remembered.

    Five modules build a Redis client and four of them had timeouts. Nothing
    compared them, so the fifth read as correct in its own file — the arguments
    that are missing are exactly the ones there is nothing to see about.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    unbounded = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "from_url"):
                continue
            names = {kw.arg for kw in node.keywords}
            if not {"socket_connect_timeout", "socket_timeout"} <= names:
                unbounded.append(f"{path.relative_to(root.parent)}:{node.lineno}")

    assert unbounded == [], (
        "Redis clients built without socket timeouts — a wedged host hangs the "
        f"caller forever: {unbounded}"
    )
