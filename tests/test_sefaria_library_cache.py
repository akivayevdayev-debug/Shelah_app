"""
Characterization tests for backend/sefaria_library.py's memory-tier cache
(_cache / _cached_get).

Scope note: unlike zmanim_engine.py / sefaria.py / search.py (all simple,
single-tier, fixed-TTL dict caches now consolidated into backend.cache.TTLCache
— see test_zmanim_cache.py, test_sefaria_cache.py, test_search_cache.py), this
file's caching is NOT swapped to TTLCache in this pass. Direct inspection
during test-writing revealed _cached_get is a three-tier cache (memory → disk
→ network) where the disk tier persists across process restarts, AND callers
pass a per-call `ttl` override that's re-evaluated against the stored
timestamp on every read (TTLCache fixes the expiry at write time instead —
a real behavioral difference, not just a storage-implementation detail).
sefaria_library.py also has several OTHER cache structures with non-TTL
invalidation (_title_catalog_cache keys off a report file's mtime, not just
time) that don't map onto a pure TTL abstraction at all. Forcing all of this
into TTLCache risked changing behavior on the most-trafficked code path in
the app, which the zero-breakage requirement for this pass doesn't allow —
deferred to a dedicated, more careful pass. These tests document and protect
the CURRENT memory-tier behavior regardless.
"""

from __future__ import annotations

import pytest

import backend.sefaria_library as sefaria_library_module


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
        assert url in sefaria_library_module._cache
        assert sefaria_library_module._cache[url]["data"] == result

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

        # Simulate 101 seconds passing — entry should be considered stale
        # against this specific call's ttl=100, even though the module
        # default CACHE_TTL is much longer.
        original_ts = sefaria_library_module._cache[url]["ts"]
        sefaria_library_module._cache[url]["ts"] = original_ts - 101

        sefaria_library_module._cached_get(url, ttl=100)
        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 2

    def test_default_ttl_used_when_not_specified(self, mock_outbound_http):
        url = f"{sefaria_library_module.SEFARIA_API}/some-test-endpoint-4"
        sefaria_library_module._cached_get(url)
        # Entry should be fresh against the module default CACHE_TTL (1 hour).
        assert (
            sefaria_library_module._cache[url]["ts"]
            > 0
        )
        cached_again = sefaria_library_module._cached_get(url)
        matching_calls = [c for c in mock_outbound_http.calls if url in c.request.url]
        assert len(matching_calls) == 1
