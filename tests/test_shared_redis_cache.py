"""
Direct unit tests for backend/cache.py's cross-instance Redis tier
(_SyncRedisClient, _build_shared_redis_client, redis_cache_get/set) and for
TTLCache's redis_prefix integration.

Mirrors tests/test_rate_limit.py's fake-client injection style: no real
Redis connection is ever opened here (tests/conftest.py sets
RATE_LIMIT_REDIS_URL="" for the whole suite), so every test below injects a
fake client via monkeypatch instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import backend.cache as cache_module
from backend.cache import TTLCache

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeSyncRedisClient:
    """Stands in for _SyncRedisClient itself (the try/except-wrapped
    module-level singleton), not the raw redis-py client it wraps -- so a
    simulated outage raises _RedisUnavailable, matching what
    _SyncRedisClient.get()/setex() actually raise after catching the real
    client's connection/timeout errors."""

    def __init__(self, get_result=None, raise_on_get=False, raise_on_setex=False):
        self._get_result = get_result
        self._raise_on_get = raise_on_get
        self._raise_on_setex = raise_on_setex
        self.setex_calls: list[tuple[str, int, str]] = []

    def get(self, key):
        if self._raise_on_get:
            raise cache_module._RedisUnavailable("redis unreachable")
        return self._get_result

    def setex(self, key, ttl_seconds, value):
        if self._raise_on_setex:
            raise cache_module._RedisUnavailable("redis unreachable")
        self.setex_calls.append((key, ttl_seconds, value))


@pytest.fixture(autouse=True)
def _clear_shared_client(monkeypatch):
    """Every test controls _shared_redis_client explicitly; start from None
    regardless of what a previous test (or module import) left behind."""
    monkeypatch.setattr(cache_module, "_shared_redis_client", None)
    yield


# ─── redis_cache_get / redis_cache_set (unconfigured) ───────────────────────

def test_redis_cache_get_returns_none_when_unconfigured():
    assert cache_module.redis_cache_get("k") is None


def test_redis_cache_set_is_a_noop_when_unconfigured():
    # Must not raise even though there's nothing to write to.
    cache_module.redis_cache_set("k", {"a": 1}, 60)


# ─── redis_cache_get / redis_cache_set (configured, fake client) ───────────

def test_redis_cache_get_returns_deserialized_json(monkeypatch):
    fake = _FakeSyncRedisClient(get_result='{"a": 1, "b": [1, 2]}')
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    assert cache_module.redis_cache_get("k") == {"a": 1, "b": [1, 2]}


def test_redis_cache_get_returns_none_on_miss(monkeypatch):
    fake = _FakeSyncRedisClient(get_result=None)
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    assert cache_module.redis_cache_get("k") is None


def test_redis_cache_get_returns_none_on_corrupt_payload(monkeypatch):
    fake = _FakeSyncRedisClient(get_result="not-json{{{")
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    assert cache_module.redis_cache_get("k") is None


def test_redis_cache_get_returns_none_when_store_unreachable(monkeypatch):
    fake = _FakeSyncRedisClient(raise_on_get=True)
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    assert cache_module.redis_cache_get("k") is None


def test_redis_cache_set_writes_serialized_json(monkeypatch):
    fake = _FakeSyncRedisClient()
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    cache_module.redis_cache_set("k", {"a": 1}, 60)
    assert fake.setex_calls == [("k", 60, '{"a": 1}')]


def test_redis_cache_set_coerces_ttl_to_a_positive_int(monkeypatch):
    fake = _FakeSyncRedisClient()
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    cache_module.redis_cache_set("k", "v", 0.4)
    assert fake.setex_calls[0][1] == 1  # max(1, int(0.4)) rounds down then floors at 1


def test_redis_cache_set_swallows_write_failure(monkeypatch):
    fake = _FakeSyncRedisClient(raise_on_setex=True)
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    # Must not raise.
    cache_module.redis_cache_set("k", "v", 60)


def test_redis_cache_set_swallows_non_json_serializable_value(monkeypatch):
    fake = _FakeSyncRedisClient()
    monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
    cache_module.redis_cache_set("k", object(), 60)  # must not raise
    assert fake.setex_calls == []


# ─── _build_shared_redis_client() ───────────────────────────────────────────

def test_build_shared_redis_client_returns_none_when_url_unset(monkeypatch):
    monkeypatch.delenv("RATE_LIMIT_REDIS_URL", raising=False)
    assert cache_module._build_shared_redis_client() is None


def test_build_shared_redis_client_returns_none_when_url_blank(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "   ")
    assert cache_module._build_shared_redis_client() is None


def test_build_shared_redis_client_returns_a_client_for_a_valid_url(monkeypatch):
    """redis.Redis.from_url() doesn't connect eagerly (no real network I/O
    at construction time), so this only needs a syntactically valid
    rediss:// URL to exercise the success path end-to-end."""
    monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "rediss://default:pw@example.invalid:6379")
    client = cache_module._build_shared_redis_client()
    assert isinstance(client, cache_module._SyncRedisClient)


# ─── _SyncRedisClient.get/setex wrap the underlying redis-py client ────────

class _FakeUnderlyingRedisPyClient:
    """Stands in for the real redis-py `redis.Redis` instance _SyncRedisClient
    wraps at self._client -- mirrors tests/test_rate_limit.py's
    _redis_store_with_fake_client() pattern for _RedisStore."""

    def __init__(self, get_result=None, raise_on_get=False, raise_on_setex=False):
        self._get_result = get_result
        self._raise_on_get = raise_on_get
        self._raise_on_setex = raise_on_setex
        self.setex_calls: list[tuple] = []

    def get(self, key):
        if self._raise_on_get:
            raise ConnectionError("redis-py connection error")
        return self._get_result

    def setex(self, key, ttl_seconds, value):
        if self._raise_on_setex:
            raise ConnectionError("redis-py connection error")
        self.setex_calls.append((key, ttl_seconds, value))


