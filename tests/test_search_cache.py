"""
Characterization tests for the 4 hand-rolled caches in backend/search.py
(_WIKI_CACHE, _HALACHIPEDIA_CACHE, _HEBREWBOOKS_CACHE, _DAILY_CACHE).

Written BEFORE these caches are swapped to the shared backend.cache.TTLCache
utility (plan.md §3.6/§4 cache consolidation). Key behavior to preserve: the
sync and async variants of each search function (e.g. search_wikipedia /
async_search_wikipedia) deliberately share the SAME cache dict — a sync call
populates an entry the async call can then read, and vice versa.

Note: en.wikipedia.org and halachipedia.com are NOT covered by conftest.py's
autouse mocks (only Sefaria/Hebcal/translation/Anthropic/Gemini/Supabase are),
so each test here registers its own explicit mock rather than relying on the
real network.
"""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
import responses as responses_lib

import backend.search as search_module

WIKI_URL_RE = re.compile(r"https://en\.wikipedia\.org/api/rest_v1/page/summary/.*")
HALACHIPEDIA_URL_RE = re.compile(r"https://halachipedia\.com/api\.php.*")


@pytest.fixture(autouse=True)
def _reset_search_caches():
    search_module._WIKI_CACHE.clear()
    search_module._HALACHIPEDIA_CACHE.clear()
    search_module._HEBREWBOOKS_CACHE.clear()
    search_module._DAILY_CACHE.clear()
    yield
    search_module._WIKI_CACHE.clear()
    search_module._HALACHIPEDIA_CACHE.clear()
    search_module._HEBREWBOOKS_CACHE.clear()
    search_module._DAILY_CACHE.clear()


# Generic TTL/LRU primitive behavior (expiry, eviction) is covered exhaustively
# by tests/test_cache.py against backend.cache.TTLCache directly — no need to
# duplicate that here now that _WIKI_CACHE/_HALACHIPEDIA_CACHE/_HEBREWBOOKS_CACHE
# are TTLCache instances rather than hand-rolled dicts with bespoke helpers.


class TestWikipediaCacheSharedAcrossSyncAndAsync:
    def test_sync_call_caches_then_async_call_reuses_it(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, WIKI_URL_RE,
            json={"title": "Shabbat", "extract": "Shabbat is the Jewish day of rest."},
            status=200,
        )
        sync_result = search_module.search_wikipedia("Shabbat")
        assert sync_result["title"] == "Shabbat"

        # No httpx mock registered for the async path — if the async call
        # actually hit the network instead of the shared cache, this would
        # either fail or hang, not silently pass.
        async_result = asyncio.run(search_module.async_search_wikipedia("Shabbat"))
        assert async_result == sync_result

    async def test_async_call_caches_then_sync_call_reuses_it(self, mock_outbound_httpx):
        # mock_outbound_httpx (autouse) is already an active respx router for
        # this test — register the Wikipedia route on it directly rather than
        # nesting a second respx.mock() context, which doesn't compose with
        # the outer one's assert_all_called bookkeeping.
        mock_outbound_httpx.get(WIKI_URL_RE).mock(
            return_value=httpx.Response(
                200, json={"title": "Kiddush", "extract": "Blessing over wine."}
            )
        )
        async_result = await search_module.async_search_wikipedia("Kiddush")
        assert async_result["title"] == "Kiddush"

        # Sync call must hit the cache, not requests (no responses mock for
        # this URL is registered in this test).
        sync_result = search_module.search_wikipedia("Kiddush")
        assert sync_result == async_result

    def test_empty_title_is_not_cached(self, mock_outbound_http):
        result = search_module.search_wikipedia("")
        assert result is None
        assert len(search_module._WIKI_CACHE) == 0


class TestHalachipediaCache:
    def test_search_then_extract_flow_caches_result(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HALACHIPEDIA_URL_RE,
            json={"query": {"search": [{"title": "Shabbat Candles"}]}},
            status=200,
        )
        mock_outbound_http.add(
            responses_lib.GET, HALACHIPEDIA_URL_RE,
            json={"query": {"pages": {"1": {"title": "Shabbat Candles", "extract": "Light candles before sunset."}}}},
            status=200,
        )
        result = search_module.search_halachipedia("candle lighting")
        assert result["title"] == "[Halachipedia] Shabbat Candles"
        assert search_module._HALACHIPEDIA_CACHE.get("candle lighting") == result

    def test_no_search_results_returns_none_and_does_not_cache(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HALACHIPEDIA_URL_RE,
            json={"query": {"search": []}},
            status=200,
        )
        result = search_module.search_halachipedia("totally nonexistent topic xyz")
        assert result is None
        assert search_module._HALACHIPEDIA_CACHE.get("totally nonexistent topic xyz") is None

    def test_upstream_error_returns_none_gracefully(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated failure")

        monkeypatch.setattr(search_module._HTTP, "get", _raise)
        result = search_module.search_halachipedia("anything")
        assert result is None


class TestDailyLearningCache:
    def test_repeat_call_within_ttl_skips_network(self, mock_outbound_http):
        first = search_module.get_daily_learning()
        second = search_module.get_daily_learning()
        assert first == second

        hebcal_calls = [c for c in mock_outbound_http.calls if "hebcal.com" in c.request.url]
        assert len(hebcal_calls) == 1

    def test_result_shape_has_parsha_and_portions(self, mock_outbound_http):
        result = search_module.get_daily_learning()
        assert "parsha" in result
        assert "portions" in result
        assert isinstance(result["portions"], list)

    def test_network_failure_returns_empty_shape_not_raise(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated failure")

        monkeypatch.setattr(search_module._HTTP, "get", _raise)
        result = search_module.get_daily_learning()
        assert result == {"parsha": None, "portions": []}
