"""
Coverage-expansion tests for backend/search.py, complementing
tests/test_search_cache.py (which covers the shared sync/async cache
semantics for Wikipedia/Halachipedia/daily-learning). This file covers the
HebrewBooks connectors (sync + async, not covered elsewhere), the full
async_search_halachipedia flow, _clean_html_text, and remaining error/edge
branches not already exercised.
"""

from __future__ import annotations

import re

import httpx
import pytest
import responses as responses_lib

import backend.search as search_module

HEBREWBOOKS_URL_RE = re.compile(r"https://www\.hebrewbooks\.org/search\.aspx.*")
HALACHIPEDIA_URL_RE = re.compile(r"https://halachipedia\.com/api\.php.*")
WIKI_URL_RE = re.compile(r"https://en\.wikipedia\.org/api/rest_v1/page/summary/.*")

HEBREWBOOKS_MATCH_HTML = (
    '<a href="/pdfpager.aspx?req=12345&st=FT">Shulchan Arukh &amp; Commentary</a>'
)
HEBREWBOOKS_NO_MATCH_HTML = "<html><body>No results here</body></html>"
CLOUDFLARE_CHALLENGE_HTML = "<html><body>Just a moment... Cloudflare</body></html>"


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


# ─────────────────────────── _clean_html_text ──────────────────────────────

class TestCleanHtmlText:
    def test_strips_tags(self):
        assert search_module._clean_html_text("<b>bold</b> text") == "bold text"

    def test_unescapes_entities(self):
        assert search_module._clean_html_text("A &amp; B") == "A & B"

    def test_collapses_whitespace(self):
        assert search_module._clean_html_text("a   b\n\nc") == "a b c"

    def test_empty_input(self):
        assert search_module._clean_html_text(None) == ""
        assert search_module._clean_html_text("") == ""


# ─────────────────────────── search_hebrewbooks (sync) ─────────────────────

class TestSearchHebrewbooksSync:
    def test_happy_path_extracts_title_and_url(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HEBREWBOOKS_URL_RE,
            body=HEBREWBOOKS_MATCH_HTML, status=200,
        )
        result = search_module.search_hebrewbooks("shulchan arukh")
        assert result["title"] == "[HebrewBooks] Shulchan Arukh & Commentary"
        assert "pdfpager.aspx" in result["url"]

    def test_empty_query_returns_none(self):
        assert search_module.search_hebrewbooks("") is None
        assert search_module.search_hebrewbooks(None) is None

    def test_no_match_returns_none(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HEBREWBOOKS_URL_RE,
            body=HEBREWBOOKS_NO_MATCH_HTML, status=200,
        )
        assert search_module.search_hebrewbooks("nonexistent") is None

    def test_non_200_status_returns_none(self, mock_outbound_http):
        mock_outbound_http.add(responses_lib.GET, HEBREWBOOKS_URL_RE, status=500)
        assert search_module.search_hebrewbooks("query") is None

    def test_cloudflare_challenge_returns_none(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HEBREWBOOKS_URL_RE,
            body=CLOUDFLARE_CHALLENGE_HTML, status=200,
        )
        assert search_module.search_hebrewbooks("query") is None

    def test_result_is_cached(self, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, HEBREWBOOKS_URL_RE,
            body=HEBREWBOOKS_MATCH_HTML, status=200,
        )
        first = search_module.search_hebrewbooks("cached query")
        second = search_module.search_hebrewbooks("cached query")
        assert first == second
        matching_calls = [c for c in mock_outbound_http.calls if "hebrewbooks" in c.request.url]
        assert len(matching_calls) == 1

    def test_network_exception_returns_none(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("simulated failure")
        monkeypatch.setattr(search_module._HTTP, "get", _raise)
        assert search_module.search_hebrewbooks("query") is None

    def test_missing_title_falls_back_to_generic_title(self, mock_outbound_http):
        html = '<a href="/pdfpager.aspx?req=99"></a>'
        mock_outbound_http.add(responses_lib.GET, HEBREWBOOKS_URL_RE, body=html, status=200)
        result = search_module.search_hebrewbooks("my query")
        assert result["title"] == "[HebrewBooks] HebrewBooks search result for my query"


# ─────────────────────────── async_search_hebrewbooks ──────────────────────

class TestSearchHebrewbooksAsync:
    async def test_happy_path(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(200, text=HEBREWBOOKS_MATCH_HTML)
        )
        result = await search_module.async_search_hebrewbooks("shulchan arukh async")
        assert result["title"] == "[HebrewBooks] Shulchan Arukh & Commentary"

    async def test_empty_query_returns_none(self):
        assert await search_module.async_search_hebrewbooks("") is None

    async def test_no_match_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(200, text=HEBREWBOOKS_NO_MATCH_HTML)
        )
        assert await search_module.async_search_hebrewbooks("nonexistent async") is None

    async def test_non_200_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(return_value=httpx.Response(500))
        assert await search_module.async_search_hebrewbooks("query async") is None

    async def test_cloudflare_challenge_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(200, text=CLOUDFLARE_CHALLENGE_HTML)
        )
        assert await search_module.async_search_hebrewbooks("blocked query") is None

    async def test_request_exception_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(side_effect=httpx.ConnectError("down"))
        assert await search_module.async_search_hebrewbooks("errored query") is None

    async def test_result_cached_and_shared_with_sync(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(200, text=HEBREWBOOKS_MATCH_HTML)
        )
        result = await search_module.async_search_hebrewbooks("shared cache query")
        assert search_module._HEBREWBOOKS_CACHE.get("shared cache query") == result