def _sync_redis_client_with_fake_underlying(fake_underlying):
    client = cache_module._SyncRedisClient.__new__(cache_module._SyncRedisClient)
    client._client = fake_underlying
    return client


def test_sync_redis_client_get_delegates_to_underlying_client():
    client = _sync_redis_client_with_fake_underlying(
        _FakeUnderlyingRedisPyClient(get_result="4.2"))
    assert client.get("k") == "4.2"


def test_sync_redis_client_get_wraps_connection_error():
    client = _sync_redis_client_with_fake_underlying(
        _FakeUnderlyingRedisPyClient(raise_on_get=True))
    with pytest.raises(cache_module._RedisUnavailable):
        client.get("k")


def test_sync_redis_client_setex_delegates_to_underlying_client():
    fake = _FakeUnderlyingRedisPyClient()
    client = _sync_redis_client_with_fake_underlying(fake)
    client.setex("k", 30, "v")
    assert fake.setex_calls == [("k", 30, "v")]


def test_sync_redis_client_setex_wraps_connection_error():
    client = _sync_redis_client_with_fake_underlying(
        _FakeUnderlyingRedisPyClient(raise_on_setex=True))
    with pytest.raises(cache_module._RedisUnavailable):
        client.setex("k", 30, "v")


_BOOT_SNIPPET = """
import backend.cache as cache_module
print(cache_module._shared_redis_client)
"""

_BASE_ENV = {
    "FLASK_ENV": "testing",
    "SEFARIA_API": "https://mock.sefaria.org/api",
    "SEFARIA_V3_API": "https://mock.sefaria.org/api/v3",
    "SUPABASE_URL": "https://mock.supabase.co",
    "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_mock-key",
    "SUPABASE_SECRET_KEY": "sb_secret_mock-key",
    "ANTHROPIC_API_KEY": "mock-anthropic-key",
    "GEMINI_API_KEY": "mock-gemini-key",
    "LOG_LEVEL": "ERROR",
}


def test_malformed_redis_url_never_crashes_app_boot():
    """A malformed RATE_LIMIT_REDIS_URL must fall back to in-process-only
    caching, never raise at import time -- same contract
    backend/rate_limit.py's _build_store() already guarantees, verified the
    same way (subprocess boot) since module-level state can't be reset
    in-process once a real import has happened."""
    env = {**os.environ, **_BASE_ENV, "RATE_LIMIT_REDIS_URL": "not-a-valid-redis-url"}
    result = subprocess.run(
        [sys.executable, "-c", _BOOT_SNIPPET],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"backend.cache failed to boot with a malformed RATE_LIMIT_REDIS_URL:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # redis.Redis.from_url() validates the URL scheme eagerly (ValueError,
    # no network I/O) -- "not-a-valid-redis-url" has no redis://
    # scheme, so _build_shared_redis_client() must catch that and fall back
    # to None rather than letting the ValueError propagate out of module
    # import.
    assert result.stdout.strip() == "None"


# ─── TTLCache redis_prefix integration ──────────────────────────────────────

class TestTTLCacheRedisIntegration:
    def test_memory_only_cache_never_touches_redis(self, monkeypatch):
        fake = _FakeSyncRedisClient(get_result='"should-not-be-read"')
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache()  # no redis_prefix
        cache.set("k", "v1")
        assert cache.get("k") == "v1"
        assert fake.setex_calls == []

    def test_set_writes_through_to_redis_with_prefix(self, monkeypatch):
        fake = _FakeSyncRedisClient()
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache(ttl=60, redis_prefix="pfx:")
        cache.set("k", {"a": 1})
        assert fake.setex_calls == [("pfx:k", 60, '{"a": 1}')]

    def test_get_falls_back_to_redis_on_local_miss(self, monkeypatch):
        fake = _FakeSyncRedisClient(get_result='"from-redis"')
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache(ttl=60, redis_prefix="pfx:")
        assert cache.get("k") == "from-redis"

    def test_get_prefers_local_memory_over_redis(self, monkeypatch):
        fake = _FakeSyncRedisClient(get_result='"from-redis"')
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache(ttl=60, redis_prefix="pfx:")
        cache.set("k", "from-memory")
        assert cache.get("k") == "from-memory"

    def test_redis_hit_backfills_local_memory(self, monkeypatch):
        fake = _FakeSyncRedisClient(get_result='"from-redis"')
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache(ttl=60, redis_prefix="pfx:")

        first = cache.get("k")
        assert first == "from-redis"

        # Second read must not hit Redis again -- backfilled locally.
        monkeypatch.setattr(fake, "get", lambda key: (_ for _ in ()).throw(
            AssertionError("Redis should not be queried again")))
        assert cache.get("k") == "from-redis"

    def test_get_or_fetch_honors_redis_tier(self, monkeypatch):
        fake = _FakeSyncRedisClient(get_result='"from-redis"')
        monkeypatch.setattr(cache_module, "_shared_redis_client", fake)
        cache = TTLCache(ttl=60, redis_prefix="pfx:")

        calls = []
        result = cache.get_or_fetch("k", lambda: calls.append(1) or "should-not-be-used")
        assert result == "from-redis"
        assert calls == []
