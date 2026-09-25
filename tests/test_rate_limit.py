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

import asyncio
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
    store._breaker = rate_limit._CircuitBreaker()
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


# ─── _RateLimitStore base class ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "call",
    [
        lambda store: store.incr("k", 60),
        lambda store: store.get("k"),
        lambda store: store.setex("k", 60, "v"),
    ],
    ids=["incr", "get", "setex"],
)
async def test_base_store_operations_are_abstract(call):
    store = rate_limit._RateLimitStore()

    with pytest.raises(NotImplementedError):
        await call(store)


# ─── _InMemoryStore.incr: LRU eviction ──────────────────────────────────────

async def test_in_memory_store_incr_evicts_least_recently_used_key_at_capacity(monkeypatch):
    store = rate_limit._InMemoryStore()
    monkeypatch.setattr(store, "_MAX_KEYS", 2)

    await store.incr("a", 60)
    await store.incr("b", 60)
    await store.incr("a", 60)  # touching "a" makes "b" the least recently used
    await store.incr("c", 60)  # at capacity -> evicts "b", not the older-inserted "a"

    assert list(store._buckets) == ["a", "c"]
    assert await store.incr("a", 60) == 3  # active caller's counter survived
    assert await store.incr("b", 60) == 1  # evicted caller starts from scratch


# ─── _RedisStore.incr and per-event-loop client rebinding ───────────────────

class _CountingRedisClient:
    """Fixed-window Redis double: records incr/expire and can be told to fail."""

    def __init__(self, counts=(1,), raise_on_incr=False, raise_on_expire=False):
        self._counts = iter(counts)
        self._raise_on_incr = raise_on_incr
        self._raise_on_expire = raise_on_expire
        self.expire_calls: list[tuple[str, int]] = []

    async def incr(self, key):
        if self._raise_on_incr:
            raise ConnectionError("redis unreachable")
        return next(self._counts)

    async def expire(self, key, seconds):
        if self._raise_on_expire:
            raise ConnectionError("redis unreachable")
        self.expire_calls.append((key, seconds))


async def test_redis_store_incr_sets_the_window_only_when_it_creates_the_key():
    client = _CountingRedisClient(counts=(1, 2, 3))
    store = _redis_store_with_fake_client(client)

    assert [await store.incr("k", 60) for _ in range(3)] == [1, 2, 3]

    # A steady stream must not keep pushing the fixed window forward.
    assert client.expire_calls == [("k", 60)]


async def test_redis_store_incr_returns_an_int_for_a_string_reply():
    store = _redis_store_with_fake_client(_CountingRedisClient(counts=("2",)))
    assert await store.incr("k", 60) == 2


@pytest.mark.parametrize(
    "client",
    [_CountingRedisClient(raise_on_incr=True), _CountingRedisClient(raise_on_expire=True)],
    ids=["incr-fails", "expire-fails"],
)
async def test_redis_store_incr_wraps_connection_errors(client):
    store = _redis_store_with_fake_client(client)
    with pytest.raises(rate_limit._StoreUnavailable, match="redis unreachable"):
        await store.incr("k", 60)


@pytest.fixture
def built_redis_clients(monkeypatch):
    """Replace redis.asyncio.Redis.from_url so building a store makes no
    connection; every client it hands out is recorded in creation order."""
    import redis.asyncio as redis_asyncio

    built: list[tuple[str, object]] = []

    def fake_from_url(url, **kwargs):
        client = object()
        built.append((url, client))
        return client

    monkeypatch.setattr(redis_asyncio.Redis, "from_url", staticmethod(fake_from_url))
    return built


async def test_redis_store_reuses_its_client_on_the_same_event_loop(built_redis_clients):
    store = rate_limit._RedisStore("redis://example.invalid:6379/0")

    first = store._client_for_current_loop()
    second = store._client_for_current_loop()

    assert first is second is built_redis_clients[0][1]
    assert len(built_redis_clients) == 1


async def test_redis_store_builds_a_fresh_client_when_the_event_loop_changed(built_redis_clients):
    store = rate_limit._RedisStore("redis://example.invalid:6379/0")
    original = store._client_for_current_loop()
    store._client_loop = object()  # a loop that is no longer the running one

    rebuilt = store._client_for_current_loop()

    assert rebuilt is not original
    assert [url for url, _ in built_redis_clients] == ["redis://example.invalid:6379/0"] * 2
    # Bound to the running loop now, so a further call keeps the new client.
    assert store._client_for_current_loop() is rebuilt


