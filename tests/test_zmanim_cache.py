"""
Characterization tests for the hand-rolled Hebcal caches in
backend/zmanim_engine.py (_HEBCAL_DAY_CACHE, _HEBCAL_MONTH_CACHE).

Written BEFORE these caches are swapped to the shared backend.cache.TTLCache
utility (plan.md §3.7/§4 cache consolidation) — asserts the externally
observable behavior (repeat calls with the same args don't re-hit the
network; different args/expiry do) that must survive the swap unchanged.
"""

from __future__ import annotations

from datetime import date

import pytest

import backend.zmanim_engine as zmanim_engine


@pytest.fixture(autouse=True)
def _clear_hebcal_caches():
    """Hand-rolled caches are module globals — reset between tests to avoid
    cross-test pollution (this requirement disappears once they're TTLCache
    instances with their own clear(), but for now they're plain dicts)."""
    zmanim_engine._HEBCAL_DAY_CACHE.clear()
    zmanim_engine._HEBCAL_MONTH_CACHE.clear()
    yield
    zmanim_engine._HEBCAL_DAY_CACHE.clear()
    zmanim_engine._HEBCAL_MONTH_CACHE.clear()


class TestHebcalDayCache:
    def test_repeat_call_with_same_args_hits_network_once(self, mock_outbound_http):
        lat, lon, tz, day = 40.7, -74.0, "America/New_York", date(2026, 6, 20)

        first = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)
        second = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)

        assert first == second
        hebcal_calls = [c for c in mock_outbound_http.calls if "hebcal.com" in c.request.url]
        assert len(hebcal_calls) == 1

    def test_different_dates_are_cached_independently(self, mock_outbound_http):
        lat, lon, tz = 40.7, -74.0, "America/New_York"

        zmanim_engine._get_hebcal_day_times(lat, lon, tz, date(2026, 6, 20))
        zmanim_engine._get_hebcal_day_times(lat, lon, tz, date(2026, 6, 21))

        hebcal_calls = [c for c in mock_outbound_http.calls if "hebcal.com" in c.request.url]
        assert len(hebcal_calls) == 2

    def test_returned_dict_is_a_copy_not_a_shared_reference(self, mock_outbound_http):
        """Callers mutating the returned dict must not corrupt the cached entry.

        Found via this test: the cache-miss path used to `return result` (the
        same dict object just stored in the cache), while the cache-hit path
        already did `return dict(cached)` (a safe copy) — an inconsistency
        where only the *first* call leaked a mutable reference into caller
        code. Fixed in backend/zmanim_engine.py to always return a copy. No
        caller currently mutates this dict, so the fix changes no observed
        behavior — it just closes the latent footgun.
        """
        lat, lon, tz, day = 40.7, -74.0, "America/New_York", date(2026, 6, 22)

        first = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)
        first["candles"] = "MUTATED"
        second = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)

        assert second.get("candles") != "MUTATED"

    def test_first_call_cache_miss_also_returns_a_copy(self, mock_outbound_http):
        """Specifically covers the cache-MISS return path (the one that had
        the bug) — mutate the dict from the very first call, before any cache
        hit has occurred, and confirm the cached entry is unaffected."""
        lat, lon, tz, day = 40.7, -74.0, "America/New_York", date(2026, 6, 24)

        first = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)
        first["candles"] = "MUTATED-ON-FIRST-CALL"

        cache_key = (lat, lon, tz, day.isoformat())
        cached_entry = zmanim_engine._HEBCAL_DAY_CACHE.get(cache_key)
        assert cached_entry.get("candles") != "MUTATED-ON-FIRST-CALL"

    def test_result_shape_has_candles_and_havdalah_keys(self, mock_outbound_http):
        result = zmanim_engine._get_hebcal_day_times(
            31.77, 35.21, "Asia/Jerusalem", date(2026, 6, 20)
        )
        assert "candles" in result
        assert "havdalah" in result

    def test_network_failure_still_returns_a_dict_and_caches_it(self, mock_outbound_http, monkeypatch):
        """On upstream failure, the function must degrade gracefully (no raise)
        and still cache the fallback result so repeat calls don't keep retrying
        a known-bad request within the TTL window."""
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated network failure")

        monkeypatch.setattr(zmanim_engine._HTTP, "get", _raise)

        lat, lon, tz, day = 40.7, -74.0, "America/New_York", date(2026, 6, 23)
        result = zmanim_engine._get_hebcal_day_times(lat, lon, tz, day)
        assert result == {"candles": None, "havdalah": None}
        # Cached despite the failure — confirm by checking the cache directly.
        assert len(zmanim_engine._HEBCAL_DAY_CACHE) == 1


class TestHebcalMonthCache:
    def test_repeat_call_hits_network_once(self, mock_outbound_http):
        lat, lon, tz = 40.7, -74.0, "America/New_York"

        first = zmanim_engine.get_monthly_events(lat, lon, tz)
        second = zmanim_engine.get_monthly_events(lat, lon, tz)

        assert first == second
        hebcal_calls = [c for c in mock_outbound_http.calls if "hebcal.com" in c.request.url]
        assert len(hebcal_calls) == 1

    def test_returns_a_list(self, mock_outbound_http):
        result = zmanim_engine.get_monthly_events(40.7, -74.0, "America/New_York")
        assert isinstance(result, list)

    def test_cached_list_is_a_copy(self, mock_outbound_http):
        """Mutating the returned list must not corrupt the cached entry."""
        lat, lon, tz = 31.77, 35.21, "Asia/Jerusalem"

        first = zmanim_engine.get_monthly_events(lat, lon, tz)
        original_len = len(first)
        first.append({"title": "INJECTED"})

        second = zmanim_engine.get_monthly_events(lat, lon, tz)
        assert len(second) == original_len