# ─────────────────────────── async_search_halachipedia (full flow) ─────────

class TestSearchHalachipediaAsyncFull:
    async def test_happy_path_search_then_extract(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json={"query": {"search": [{"title": "Niddah"}]}}),
            httpx.Response(200, json={"query": {"pages": {"1": {"title": "Niddah", "extract": "Laws of family purity."}}}}),
        ])
        result = await search_module.async_search_halachipedia("niddah")
        assert result["title"] == "[Halachipedia] Niddah"
        assert "family purity" in result["summary"]

    async def test_empty_query_returns_none(self):
        assert await search_module.async_search_halachipedia("") is None

    async def test_no_search_results_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(
            return_value=httpx.Response(200, json={"query": {"search": []}})
        )
        assert await search_module.async_search_halachipedia("nonexistent topic xyz") is None

    async def test_search_non_200_treated_as_no_results(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(return_value=httpx.Response(500))
        assert await search_module.async_search_halachipedia("query") is None

    async def test_extract_non_200_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json={"query": {"search": [{"title": "Shabbat"}]}}),
            httpx.Response(500),
        ])
        assert await search_module.async_search_halachipedia("shabbat query") is None

    async def test_request_exception_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=httpx.ConnectError("down"))
        assert await search_module.async_search_halachipedia("errored") is None

    async def test_result_shared_with_sync_cache(self, mock_outbound_httpx):
        mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json={"query": {"search": [{"title": "Kashrut"}]}}),
            httpx.Response(200, json={"query": {"pages": {"1": {"title": "Kashrut", "extract": "Dietary laws."}}}}),
        ])
        result = await search_module.async_search_halachipedia("kashrut async")
        assert search_module._HALACHIPEDIA_CACHE.get("kashrut async") == result


# ─────────────────────────── search_wikipedia edge cases ───────────────────

class TestSearchWikipediaEdgeCases:
    def test_non_200_status_returns_none(self, mock_outbound_http):
        mock_outbound_http.add(responses_lib.GET, WIKI_URL_RE, status=404)
        assert search_module.search_wikipedia("Nonexistent Page") is None

    def test_network_exception_returns_none(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("boom")
        monkeypatch.setattr(search_module._HTTP, "get", _raise)
        assert search_module.search_wikipedia("Shabbat") is None

    async def test_async_empty_title_returns_none(self):
        assert await search_module.async_search_wikipedia("") is None

    async def test_async_non_200_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(WIKI_URL_RE).mock(return_value=httpx.Response(404))
        assert await search_module.async_search_wikipedia("Missing Page") is None

    async def test_async_request_exception_returns_none(self, mock_outbound_httpx):
        mock_outbound_httpx.get(WIKI_URL_RE).mock(side_effect=httpx.ConnectError("down"))
        assert await search_module.async_search_wikipedia("Error Page") is None


# ─────────────────────────── get_daily_learning edge cases ─────────────────

class TestDailyLearningEdgeCases:
    def test_upstream_exception_returns_default_shape(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("hebcal down")
        monkeypatch.setattr(search_module._HTTP, "get", _raise)
        result = search_module.get_daily_learning()
        assert result == {"parsha": None, "portions": []}

    def test_parses_parasha_and_rambam_titles(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": [
                {"title": "Parashat Vaera"},
                {"title": "Rambam - 1 Chapter"},
                {"title": "Some other event"},
            ]},
            status=200,
        )
        result = search_module.get_daily_learning()
        assert result["parsha"] == "Parashat Vaera"
        assert "Rambam - 1 Chapter" in result["portions"]


# ─────────────────────────── _get_async_client ─────────────────────────────

class TestGetAsyncClient:
    def test_returns_same_instance_on_repeated_calls(self):
        client1 = search_module._get_async_client()
        client2 = search_module._get_async_client()
        assert client1 is client2
