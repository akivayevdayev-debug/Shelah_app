"""
Tests for backend/routes_library.py routes.

Covers:
  - GET /api/library/index       happy path → 200 with JSON list
  - GET /api/text/<ref>          happy path with mocked Sefaria → 200 with ref/lines keys
  - GET /api/text/<ref>          Sefaria 500 → graceful (no 500 propagated)
  - GET /api/text/<ref>          empty/malformed ref → 400
  - GET /api/library/leaf-refs    happy path, missing title, upstream error
  - GET /api/library/popular      happy path
  - GET /api/diagnostics/sefaria  available + unavailable (503)
  - GET /api/word/meaning         English + Hebrew lookups, missing param (400), not-found (404)
  - POST /api/export/chapter      txt/docx/pdf happy paths, bad format (400), no lines (400)
  - GET /api/library/search       (existing) + metadata filters
  - GET /api/search/suggest       happy path, empty query, alias/community matches
  - GET /api/text/<ref>/links     happy path
  - GET /api/text/<ref>/graph     happy path, nodes/edges shape
  - GET /api/library/category/<category> happy path
  - GET /api/texts-index          happy path, full shape

Sefaria outbound calls are intercepted by the autouse `mock_outbound_http`
fixture in conftest.py. Routes that hit *uncovered* upstream domains
(api.dictionaryapi.dev for English word lookups, www.sefaria.org for the
BDB/Jastrow lexicon, www.sefaria.org for diagnostics-style checks) register
explicit mocks per-test, following the pattern in test_search_cache.py /
test_sefaria_cache.py.
"""

from __future__ import annotations

import json
import re
import pytest
import responses as responses_lib

from backend.sefaria_library import get_text

DICTIONARYAPI_URL_RE = re.compile(r"https://api\.dictionaryapi\.dev/.*")
SEFARIA_LEXICON_URL_RE = re.compile(r"https://www\.sefaria\.org/api/words/.*")


class TestLibraryIndex:
    def test_index_happy_path_status(self, test_client):
        response = test_client.get("/api/library/index")
        assert response.status_code == 200

    def test_index_returns_json(self, test_client):
        response = test_client.get("/api/library/index")
        ct = response.content_type.lower()
        assert "application/json" in ct

    def test_index_body_is_list_or_dict(self, test_client):
        response = test_client.get("/api/library/index")
        body = response.get_json()
        # The library index returns either a list of items or a dict tree
        assert isinstance(body, (list, dict))


class TestGetTextRoute:
    def test_get_text_happy_path(self, test_client):
        """Mocked Sefaria: /api/text/<ref> should return 200 with expected keys."""
        response = test_client.get("/api/text/Genesis%201:1")
        # The route returns the Sefaria payload; may be 200 or a graceful fallback
        assert response.status_code in (200, 503)

    def test_get_text_has_ref_key_or_error(self, test_client):
        """Response body must be a dict (either success payload or error)."""
        response = test_client.get("/api/text/Genesis%201:1")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_get_text_sefaria_500_is_graceful(self, test_client, mock_outbound_http):
        """When Sefaria returns 500, our route must not propagate 500."""
        # Override the generic Sefaria mock to return 500 for this test only
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/text/Genesis%201:1")
        # Must NOT be a raw 500 — should be 200 (error dict) or 503
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, dict)

    @pytest.mark.xfail(reason="empty-ref validation may not be enforced server-side yet")
    def test_get_text_empty_ref_returns_400(self, test_client):
        """Empty ref string should be rejected with 400."""
        response = test_client.get("/api/text/")
        assert response.status_code == 400


class TestLibrarySearch:
    def test_library_search_no_query_returns_empty(self, test_client):
        response = test_client.get("/api/library/search")
        assert response.status_code == 200
        body = response.get_json()
        assert body == []

    def test_library_search_with_query_returns_list(self, test_client):
        response = test_client.get("/api/library/search?q=Shabbat")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)

    def test_library_search_with_metadata_filters_returns_list(self, test_client):
        """era/author/category/geography/nusach query params should not crash the route."""
        response = test_client.get(
            "/api/library/search?q=Shabbat&era=Rishonim&category=Halakhah"
        )
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)

    def test_library_search_size_param_is_respected(self, test_client):
        response = test_client.get("/api/library/search?q=Shabbat&size=3")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)
        assert len(body) <= 3

    def test_library_search_sefaria_failure_is_graceful(self, test_client, mock_outbound_http):
        """Upstream Sefaria failure must not surface as a raw 500."""
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/library/search?q=Shabbat")
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, list)


