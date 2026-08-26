"""
Direct unit tests for backend/rate_limit.py's store abstraction additions
(plan.md §16.3-L3 / Prompt 29b): get()/setex() on _InMemoryStore and
_RedisStore, and the get_shared_store() accessor backend/cost_meter.py's
global cost breaker reuses instead of opening a second Redis connection.

RateLimitMiddleware's own incr()-based request-counting behavior already has
coverage via tests/test_ask.py's TestAskRateLimit; this file covers only the
new value-store surface those tests don't touch.
"""

from __future__ import annotations

import dataclasses

import pytest

from backend import rate_limit


# ─── _InMemoryStore.get / setex ─────────────────────────────────────────────

async def test_in_memory_store_get_missing_key_returns_none():
    store = rate_limit._InMemoryStore()
    assert await store.get("nope") is None


async def test_in_memory_store_setex_then_get_roundtrips():
    store = rate_limit._InMemoryStore()
    await store.setex("k", 30, "3.5")
    assert await store.get("k") == "3.5"


async def test_in_memory_store_get_after_ttl_expiry_returns_none(monkeypatch):
    store = rate_limit._InMemoryStore()
    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_now[0])

    await store.setex("k", 30, "value")
    fake_now[0] += 31  # past the 30s TTL

    assert await store.get("k") is None
    # Expiry also evicts the entry rather than leaving it to accumulate.
    assert "k" not in store._values


async def test_in_memory_store_get_just_before_ttl_expiry_still_hits(monkeypatch):
    store = rate_limit._InMemoryStore()
    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_now[0])

    await store.setex("k", 30, "value")
    fake_now[0] += 29

    assert await store.get("k") == "value"


async def test_in_memory_store_setex_evicts_oldest_when_at_capacity(monkeypatch):
    store = rate_limit._InMemoryStore()
    monkeypatch.setattr(store, "_MAX_KEYS", 2)

    await store.setex("a", 60, "1")
    await store.setex("b", 60, "2")
    await store.setex("c", 60, "3")

    assert len(store._values) == 2
    assert await store.get("c") == "3"


async def test_in_memory_store_values_and_buckets_are_independent():
    """setex()/get() write to a separate namespace from incr()'s
    sliding-window buckets -- confirms the two don't corrupt each other
    when a caller happens to reuse the same key string in both roles."""
    store = rate_limit._InMemoryStore()
    await store.setex("shared-key", 60, "not-a-count")
    count = await store.incr("shared-key", 60)
    assert count == 1
    assert await store.get("shared-key") == "not-a-count"


# ─── _RedisStore.get / setex ────────────────────────────────────────────────

class _FakeRedisClient:
    def __init__(self, get_result=None, raise_on_get=False, raise_on_setex=False):
        self._get_result = get_result
        self._raise_on_get = raise_on_get
        self._raise_on_setex = raise_on_setex
        self.setex_calls: list[tuple[str, int, str]] = []

    async def get(self, key):
        if self._raise_on_get:
            raise ConnectionError("redis unreachable")
        return self._get_result

    async def setex(self, key, ttl_seconds, value):
        if self._raise_on_setex:
            raise ConnectionError("redis unreachable")
        self.setex_calls.append((key, ttl_seconds, value))


def _redis_store_with_fake_client(fake_client):
    store = rate_limit._RateLimitStore.__new__(rate_limit._RedisStore)
    store._client = fake_client
    return store


async def test_redis_store_get_returns_value():
    store = _redis_store_with_fake_client(_FakeRedisClient(get_result="4.2"))
    assert await store.get("k") == "4.2"


async def test_redis_store_get_wraps_connection_error():
    store = _redis_store_with_fake_client(_FakeRedisClient(raise_on_get=True))
    with pytest.raises(rate_limit._StoreUnavailable):
        await store.get("k")


