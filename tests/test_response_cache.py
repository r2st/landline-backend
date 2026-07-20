"""Short-TTL response cache for dashboard/analytics aggregates.

The cache is disabled in the suite by default (RESPONSE_CACHE_ENABLED=false), so
these tests inject a fake Redis to exercise the hit/miss + tenant-isolation logic
directly.
"""

import uuid

import pytest

from app.services import cache


class FakeRedis:
    def __init__(self):
        self.store: dict = {}
        self.sets = 0

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.sets += 1
        self.store[key] = value


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()

    async def _get_redis():
        return fake

    monkeypatch.setattr(cache, "_get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_cached_computes_on_miss_and_serves_on_hit(fake_redis):
    calls = {"n": 0}

    async def builder():
        calls["n"] += 1
        return {"value": 42}

    first = await cache.cached("k1", builder)
    second = await cache.cached("k1", builder)

    assert first == {"value": 42}
    assert second == {"value": 42}
    # Builder ran once; the second call was served from cache.
    assert calls["n"] == 1
    assert fake_redis.sets == 1


@pytest.mark.asyncio
async def test_cached_serializes_pydantic_models(fake_redis):
    from app.schemas.dashboard import DashboardKPIs

    async def builder():
        return DashboardKPIs(
            emails_sent_today=1, open_rate=10.0, replies_this_week=2,
            deals_won_this_month=0, deals_won_value=0.0, active_pipeline_value=0.0,
            avg_response_time_hours=None,
        )

    first = await cache.cached("kpis", builder)
    second = await cache.cached("kpis", builder)
    # Hit returns the JSON dict form.
    assert second["emails_sent_today"] == 1
    assert second["open_rate"] == 10.0


@pytest.mark.asyncio
async def test_cache_is_org_scoped(fake_redis):
    from app.org_context import reset_current_org_id, set_current_org_id

    org_a, org_b = uuid.uuid4(), uuid.uuid4()

    async def builder_a():
        return {"org": "a"}

    async def builder_b():
        return {"org": "b"}

    tok = set_current_org_id(org_a)
    try:
        await cache.cached("shared-key", builder_a)
    finally:
        reset_current_org_id(tok)

    tok = set_current_org_id(org_b)
    try:
        # Same logical key, different org → must NOT see org A's value.
        result = await cache.cached("shared-key", builder_b)
    finally:
        reset_current_org_id(tok)

    assert result == {"org": "b"}
    # Two distinct scoped keys stored.
    assert len(fake_redis.store) == 2


@pytest.mark.asyncio
async def test_cache_fails_open_when_redis_unavailable(monkeypatch):
    async def _no_redis():
        return None

    monkeypatch.setattr(cache, "_get_redis", _no_redis)

    calls = {"n": 0}

    async def builder():
        calls["n"] += 1
        return {"v": 1}

    await cache.cached("x", builder)
    await cache.cached("x", builder)
    # No cache → builder runs every time, response still correct.
    assert calls["n"] == 2