async def test_redis_store_rebuilds_a_missing_client(built_redis_clients):
    store = rate_limit._RedisStore("redis://example.invalid:6379/0")
    store._client = None

    assert store._client_for_current_loop() is built_redis_clients[-1][1]
    assert len(built_redis_clients) == 2


# ─── _RedisStore circuit breaker ────────────────────────────────────────────
# A down Redis costs every caller the full 2s connect timeout; after one
# failure the store must skip Redis for the cooldown, then admit one probe.

class _FlakyRedisClient:
    """Counts every command; fails while ``down`` is True."""

    def __init__(self, down=True):
        self.down = down
        self.calls = 0

    async def incr(self, key):
        self.calls += 1
        if self.down:
            raise ConnectionError("Timeout connecting to server")
        return 2  # never 1, so no expire() round trip to account for

    async def get(self, key):
        self.calls += 1
        if self.down:
            raise ConnectionError("Timeout connecting to server")
        return "v"

    async def setex(self, key, ttl_seconds, value):
        self.calls += 1
        if self.down:
            raise ConnectionError("Timeout connecting to server")


@pytest.fixture
def fake_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: now[0])
    return now


async def test_breaker_opens_after_a_store_failure_and_reports_it_once(fake_clock):
    store = _redis_store_with_fake_client(_FlakyRedisClient(down=True))

    with pytest.raises(rate_limit._StoreUnavailable) as first:
        await store.incr("k", 60)

    assert not isinstance(first.value, rate_limit._CircuitOpen)
    assert first.value.report is True
    assert store._breaker._opened_at == fake_clock[0]


async def test_open_breaker_skips_redis_for_every_command_during_cooldown(fake_clock):
    client = _FlakyRedisClient(down=True)
    store = _redis_store_with_fake_client(client)
    with pytest.raises(rate_limit._StoreUnavailable):
        await store.incr("k", 60)
    client.down = False  # even a healed Redis is not consulted until cooldown ends

    fake_clock[0] += rate_limit._BREAKER_COOLDOWN_SECONDS - 1
    for call in (store.incr("k", 60), store.get("k"), store.setex("k", 30, "v")):
        with pytest.raises(rate_limit._CircuitOpen) as short_circuit:
            await call
        assert short_circuit.value.report is False

    assert client.calls == 1


async def test_half_open_probe_success_closes_the_breaker(fake_clock):
    client = _FlakyRedisClient(down=True)
    store = _redis_store_with_fake_client(client)
    with pytest.raises(rate_limit._StoreUnavailable):
        await store.incr("k", 60)

    client.down = False
    fake_clock[0] += rate_limit._BREAKER_COOLDOWN_SECONDS

    assert await store.incr("k", 60) == 2  # the probe
    assert store._breaker._opened_at is None
    assert await store.get("k") == "v"  # closed: traffic flows normally again
    assert client.calls == 3


async def test_half_open_probe_failure_reopens_and_reports_again(fake_clock):
    client = _FlakyRedisClient(down=True)
    store = _redis_store_with_fake_client(client)
    with pytest.raises(rate_limit._StoreUnavailable):
        await store.incr("k", 60)

    fake_clock[0] += rate_limit._BREAKER_COOLDOWN_SECONDS
    with pytest.raises(rate_limit._StoreUnavailable) as probe:
        await store.incr("k", 60)

    assert not isinstance(probe.value, rate_limit._CircuitOpen)
    assert probe.value.report is True  # one report per cooldown window
    assert store._breaker._opened_at == fake_clock[0]  # a fresh cooldown
    with pytest.raises(rate_limit._CircuitOpen):
        await store.incr("k", 60)
    assert client.calls == 2


def test_breaker_admits_only_one_half_open_probe_at_a_time(fake_clock):
    breaker = rate_limit._CircuitBreaker()
    breaker.record_failure(is_probe=False)
    fake_clock[0] += rate_limit._BREAKER_COOLDOWN_SECONDS

    assert breaker.acquire() is True
    with pytest.raises(rate_limit._CircuitOpen):
        breaker.acquire()