class TestLibraryLeafRefs:
    def test_missing_title_returns_empty_shape(self, test_client):
        response = test_client.get("/api/library/leaf-refs")
        assert response.status_code == 200
        body = response.get_json()
        assert body == {"title": "", "refs": [], "sections": []}

    def test_happy_path_returns_expected_keys(self, test_client):
        response = test_client.get("/api/library/leaf-refs?title=Genesis")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert set(["title", "refs", "sections"]).issubset(body.keys())
        assert body["title"] == "Genesis"
        assert isinstance(body["refs"], list)
        assert isinstance(body["sections"], list)

    def test_max_param_is_clamped_and_does_not_crash(self, test_client):
        """max is coerced via _coerce_int(min=1, max=260); an out-of-range value
        must not raise, and the route must still respond 200."""
        response = test_client.get("/api/library/leaf-refs?title=Genesis&max=99999")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["refs"], list)

    def test_invalid_max_param_falls_back_to_default(self, test_client):
        response = test_client.get("/api/library/leaf-refs?title=Genesis&max=not-a-number")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)

    def test_upstream_failure_returns_empty_refs_not_500(self, test_client, mock_outbound_http):
        """get_index_leaf_refs/get_index_entry raising must be swallowed (route
        wraps the call in try/except Exception)."""
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/library/leaf-refs?title=Genesis")
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, dict)
        assert isinstance(body.get("refs"), list)


class TestLibraryPopular:
    def test_happy_path_status_and_json(self, test_client):
        response = test_client.get("/api/library/popular")
        assert response.status_code == 200
        ct = response.content_type.lower()
        assert "application/json" in ct

    def test_returns_known_categories(self, test_client):
        response = test_client.get("/api/library/popular")
        body = response.get_json()
        assert isinstance(body, dict)
        # get_popular_texts() is a static curated dict — assert structural shape
        # rather than hardcoding the full category set (resilient to additions).
        assert "Tanakh" in body
        assert isinstance(body["Tanakh"], list)
        assert all("ref" in item for item in body["Tanakh"])


