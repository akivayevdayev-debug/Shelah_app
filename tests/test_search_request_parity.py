"""
Request-shape parity between the sync and async search connectors
(docs/AI_SECURITY_REVIEW.md L3).

backend/search.py keeps two independently-maintained copies of each connector:
sync (requests; Flask /ask, backend/data_service.py, backend/utils/search_provider.py)
and async (httpx; ASGI /ask, backend/ai_tools.py). They could not be collapsed
onto one implementation (see the L3 note in the review), so this file pins the
property M1 fixed -- hostile characters in the query stay literal data and can
never become URL structure -- on BOTH copies and asserts they send the same
request, so a fix applied to only one of them fails here instead of silently
re-opening the bug in the other.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import responses as responses_lib

import backend.search as search_module

WIKI_URL_RE = re.compile(r"https://en\.wikipedia\.org/api/rest_v1/page/summary/.*")
HALACHIPEDIA_URL_RE = re.compile(r"https://halachipedia\.com/api\.php.*")
HEBREWBOOKS_URL_RE = re.compile(r"https://www\.hebrewbooks\.org/search\.aspx.*")

# ?, &, = and # would each change what the destination API sees if they were
# spliced into a URL unescaped; "/" would change the Wikipedia path.
HOSTILE = "Shabbat?foo=bar&x=1#frag/a b"

WIKI_BODY = {"title": "Shabbat", "extract": "The Jewish day of rest."}
HALACHIPEDIA_SEARCH_BODY = {"query": {"search": [{"title": "Shabbat & Yom Tov?a=b#c"}]}}
HALACHIPEDIA_EXTRACT_BODY = {"query": {"pages": {"1": {"title": "Shabbat", "extract": "Laws of Shabbat."}}}}
HEBREWBOOKS_HTML = '<a href="/pdfpager.aspx?req=1&st=FT">Shulchan Arukh</a>'


def _clear_caches():
    search_module._WIKI_CACHE.clear()
    search_module._HALACHIPEDIA_CACHE.clear()
    search_module._HEBREWBOOKS_CACHE.clear()


def _params(url: str) -> dict:
    return parse_qs(urlsplit(url).query, keep_blank_values=True)


class TestWikipediaTitleStaysOneSafePathSegment:
    EXPECTED_PATH = "/api/rest_v1/page/summary/Shabbat%3Ffoo%3Dbar%26x%3D1%23frag%2Fa_b"

    def _assert_safe(self, url: str):
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc) == ("https", "en.wikipedia.org")
        assert parts.path == self.EXPECTED_PATH
        assert parts.query == "" and parts.fragment == ""
        assert unquote(parts.path.rsplit("/", 1)[1]) == "Shabbat?foo=bar&x=1#frag/a_b"

    def test_sync(self, mock_outbound_http):
        _clear_caches()
        mock_outbound_http.add(responses_lib.GET, WIKI_URL_RE, json=WIKI_BODY)

        assert search_module.search_wikipedia(HOSTILE) is not None

        self._assert_safe(mock_outbound_http.calls[-1].request.url)

    async def test_async(self, mock_outbound_httpx):
        _clear_caches()
        route = mock_outbound_httpx.get(WIKI_URL_RE).mock(return_value=httpx.Response(200, json=WIKI_BODY))

        assert await search_module.async_search_wikipedia(HOSTILE) is not None

        self._assert_safe(str(route.calls.last.request.url))

    async def test_sync_and_async_send_the_identical_url(self, mock_outbound_http, mock_outbound_httpx):
        _clear_caches()
        mock_outbound_http.add(responses_lib.GET, WIKI_URL_RE, json=WIKI_BODY)
        route = mock_outbound_httpx.get(WIKI_URL_RE).mock(return_value=httpx.Response(200, json=WIKI_BODY))

        search_module.search_wikipedia(HOSTILE)
        _clear_caches()
        await search_module.async_search_wikipedia(HOSTILE)

        assert mock_outbound_http.calls[-1].request.url == str(route.calls.last.request.url)


class TestHalachipediaQueryAndTitleStayLiteralParams:
    def _assert_safe(self, search_url: str, extract_url: str):
        for url in (search_url, extract_url):
            parts = urlsplit(url)
            assert (parts.scheme, parts.netloc, parts.path) == ("https", "halachipedia.com", "/api.php")
            assert parts.fragment == ""
        search, extract = _params(search_url), _params(extract_url)
        # The hostile text is exactly one parameter's value; nothing was smuggled in.
        assert search["srsearch"] == [HOSTILE]
        assert set(search) == {"action", "list", "srsearch", "utf8", "format"}
        # A remote-supplied title is equally just a value.
        assert extract["titles"] == ["Shabbat & Yom Tov?a=b#c"]
        assert set(extract) == {"action", "prop", "exsentences", "exintro", "explaintext", "titles", "format"}

    def test_sync(self, mock_outbound_http):
        _clear_caches()
        mock_outbound_http.add(responses_lib.GET, HALACHIPEDIA_URL_RE, json=HALACHIPEDIA_SEARCH_BODY)
        mock_outbound_http.add(responses_lib.GET, HALACHIPEDIA_URL_RE, json=HALACHIPEDIA_EXTRACT_BODY)

        assert search_module.search_halachipedia(HOSTILE) is not None

        search_call, extract_call = mock_outbound_http.calls[-2:]
        self._assert_safe(search_call.request.url, extract_call.request.url)

    async def test_async(self, mock_outbound_httpx):
        _clear_caches()
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json=HALACHIPEDIA_SEARCH_BODY),
            httpx.Response(200, json=HALACHIPEDIA_EXTRACT_BODY),
        ])

        assert await search_module.async_search_halachipedia(HOSTILE) is not None

        search_call, extract_call = route.calls[-2], route.calls[-1]
        self._assert_safe(str(search_call.request.url), str(extract_call.request.url))

    async def test_sync_and_async_send_the_same_parameters(self, mock_outbound_http, mock_outbound_httpx):
        _clear_caches()
        mock_outbound_http.add(responses_lib.GET, HALACHIPEDIA_URL_RE, json=HALACHIPEDIA_SEARCH_BODY)
        mock_outbound_http.add(responses_lib.GET, HALACHIPEDIA_URL_RE, json=HALACHIPEDIA_EXTRACT_BODY)
        route = mock_outbound_httpx.get(HALACHIPEDIA_URL_RE).mock(side_effect=[
            httpx.Response(200, json=HALACHIPEDIA_SEARCH_BODY),
            httpx.Response(200, json=HALACHIPEDIA_EXTRACT_BODY),
        ])

        search_module.search_halachipedia(HOSTILE)
        sync_calls = [_params(c.request.url) for c in mock_outbound_http.calls[-2:]]
        _clear_caches()
        await search_module.async_search_halachipedia(HOSTILE)
        async_calls = [_params(str(c.request.url)) for c in route.calls[-2:]]

        assert sync_calls == async_calls


class TestHebrewBooksQueryStaysOneLiteralParam:
    def _assert_safe(self, url: str):
        parts = urlsplit(url)
        assert (parts.scheme, parts.netloc, parts.path) == ("https", "www.hebrewbooks.org", "/search.aspx")
        assert parts.fragment == ""
        params = _params(url)
        assert params["q"] == [HOSTILE]
        assert set(params) == {"st", "q"}

    def test_sync(self, mock_outbound_http):
        _clear_caches()
        mock_outbound_http.add(responses_lib.GET, HEBREWBOOKS_URL_RE, body=HEBREWBOOKS_HTML)

        assert search_module.search_hebrewbooks(HOSTILE) is not None

        self._assert_safe(mock_outbound_http.calls[-1].request.url)

    async def test_async(self, mock_outbound_httpx):
        _clear_caches()
        route = mock_outbound_httpx.get(HEBREWBOOKS_URL_RE).mock(
            return_value=httpx.Response(200, text=HEBREWBOOKS_HTML))

        assert await search_module.async_search_hebrewbooks(HOSTILE) is not None

        self._assert_safe(str(route.calls.last.request.url))
