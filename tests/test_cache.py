"""
Tests for backend/cache.py's TTLCache utility.

Written before TTLCache gets adopted into sefaria_library.py, sefaria.py,
search.py, and zmanim_engine.py (plan.md §3.1.4/§4 cache consolidation) — this
is the characterization-test safety net for that swap, plus standalone
coverage since the utility itself was previously orphaned (0% covered).
"""

from __future__ import annotations

import pytest

from backend.cache import TTLCache


class _FakeClock:
    """Deterministic monotonic clock for TTL tests — avoids real sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = _FakeClock()
    import backend.cache as cache_module
    monkeypatch.setattr(cache_module.time, "monotonic", clock.monotonic)
    return clock


class TestBasicGetSet:
    def test_get_on_empty_cache_returns_none(self):
        cache = TTLCache()
        assert cache.get("missing") is None

    def test_set_then_get_round_trips(self):
        cache = TTLCache()
        cache.set("k1", "v1")
        assert cache.get("k1") == "v1"

    def test_set_overwrites_existing_value(self):
        cache = TTLCache()
        cache.set("k1", "v1")
        cache.set("k1", "v2")
        assert cache.get("k1") == "v2"

    def test_len_reflects_entry_count(self):
        cache = TTLCache()
        assert len(cache) == 0
        cache.set("a", 1)
        cache.set("b", 2)
        assert len(cache) == 2

    def test_set_overwrite_does_not_increase_len(self):
        cache = TTLCache()
        cache.set("a", 1)
        cache.set("a", 2)
        assert len(cache) == 1

    def test_values_can_be_any_type(self):
        cache = TTLCache()
        cache.set("dict", {"a": 1})
        cache.set("list", [1, 2, 3])
        cache.set("none_value", None)
        assert cache.get("dict") == {"a": 1}
        assert cache.get("list") == [1, 2, 3]
        # Storing None as a value is indistinguishable from a miss by design —
        # both return None from get(). Documented behavior, not tested deeper.


class TestDeleteAndClear:
    def test_delete_removes_key(self):
        cache = TTLCache()
        cache.set("k1", "v1")
        cache.delete("k1")
        assert cache.get("k1") is None
        assert len(cache) == 0

    def test_delete_missing_key_is_a_noop(self):
        cache = TTLCache()
        cache.delete("never-existed")  # must not raise

    def test_clear_empties_everything(self):
        cache = TTLCache()
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert len(cache) == 0
        assert cache.get("a") is None


class TestTTLExpiration:
    def test_value_available_before_ttl_expires(self, fake_clock):
        cache = TTLCache(ttl=10.0)
        cache.set("k1", "v1")
        fake_clock.advance(9.0)
        assert cache.get("k1") == "v1"

    def test_value_expires_after_ttl(self, fake_clock):
        cache = TTLCache(ttl=10.0)
        cache.set("k1", "v1")
        fake_clock.advance(10.1)
        assert cache.get("k1") is None

    def test_expired_entry_is_evicted_on_access(self, fake_clock):
        cache = TTLCache(ttl=5.0)
        cache.set("k1", "v1")
        fake_clock.advance(5.1)
        cache.get("k1")  # triggers lazy eviction
        assert len(cache) == 0

    def test_per_call_ttl_override(self, fake_clock):
        cache = TTLCache(ttl=1000.0)  # long default
        cache.set("short-lived", "v1", ttl=5.0)
        fake_clock.advance(5.1)
        assert cache.get("short-lived") is None

    def test_set_on_existing_key_refreshes_ttl(self, fake_clock):
        cache = TTLCache(ttl=10.0)
        cache.set("k1", "v1")
        fake_clock.advance(8.0)
        cache.set("k1", "v2")  # refresh — new 10s window from t=8
        fake_clock.advance(8.0)  # now t=16, 8s since refresh — still valid
        assert cache.get("k1") == "v2"


class TestLRUEviction:
    def test_evicts_least_recently_used_when_full(self):
        cache = TTLCache(maxsize=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.set("c", 3)  # evicts "a" (least recently used)
        assert cache.get("a") is None
        assert cache.get("b") == 2
        assert cache.get("c") == 3

    def test_get_marks_entry_as_recently_used(self):
        cache = TTLCache(maxsize=2)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.get("a")  # touch "a" — now "b" is least recently used
        cache.set("c", 3)  # should evict "b", not "a"
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3

    def test_maxsize_of_zero_or_negative_is_clamped_to_one(self):
        cache = TTLCache(maxsize=0)
        cache.set("a", 1)
        cache.set("b", 2)
        assert len(cache) == 1
        assert cache.get("b") == 2


class TestGetOrFetch:
    def test_cache_miss_calls_fetch_fn_and_caches_result(self):
        cache = TTLCache()
        calls = []

        def fetch():
            calls.append(1)
            return "fetched-value"

        result = cache.get_or_fetch("k1", fetch)
        assert result == "fetched-value"
        assert len(calls) == 1
        assert cache.get("k1") == "fetched-value"

    def test_cache_hit_does_not_call_fetch_fn(self):
        cache = TTLCache()
        cache.set("k1", "cached-value")
        calls = []

        def fetch():
            calls.append(1)
            return "should-not-be-used"

        result = cache.get_or_fetch("k1", fetch)
        assert result == "cached-value"
        assert len(calls) == 0

    def test_fetch_fn_returning_none_is_not_cached(self):
        cache = TTLCache()
        calls = []

        def fetch():
            calls.append(1)
            return None

        assert cache.get_or_fetch("k1", fetch) is None
        assert cache.get_or_fetch("k1", fetch) is None
        # Not cached — fetch_fn is called again on every miss, by design
        # (avoids negative-caching a transient failure).
        assert len(calls) == 2

    def test_get_or_fetch_respects_ttl_override(self, fake_clock):
        cache = TTLCache(ttl=1000.0)
        cache.get_or_fetch("k1", lambda: "v1", ttl=5.0)
        fake_clock.advance(5.1)
        assert cache.get("k1") is None