async def test_redis_store_setex_calls_client_setex():
    fake_client = _FakeRedisClient()
    store = _redis_store_with_fake_client(fake_client)
    await store.setex("k", 30, "4.2")
    assert fake_client.setex_calls == [("k", 30, "4.2")]


async def test_redis_store_setex_wraps_connection_error():
    store = _redis_store_with_fake_client(_FakeRedisClient(raise_on_setex=True))
    with pytest.raises(rate_limit._StoreUnavailable):
        await store.setex("k", 30, "4.2")


# ─── get_shared_store() ─────────────────────────────────────────────────────

def test_get_shared_store_returns_the_module_level_store_singleton():
    assert rate_limit.get_shared_store() is rate_limit._store


# ─── Identity-aware daily quota (plan.md §16.6 Phase 9c) ──────────────────────
# HTTP-roundtrip coverage of the per-minute allowance lives in
# tests/test_ask.py::TestAskIdentityAwareQuotas; this exercises the daily
# cap directly against _check() -- a real 200-request/day ceiling would be
# far too slow to prove one HTTP call at a time.

async def test_daily_quota_enforced_independently_of_the_per_minute_window(monkeypatch):
    fresh_store = rate_limit._InMemoryStore()
    monkeypatch.setattr(rate_limit, "_store", fresh_store)
    monkeypatch.setitem(
        rate_limit._POLICIES, "llm",
        dataclasses.replace(rate_limit._POLICIES["llm"], daily_max_requests=2),
    )

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_now[0])

    user_id = "user-daily-quota-test"
    ip = "203.0.113.70"

    # Each call is a full 60s+ window apart, so the per-minute bucket alone
    # would allow every one of these -- only the daily cap can reject them.
    for _ in range(2):
        allowed, _ = await rate_limit._check("llm", ip, user_id, "/ask")
        assert allowed is True
        fake_now[0] += 61

    allowed, retry_after = await rate_limit._check("llm", ip, user_id, "/ask")
    assert allowed is False
    assert retry_after == rate_limit._DAILY_WINDOW_SECONDS


async def test_daily_quota_does_not_apply_to_anonymous_callers(monkeypatch):
    fresh_store = rate_limit._InMemoryStore()
    monkeypatch.setattr(rate_limit, "_store", fresh_store)
    monkeypatch.setitem(
        rate_limit._POLICIES, "llm",
        dataclasses.replace(rate_limit._POLICIES["llm"], daily_max_requests=1),
    )

    fake_now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: fake_now[0])
    ip = "203.0.113.71"

    for _ in range(3):
        allowed, _ = await rate_limit._check("llm", ip, None, "/ask")
        assert allowed is True
        fake_now[0] += 61


# ─── Mitigation observability (plan.md §16.4) ─────────────────────────────────

async def test_check_logs_a_mitigation_event_on_per_minute_rejection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rate_limit, "log_mitigation",
        lambda tier, route_class, key_hash, route: calls.append((tier, route_class, route)),
    )
    fresh_store = rate_limit._InMemoryStore()
    monkeypatch.setattr(rate_limit, "_store", fresh_store)

    policy = rate_limit._POLICIES["cheap"]
    ip = "203.0.113.72"
    for _ in range(policy.max_requests):
        allowed, _ = await rate_limit._check("cheap", ip, None, "/api/some/route")
        assert allowed is True

    allowed, _ = await rate_limit._check("cheap", ip, None, "/api/some/route")
    assert allowed is False
    assert calls == [("middleware", "cheap", "/api/some/route")]


async def test_check_does_not_log_a_mitigation_event_when_allowed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        rate_limit, "log_mitigation",
        lambda *args: calls.append(args),
    )
    fresh_store = rate_limit._InMemoryStore()
    monkeypatch.setattr(rate_limit, "_store", fresh_store)

    allowed, _ = await rate_limit._check("cheap", "203.0.113.73", None, "/api/some/route")
    assert allowed is True
    assert calls == []