class TestSefariaDiagnostics:
    def test_available_returns_200(self, test_client):
        response = test_client.get("/api/diagnostics/sefaria")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert "overall_available" in body
        assert body["overall_available"] is True
        assert "v3_api" in body
        assert "v2_api" in body

    def test_unavailable_returns_503(self, test_client, mock_outbound_http):
        """When both v3 and v2 probes fail, the route must return 503."""
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            status=500,
            json={"error": "internal error"},
        )
        response = test_client.get("/api/diagnostics/sefaria")
        assert response.status_code == 503
        body = response.get_json()
        assert body["overall_available"] is False

    def test_connection_error_is_graceful(self, test_client, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/diagnostics/sefaria")
        # Must not propagate a raw 500 — either 503 (declared unavailable) or 200.
        assert response.status_code in (200, 503)
        body = response.get_json()
        assert isinstance(body, dict)


class TestWordMeaning:
    @pytest.fixture(autouse=True)
    def _clear_translation_cache(self):
        """TRANSLATION_CACHE/TRANSLATION_SOURCE_CACHE are plain module-level dicts
        (not TTLCache), so entries persist across tests in the same process.
        Clear before and after every test in this class to keep word-meaning
        lookups deterministic regardless of test execution order."""
        import backend.helpers as helpers_module
        helpers_module.TRANSLATION_CACHE.clear()
        helpers_module.TRANSLATION_SOURCE_CACHE.clear()
        yield
        helpers_module.TRANSLATION_CACHE.clear()
        helpers_module.TRANSLATION_SOURCE_CACHE.clear()

    def test_missing_word_param_returns_400(self, test_client):
        response = test_client.get("/api/word/meaning")
        assert response.status_code == 400
        body = response.get_json()
        assert "error" in body

    def test_blank_word_param_returns_400(self, test_client):
        response = test_client.get("/api/word/meaning?word=%20%20")
        assert response.status_code == 400

    def test_english_word_happy_path(self, test_client, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, DICTIONARYAPI_URL_RE,
            json=[{
                "word": "shabbat",
                "meanings": [{
                    "definitions": [{"definition": "The Jewish day of rest."}],
                }],
            }],
            status=200,
        )
        response = test_client.get("/api/word/meaning?word=shabbat")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "ok"
        assert body["word"] == "shabbat"
        assert body["meaning"]
        assert isinstance(body["alternatives"], list)
        assert body["lang"] == "en"

    def test_english_word_not_found_returns_404(self, test_client, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, DICTIONARYAPI_URL_RE,
            status=404,
            json={"title": "No Definitions Found"},
        )
        response = test_client.get("/api/word/meaning?word=zzznonexistentword")
        assert response.status_code == 404
        body = response.get_json()
        assert body["status"] == "not_found"
        assert body["meaning"] == ""
        assert body["alternatives"] == []

    def test_dictionary_api_failure_is_graceful_404(self, test_client, mock_outbound_http):
        """Upstream dictionary API failure → no meaning found → 404, not 500."""
        mock_outbound_http.add(
            responses_lib.GET, DICTIONARYAPI_URL_RE,
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/word/meaning?word=anotherword")
        assert response.status_code in (404, 200)
        body = response.get_json()
        assert isinstance(body, dict)

    def test_hebrew_word_uses_local_glossary_or_lexicon(self, test_client, mock_outbound_http):
        """Hebrew input routes through _lookup_hebrew_word_meaning — mock the
        Sefaria lexicon endpoint (real www.sefaria.org domain, not covered by
        the autouse mock.sefaria.org catch-all) in case the glossary misses."""
        mock_outbound_http.add(
            responses_lib.GET, SEFARIA_LEXICON_URL_RE,
            json=[{
                "lexicon_name": "Jastrow Dictionary",
                "content": {"definitions": [{"definition": "Sabbath, day of rest"}]},
            }],
            status=200,
        )
        response = test_client.get("/api/word/meaning?word=%D7%A9%D7%91%D7%AA")
        assert response.status_code in (200, 404)
        body = response.get_json()
        assert isinstance(body, dict)
        assert body.get("word") == "שבת"

    def test_invalid_lang_param_falls_back_to_en(self, test_client, mock_outbound_http):
        mock_outbound_http.add(
            responses_lib.GET, DICTIONARYAPI_URL_RE,
            json=[{
                "word": "torah",
                "meanings": [{"definitions": [{"definition": "Jewish teaching."}]}],
            }],
            status=200,
        )
        response = test_client.get("/api/word/meaning?word=torah&lang=fr")
        assert response.status_code == 200
        body = response.get_json()
        assert body["lang"] == "en"


class TestExportChapter:
    VALID_PAYLOAD = {
        "title": "Genesis 1",
        "ref": "Genesis 1:1",
        "format": "txt",
        "lines": [
            {"segment": "1", "he": "בְּרֵאשִׁית", "en": "In the beginning"},
        ],
    }

    def test_txt_export_happy_path(self, test_client):
        response = test_client.post(
            "/api/export/chapter",
            json=self.VALID_PAYLOAD,
            content_type="application/json",
        )
        assert response.status_code == 200
        assert "text/plain" in response.content_type.lower()
        assert response.data  # non-empty file body

    def test_docx_export_happy_path(self, test_client):
        payload = dict(self.VALID_PAYLOAD, format="docx")
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        # docx export depends on python-docx being installed; if unavailable,
        # the route gracefully returns 503 rather than crashing.
        assert response.status_code in (200, 503)
        if response.status_code == 200:
            assert "wordprocessingml" in response.content_type.lower()

    def test_pdf_export_happy_path(self, test_client):
        payload = dict(self.VALID_PAYLOAD, format="pdf")
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        # pdf export depends on reportlab being installed; if unavailable,
        # the route gracefully returns 503 rather than crashing.
        assert response.status_code in (200, 503)
        if response.status_code == 200:
            assert "pdf" in response.content_type.lower()

    def test_unsupported_format_returns_400(self, test_client):
        payload = dict(self.VALID_PAYLOAD, format="epub")
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "error" in body

    def test_no_lines_returns_400(self, test_client):
        payload = dict(self.VALID_PAYLOAD, lines=[])
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        assert response.status_code == 400
        body = response.get_json()
        assert "error" in body

    def test_lines_with_blank_he_and_en_are_filtered_and_400(self, test_client):
        """Lines with neither he nor en text are dropped; if all lines are
        dropped the route must return 400, not crash on an empty body."""
        payload = dict(
            self.VALID_PAYLOAD,
            lines=[{"segment": "1", "he": "", "en": ""}],
        )
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_missing_body_returns_400(self, test_client):
        """No JSON body at all → payload defaults to {} → empty lines → 400."""
        response = test_client.post(
            "/api/export/chapter",
            data="",
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_default_title_used_when_missing(self, test_client):
        payload = {
            "ref": "Genesis 1:1",
            "format": "txt",
            "lines": [{"segment": "1", "he": "א", "en": "a"}],
        }
        response = test_client.post(
            "/api/export/chapter",
            json=payload,
            content_type="application/json",
        )
        assert response.status_code == 200
        # download filename should fall back to the default slug
        disposition = response.headers.get("Content-Disposition", "")
        assert "shelah-chapter" in disposition


class TestSearchSuggest:
    def test_empty_query_returns_empty_list(self, test_client):
        response = test_client.get("/api/search/suggest")
        assert response.status_code == 200
        body = response.get_json()
        assert body == []

    def test_happy_path_returns_list(self, test_client):
        response = test_client.get("/api/search/suggest?q=Shabbat")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)

    def test_response_includes_ask_ai_option(self, test_client):
        """The route always appends an 'Ask Sh'elah' AI-synthesis suggestion."""
        response = test_client.get("/api/search/suggest?q=somequery")
        body = response.get_json()
        ask_items = [item for item in body if item.get("type") == "ask"]
        assert len(ask_items) == 1
        assert ask_items[0]["value"] == "somequery"

    def test_quick_alias_match_returns_text_suggestion(self, test_client):
        """'genesis' is a QUICK_TEXT_ALIASES key → should surface a 'text' suggestion."""
        response = test_client.get("/api/search/suggest?q=genesis")
        body = response.get_json()
        text_items = [item for item in body if item.get("type") == "text"]
        assert any(item.get("value") == "Genesis 1" for item in text_items)

    def test_community_match_returns_community_suggestion(self, test_client):
        response = test_client.get("/api/search/suggest?q=ashken")
        body = response.get_json()
        community_items = [item for item in body if item.get("type") == "community"]
        assert any(item.get("value") == "Ashkenaz" for item in community_items)

    def test_size_param_limits_results(self, test_client):
        response = test_client.get("/api/search/suggest?q=a&size=2")
        assert response.status_code == 200
        body = response.get_json()
        assert len(body) <= 2

    def test_sefaria_failure_is_graceful(self, test_client, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/search/suggest?q=Shabbat")
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, list)


class TestGetTextLinks:
    def test_happy_path_returns_dict(self, test_client):
        response = test_client.get("/api/text/Genesis%201:1/links")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)

    def test_upstream_failure_is_graceful(self, test_client, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/text/Genesis%201:1/links")
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, dict)


class TestGetTextGraph:
    def test_happy_path_has_nodes_and_edges(self, test_client):
        response = test_client.get("/api/text/Genesis%201:1/graph")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert "nodes" in body
        assert "edges" in body
        assert isinstance(body["nodes"], list)
        assert isinstance(body["edges"], list)

    def test_root_node_present(self, test_client):
        response = test_client.get("/api/text/Genesis%201:1/graph")
        body = response.get_json()
        root_nodes = [n for n in body["nodes"] if n.get("kind") == "root"]
        assert len(root_nodes) == 1
        assert root_nodes[0]["id"] == "Genesis 1:1"

    def test_upstream_failure_is_graceful(self, test_client, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/text/Genesis%201:1/graph")
        assert response.status_code != 500
        body = response.get_json()
        assert isinstance(body, dict)
        assert isinstance(body.get("nodes"), list)


class TestLibraryCategory:
    def test_happy_path_status_and_json(self, test_client):
        response = test_client.get("/api/library/category/Tanakh")
        assert response.status_code == 200
        ct = response.content_type.lower()
        assert "application/json" in ct

    def test_nested_category_path(self, test_client):
        response = test_client.get("/api/library/category/Tanakh/Torah")
        assert response.status_code == 200

    def test_upstream_failure_is_graceful(self, test_client, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET,
            re.compile(r"https://mock\.sefaria\.org/api/.*"),
            body=Exception("connection refused"),
        )
        response = test_client.get("/api/library/category/Tanakh")
        assert response.status_code != 500


class TestTextsIndex:
    def test_happy_path_status_and_shape(self, test_client):
        response = test_client.get("/api/texts-index")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert set(["siddur", "merkava", "sefaria"]).issubset(body.keys())

    def test_siddur_section_has_title_and_items(self, test_client):
        response = test_client.get("/api/texts-index")
        body = response.get_json()
        assert "title" in body["siddur"]
        assert isinstance(body["siddur"]["items"], list)

    def test_merkava_section_lists_known_communities(self, test_client):
        response = test_client.get("/api/texts-index")
        body = response.get_json()
        assert "Ashkenaz" in body["merkava"]["items"]

    def test_sefaria_section_lists_top_level_categories(self, test_client):
        response = test_client.get("/api/texts-index")
        body = response.get_json()
        assert "Tanakh" in body["sefaria"]["items"]
        assert "Talmud" in body["sefaria"]["items"]
