"""
Characterization tests for backend/sefaria_library.py's memory-tier cache
(_cache / _cached_get).

Update (plan.md §5.2, Phase 5): _cache is now a backend.cache.TTLCache
instance rather than a hand-rolled dict. An earlier pass deliberately left
this cache as a plain dict because TTLCache fixes an entry's expiry at
*write* time (from the ttl passed to .set()), whereas the old dict
re-evaluated the ttl argument passed to *each read* against the stored
timestamp — a real API difference. Phase 5 migrates it anyway per plan.md's
explicit instruction. This is not observable in practice: every call site
passes a single, constant ttl per URL shape (e.g. the /name/ lookup always
uses ttl=43200, /texts/ always ttl=86400), so no call ever re-reads the same
URL with a different ttl than it was written with. These tests now exercise
the TTLCache-backed contract directly instead of poking the old dict's
internal {'data':..., 'ts':...} shape.
"""

from __future__ import annotations

import pytest

import backend.cache as cache_module
import backend.sefaria_library as sefaria_library_module


class _SharedFakeRedis:
    """Dict-backed fake standing in for a real shared Redis deployment --
    see tests/test_sefaria_library.py::_SharedFakeRedis for the fuller
    rationale (no live Redis available in this environment)."""

    def __init__(self):
        self._store: dict[str, str] = {}

    def get(self, key):
        return self._store.get(key)

    def setex(self, key, ttl_seconds, value):
        self._store[key] = value


@pytest.fixture(autouse=True)
def _isolate_memory_cache_from_disk_tier(monkeypatch):
    """Force every _cached_get call to skip the disk tier, so these tests
    exercise the memory-tier behavior in isolation without real file I/O."""
    sefaria_library_module._cache.clear()
    monkeypatch.setattr(sefaria_library_module, "_disk_cache_get", lambda url: None)
    monkeypatch.setattr(sefaria_library_module, "_disk_cache_set", lambda url, data: None)
    yield
    sefaria_library_module._cache.clear()


class TestCachedGetMemoryTier:
    def test_first_call_fetches_from_network_and_caches(self, mock_outbound_http):
        url = f"{sefaria_library_module.SEFARIA_API}/some-test-endpoint"
        result = sefaria_library_module._cached_get(url)
        assert sefaria_library_module._cache.get(url) == result

    def test_second_call_within_ttl_skips_network(self, mock_outbound_http):
        url = f"{sefaria_library_module.SEFARIA_API}/some-test-endpoint-2"
        first = sefaria_library_module._cached_get(url)
        second = sefaria_library_module._cached_get(url)
        assert first == second

        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 1

    def test_custom_ttl_expiry_triggers_refetch(self, mock_outbound_http, monkeypatch):
        url = f"{sefaria_library_module.SEFARIA_API}/some-test-endpoint-3"
        sefaria_library_module._cached_get(url, ttl=100)

        # Simulate 101 seconds passing (past this call's ttl=100) by
        # advancing TTLCache's monotonic clock rather than poking internals.
        real_monotonic = cache_module.time.monotonic
        monkeypatch.setattr(cache_module.time, "monotonic",
                             lambda: real_monotonic() + 101)

        sefaria_library_module._cached_get(url, ttl=100)
        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 2

    def test_default_ttl_used_when_not_specified(self, mock_outbound_http):
        url = f"{sefaria_library_module.SEFARIA_API}/some-test-endpoint-4"
        sefaria_library_module._cached_get(url)
        # Entry should be fresh against the module default CACHE_TTL (1 hour).
        assert sefaria_library_module._cache.get(url) is not None
        sefaria_library_module._cached_get(url)
        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 1


class TestCachedGetRedisTier:
    """Simulated cold-instance coverage for _cache's redis_prefix tier --
    this is the fix for /api/text/<ref> and /api/library/index's raw
    Sefaria fetch (plan.md cross-instance cache task). No live Redis is
    available in this environment; a shared fake models what a real
    Upstash/Redis deployment provides: state visible to every concurrent
    instance, independent of any single instance's own process memory."""

    def test_cold_instance_gets_value_from_redis_without_a_network_call(self, monkeypatch, mock_outbound_http):
        monkeypatch.setattr(cache_module, "_shared_redis_client", _SharedFakeRedis())
        url = f"{sefaria_library_module.SEFARIA_API}/redis-tier-endpoint"

        warm = sefaria_library_module._cached_get(url)

        # Simulate a brand new instance: only the in-process memory tier is
        # wiped, exactly like a fresh Fluid Compute instance's empty
        # process -- the fake shared Redis is untouched.
        sefaria_library_module._cache.clear()
        cold = sefaria_library_module._cached_get(url)

        assert cold == warm
        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 1

    def test_without_redis_configured_a_cold_instance_still_refetches(self, monkeypatch, mock_outbound_http):
        """Control case for the test above: with no shared Redis client
        (this suite's default / an unconfigured RATE_LIMIT_REDIS_URL in
        production), clearing memory must still force a real refetch --
        confirms the prior test is exercising the Redis tier specifically."""
        monkeypatch.setattr(cache_module, "_shared_redis_client", None)
        url = f"{sefaria_library_module.SEFARIA_API}/redis-tier-endpoint-control"

        sefaria_library_module._cached_get(url)
        sefaria_library_module._cache.clear()
        sefaria_library_module._cached_get(url)

        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 2