def test_late_failure_of_a_call_admitted_before_the_trip_is_not_reported_again(fake_clock):
    breaker = rate_limit._CircuitBreaker()
    assert breaker.record_failure(is_probe=False) is True
    opened_at = breaker._opened_at

    fake_clock[0] += 1
    assert breaker.record_failure(is_probe=False) is False
    assert breaker._opened_at == opened_at  # does not extend the cooldown


async def test_cancelled_probe_frees_the_probe_slot(fake_clock):
    class _HangingClient:
        async def get(self, key):
            await asyncio.Event().wait()

    store = _redis_store_with_fake_client(_HangingClient())
    store._breaker.record_failure(is_probe=False)
    fake_clock[0] += rate_limit._BREAKER_COOLDOWN_SECONDS

    probe = asyncio.ensure_future(store.get("k"))
    await asyncio.sleep(0)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe

    # The next caller becomes the probe instead of the breaker wedging open.
    assert store._breaker.acquire() is True


async def test_check_does_not_log_or_capture_a_short_circuited_failure(monkeypatch, caplog):
    class _OpenCircuitStore(rate_limit._RateLimitStore):
        async def incr(self, key, window_seconds):
            raise rate_limit._CircuitOpen()

    captured = []
    monkeypatch.setattr(rate_limit, "_store", _OpenCircuitStore())
    monkeypatch.setattr(rate_limit, "_capture_backend_error", lambda *a, **k: captured.append(a))

    with caplog.at_level("ERROR", logger=rate_limit.logger.name):
        assert await rate_limit._check("cheap", "192.0.2.50", None, "/api/x") == (True, 60)
        assert await rate_limit._check("llm", "192.0.2.50", None, "/ask") == (False, 60)

    assert captured == []
    assert "rate_limit_store_unavailable" not in caplog.text


# ─── _build_store() ─────────────────────────────────────────────────────────

def test_build_store_uses_redis_when_a_url_is_configured(monkeypatch, built_redis_clients):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_REDIS_URL", "redis://example.invalid:6379/0")

    store = rate_limit._build_store()

    assert isinstance(store, rate_limit._RedisStore)


def test_build_store_falls_back_in_process_when_no_url_is_set(monkeypatch, caplog):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_REDIS_URL", "")

    with caplog.at_level("WARNING", logger=rate_limit.logger.name):
        store = rate_limit._build_store()

    assert isinstance(store, rate_limit._InMemoryStore)
    assert "RATE_LIMIT_REDIS_URL is not set" in caplog.text


def test_build_store_never_crashes_boot_on_an_invalid_url(monkeypatch, caplog):
    monkeypatch.setattr(rate_limit, "RATE_LIMIT_REDIS_URL", "not-a-redis-url")

    with caplog.at_level("CRITICAL", logger=rate_limit.logger.name):
        store = rate_limit._build_store()

    assert isinstance(store, rate_limit._InMemoryStore)
    assert "RATE_LIMIT_REDIS_URL is set but invalid" in caplog.text
    # The password-bearing URL itself must not be echoed into the log.
    assert "not-a-redis-url" not in caplog.text


# ─── RateLimitMiddleware kill switch ────────────────────────────────────────

async def test_middleware_passes_requests_through_when_rate_limiting_is_disabled(
    fastapi_client, monkeypatch
):
    class _ForbiddenStore(rate_limit._RateLimitStore):
        async def incr(self, key, window_seconds):
            raise AssertionError("the store must not be consulted when disabled")

    monkeypatch.setattr(rate_limit, "RATELIMIT_ENABLED", False)
    monkeypatch.setattr(rate_limit, "_store", _ForbiddenStore())

    response = await fastapi_client.get("/api/async/health")

    assert response.status_code == 200


async def test_static_assets_bypass_the_limiter_entirely(fastapi_client, monkeypatch):
    class _ForbiddenStore(rate_limit._RateLimitStore):
        async def incr(self, key, window_seconds):
            raise AssertionError("the store must not be consulted for /static/*")

    monkeypatch.setattr(rate_limit, "_store", _ForbiddenStore())

    response = await fastapi_client.get("/static/style.css")

    assert response.status_code == 200


def test_only_static_paths_are_exempt():
    assert rate_limit.is_exempt("/static/css/ai.css")
    assert not rate_limit.is_exempt("/static")
    assert not rate_limit.is_exempt("/api/static/x")
    assert not rate_limit.is_exempt("/ask")
