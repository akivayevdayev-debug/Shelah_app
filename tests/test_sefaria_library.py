"""
Coverage-expansion tests for backend/sefaria_library.py's public API and pure
logic helpers, complementing tests/test_sefaria_library_cache.py (which
covers only the low-level _cached_get memory-tier behavior).

All module-level mutable caches (_cache, _resolved_title_ref_cache,
_resolved_query_ref_cache, _title_catalog_cache, _search_query_cache,
_library_index_view_cache, _library_index_adjustments_cache) are reset around
every test so tests can't leak state into each other. _load_library_index_adjustments
is monkeypatched to a controlled fixture in most tests to isolate them from the
717KB real reports/library_leaf_remove_fix_report.full.json on disk.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib

import backend.sefaria_library as sl


@pytest.fixture(autouse=True)
def _reset_all_module_caches(monkeypatch):
    sl._cache.clear()
    sl._resolved_title_ref_cache.clear()
    sl._resolved_query_ref_cache.clear()
    sl._search_query_cache.clear()
    sl._title_catalog_cache.update({"ts": 0, "report_mtime": 0.0, "data": []})
    sl._library_index_view_cache.update({"ts": 0.0, "report_mtime": 0.0, "data": None})
    sl._library_index_adjustments_cache.update({
        "loaded": False, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    sl._sefaria_block_status.update({
        "is_blocked": False, "http_status": None, "block_reason": "",
        "last_blocked_ts": 0.0, "consecutive_blocks": 0,
    })
    monkeypatch.setattr(sl, "_disk_cache_get", lambda url: None)
    monkeypatch.setattr(sl, "_disk_cache_set", lambda url, data: None)
    # Default: no removal/fix adjustments, isolated from the real report file.
    monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
        "loaded": True, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    yield
    sl._cache.clear()


# ─────────────────────────── Pure logic helpers ───────────────────────────

class TestNormalizeTitleKey:
    def test_strips_non_alnum_and_lowercases(self):
        assert sl._normalize_title_key("Kinnot for Tisha B'Av (Ashkenaz)") == "kinnotfortishabavashkenaz"

    def test_none_and_empty(self):
        assert sl._normalize_title_key(None) == ""
        assert sl._normalize_title_key("") == ""


class TestNormalizeFilterValues:
    def test_none_returns_empty_list(self):
        assert sl._normalize_filter_values(None) == []

    def test_comma_separated_string(self):
        assert sl._normalize_filter_values("Ashkenaz, Sefardic") == ["ashkenaz", "sefardic"]

    def test_list_input(self):
        assert sl._normalize_filter_values(["Ashkenaz", " Sefardic "]) == ["ashkenaz", "sefardic"]

    def test_scalar_input(self):
        assert sl._normalize_filter_values(42) == ["42"]


class TestNormalizeToList:
    def test_list_passthrough_stringified(self):
        assert sl._normalize_to_list([1, "a", None, "b"]) == ["1", "a", "b"]

    def test_string_wrapped_in_list(self):
        assert sl._normalize_to_list("solo") == ["solo"]

    def test_other_types_empty(self):
        assert sl._normalize_to_list(42) == []
        assert sl._normalize_to_list(None) == []


class TestInferNusach:
    def test_ashkenaz(self):
        assert sl._infer_nusach("Siddur Ashkenaz") == "Ashkenaz"

    def test_sefard_and_sephard_variants(self):
        assert sl._infer_nusach("Siddur Sefard") == "Sefardic"
        assert sl._infer_nusach("Sephardic Siddur") == "Sefardic"

    def test_mizrahi(self):
        assert sl._infer_nusach("Nusach Mizrahi") == "Mizrahi"

    def test_yemenite(self):
        assert sl._infer_nusach("Yemenite custom") == "Yemenite"

    def test_no_match_returns_empty(self):
        assert sl._infer_nusach("Genesis 1", categories=["Tanakh"]) == ""

    def test_checks_categories_too(self):
        assert sl._infer_nusach("Some Ref", categories=["Ashkenaz Prayer"]) == "Ashkenaz"


class TestMatchesMetadataFilters:
    def test_no_filters_always_matches(self):
        assert sl._matches_metadata_filters({}, None) is True
        assert sl._matches_metadata_filters({}, {}) is True

    def test_matching_single_filter(self):
        result = {"era": "Rishonim", "authors": [], "categories": [], "path": "", "geography": "", "nusach": "", "ref": ""}
        assert sl._matches_metadata_filters(result, {"era": "rishonim"}) is True

    def test_non_matching_filter_returns_false(self):
        result = {"era": "Rishonim", "authors": [], "categories": [], "path": "", "geography": "", "nusach": "", "ref": ""}
        assert sl._matches_metadata_filters(result, {"era": "acharonim"}) is False

    def test_unknown_filter_key_with_no_haystack_returns_false(self):
        result = {"era": "", "authors": [], "categories": [], "path": "", "geography": "", "nusach": "", "ref": ""}
        assert sl._matches_metadata_filters(result, {"author": "rambam"}) is False

    def test_all_filter_keys_must_match(self):
        result = {
            "era": "Rishonim", "authors": ["Rambam"], "categories": ["Halakhah"],
            "path": "Mishneh Torah", "geography": "", "nusach": "", "ref": "Mishneh Torah 1",
        }
        assert sl._matches_metadata_filters(result, {"era": "rishonim", "author": "rambam"}) is True
        assert sl._matches_metadata_filters(result, {"era": "rishonim", "author": "ramban"}) is False


class TestEncodeRefPath:
    def test_spaces_become_underscores_then_encoded(self):
        assert sl._encode_ref_path("Genesis 1:1") == "Genesis_1:1"

    def test_none_becomes_empty_string(self):
        assert sl._encode_ref_path(None) == ""

    def test_special_chars_preserved_in_safe_set(self):
        encoded = sl._encode_ref_path("Rashi on Genesis 1:1")
        assert " " not in encoded
        assert ":" in encoded


class TestNormalizeRequestedRef:
    def test_single_encoded_value_decoded(self):
        assert sl._normalize_requested_ref("Genesis%201%3A1") == "Genesis 1:1"

    def test_double_encoded_value_fully_decoded(self):
        assert sl._normalize_requested_ref("Genesis%25201%253A1") == "Genesis 1:1"

    def test_plain_value_unchanged(self):
        assert sl._normalize_requested_ref("Genesis 1:1") == "Genesis 1:1"

    def test_none_becomes_empty(self):
        assert sl._normalize_requested_ref(None) == ""


class TestBuildTextUrl:
    def test_builds_expected_url_shape(self):
        url = sl._build_text_url("Genesis 1:1", lang="he", context=1)
        assert url.startswith(f"{sl.SEFARIA_API}/texts/Genesis_1:1")
        assert "lang=he" in url
        assert "context=1" in url

    def test_v3_url_requests_source_and_translation(self):
        url = sl._build_v3_text_url("Genesis 1:1")
        assert url.startswith(f"{sl.SEFARIA_V3_API}/texts/Genesis_1:1")
        assert "version=source" in url
        assert "version=translation" in url


class TestParseV3Response:
    def test_not_a_dict_returns_none(self):
        assert sl._parse_v3_response(["not", "a", "dict"], "Genesis 1") is None

    def test_error_field_returns_none(self):
        assert sl._parse_v3_response({"error": "nope"}, "Genesis 1") is None

    def test_no_versions_returns_none(self):
        assert sl._parse_v3_response({"versions": []}, "Genesis 1") is None

    def test_happy_path_extracts_hebrew_and_english(self):
        data = {
            "ref": "Genesis 1:1",
            "title": "Genesis",
            "heTitle": "בראשית",
            "categories": ["Tanakh", "Torah"],
            "versions": [
                {"language": "he", "direction": "rtl", "isSource": True, "text": ["בְּרֵאשִׁית"]},
                {"language": "en", "direction": "ltr", "isSource": False, "text": ["In the beginning"]},
            ],
        }
        result = sl._parse_v3_response(data, "Genesis 1:1")
        assert result["ref"] == "Genesis 1:1"
        assert result["title"] == "Genesis"
        assert result["he"] == ["בְּרֵאשִׁית"]
        assert result["en"] == ["In the beginning"]
        assert len(result["lines"]) == 1
        assert result["lines"][0]["segment"] == "1"

    def test_falls_back_to_ref_derived_title_when_no_title_fields(self):
        data = {
            "ref": "Genesis 1:1",
            "versions": [{"language": "he", "direction": "rtl", "isSource": True, "text": ["x"]}],
        }
        result = sl._parse_v3_response(data, "Genesis 1:1")
        assert result["title"] == "Genesis 1:1"

    def test_no_extractable_text_returns_none(self):
        data = {"ref": "Genesis 1:1", "versions": [{"language": "he", "direction": "rtl", "isSource": True, "text": []}]}
        assert sl._parse_v3_response(data, "Genesis 1:1") is None


class TestIsSpecificRefQuery:
    def test_chapter_verse_is_specific(self):
        assert sl._is_specific_ref_query("Genesis 1:1") is True

    def test_bare_number_is_specific(self):
        assert sl._is_specific_ref_query("Genesis 1") is True

    def test_daf_with_amud_is_specific(self):
        assert sl._is_specific_ref_query("Berakhot 2a") is True

    def test_title_only_is_not_specific(self):
        assert sl._is_specific_ref_query("Genesis") is False

    def test_comma_suffix_with_digit_is_specific(self):
        assert sl._is_specific_ref_query("Shulchan Arukh, Orach Chayim 1") is True


class TestSplitTitleSuffix:
    def test_no_comma_returns_whole_as_title(self):
        assert sl._split_title_suffix("Genesis") == ("Genesis", "")

    def test_comma_splits_title_and_suffix(self):
        assert sl._split_title_suffix("Shulchan Arukh, Orach Chayim") == ("Shulchan Arukh", "Orach Chayim")


class TestPruneAndFixLibraryIndex:
    def test_removes_matching_title(self):
        node = {"title": "Removable Work", "contents": []}
        remove_keys = {sl._normalize_title_key("Removable Work")}
        assert sl._prune_and_fix_library_index(node, remove_keys, {}) is None

    def test_applies_fix_map_to_first_section_ref(self):
        node = {"title": "Broken Work"}
        fix_map = {sl._normalize_title_key("Broken Work"): "Broken Work 1"}
        result = sl._prune_and_fix_library_index(node, set(), fix_map)
        assert result["firstSectionRef"] == "Broken Work 1"

    def test_does_not_overwrite_existing_distinct_ref(self):
        node = {"title": "Broken Work", "firstSectionRef": "Already Set 5"}
        fix_map = {sl._normalize_title_key("Broken Work"): "Broken Work 1"}
        result = sl._prune_and_fix_library_index(node, set(), fix_map)
        assert result["firstSectionRef"] == "Already Set 5"

    def test_recurses_into_contents_and_prunes_children(self):
        node = {
            "title": "Category",
            "contents": [{"title": "Keep Me"}, {"title": "Remove Me"}],
        }
        remove_keys = {sl._normalize_title_key("Remove Me")}
        result = sl._prune_and_fix_library_index(node, remove_keys, {})
        titles = [c["title"] for c in result["contents"]]
        assert titles == ["Keep Me"]

    def test_list_input_prunes_each_element(self):
        nodes = [{"title": "Keep"}, {"title": "Drop"}]
        result = sl._prune_and_fix_library_index(nodes, {sl._normalize_title_key("Drop")}, {})
        assert len(result) == 1
        assert result[0]["title"] == "Keep"

    def test_non_dict_non_list_passthrough(self):
        assert sl._prune_and_fix_library_index("scalar", set(), {}) == "scalar"


class TestFlattenIndexTitles:
    def test_flattens_nested_contents_and_dedupes(self):
        index = [
            {"title": "Genesis", "categories": ["Tanakh", "Torah"], "heTitle": "בראשית"},
            {"title": "Torah", "contents": [
                {"title": "Genesis", "categories": ["Tanakh"]},  # duplicate, should be skipped
                {"title": "Exodus", "categories": ["Tanakh", "Torah"]},
            ]},
        ]
        rows = []
        sl._flatten_index_titles(index, rows, set())
        titles = [r["title"] for r in rows]
        assert titles.count("Genesis") == 1
        assert "Exodus" in titles


class TestGetEnTitle:
    def test_extracts_from_titles_list(self):
        node = {"titles": [{"lang": "he", "text": "בראשית"}, {"lang": "en", "text": "Genesis"}]}
        assert sl._get_en_title(node) == "Genesis"

    def test_falls_back_to_title_field(self):
        assert sl._get_en_title({"title": "Genesis"}) == "Genesis"

    def test_falls_back_to_key_field(self):
        assert sl._get_en_title({"key": "genesis_key"}) == "genesis_key"

    def test_non_dict_returns_empty(self):
        assert sl._get_en_title("not a dict") == ""


class TestIsSameWorkTitle:
    def test_exact_match(self):
        assert sl._is_same_work_title("Genesis", "Genesis") is True

    def test_substring_match(self):
        assert sl._is_same_work_title("Genesis", "Genesis 1") is True

    def test_fuzzy_match_minor_typo(self):
        assert sl._is_same_work_title("Berakhot", "Brakhot") is True

    def test_no_match(self):
        assert sl._is_same_work_title("Genesis", "Exodus") is False

    def test_empty_inputs_no_match(self):
        assert sl._is_same_work_title("", "Genesis") is False
        assert sl._is_same_work_title("Genesis", "") is False


# ─────────────────────────── _cached_get error branches ───────────────────

class TestCachedGetErrorBranches:
    def test_403_marks_sefaria_blocked(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            url = f"{sl.SEFARIA_API}/blocked-endpoint"
            rsps.add(responses_lib.GET, url, status=403)
            result = sl._cached_get(url)
            assert result is None
            assert sl._sefaria_block_status["is_blocked"] is True
            assert sl._sefaria_block_status["http_status"] == 403
            assert sl._sefaria_block_status["consecutive_blocks"] == 1

    def test_404_returns_none_without_marking_blocked(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            url = f"{sl.SEFARIA_API}/missing-endpoint"
            rsps.add(responses_lib.GET, url, status=404)
            result = sl._cached_get(url)
            assert result is None
            assert sl._sefaria_block_status["is_blocked"] is False

    def test_500_returns_none(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            url = f"{sl.SEFARIA_API}/error-endpoint"
            rsps.add(responses_lib.GET, url, status=500)
            assert sl._cached_get(url) is None

    def test_connection_error_returns_none(self):
        import requests as _requests
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            url = f"{sl.SEFARIA_API}/conn-error-endpoint"
            rsps.add(responses_lib.GET, url, body=_requests.ConnectionError("boom"))
            assert sl._cached_get(url) is None

    def test_successful_fetch_clears_prior_blocked_status(self):
        sl._sefaria_block_status.update({"is_blocked": True, "http_status": 403, "consecutive_blocks": 3})
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            url = f"{sl.SEFARIA_API}/recovered-endpoint"
            rsps.add(responses_lib.GET, url, json={"ok": True}, status=200)
            result = sl._cached_get(url)
            assert result == {"ok": True}
            assert sl._sefaria_block_status["is_blocked"] is False
            assert sl._sefaria_block_status["consecutive_blocks"] == 0


# ─────────────────────────── Public API (network-backed) ──────────────────

class TestGetLibraryIndex:
    def test_empty_response_returns_empty_list(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, f"{sl.SEFARIA_API}/index", json=None, status=200)
            assert sl.get_library_index() == []

    def test_applies_prune_and_fix_adjustments(self, monkeypatch):
        monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
            "loaded": True, "mtime": 1.0,
            "remove_keys": {sl._normalize_title_key("Bad Work")},
            "fix_map": {},
        })
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, f"{sl.SEFARIA_API}/index",
                json=[{"title": "Good Work"}, {"title": "Bad Work"}],
                status=200,
            )
            result = sl.get_library_index()
            titles = [n["title"] for n in result]
            assert titles == ["Good Work"]

    def test_second_call_uses_view_cache_not_network(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, f"{sl.SEFARIA_API}/index", json=[{"title": "X"}], status=200)
            first = sl.get_library_index()
            second = sl.get_library_index()
            assert first == second
            matching = [c for c in rsps.calls if "/index" in c.request.url]
            assert len(matching) == 1


class TestGetCategoryContents:
    def test_direct_index_path_success(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, f"{sl.SEFARIA_API}/index/Tanakh,Torah",
                json={"category": "Torah", "contents": []}, status=200,
            )
            result = sl.get_category_contents("Tanakh/Torah")
            assert result["category"] == "Torah"

    def test_falls_back_to_library_index_traversal(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, f"{sl.SEFARIA_API}/index/Tanakh", json={"error": "not found"}, status=200)
            rsps.add(
                responses_lib.GET, f"{sl.SEFARIA_API}/index",
                json=[{"category": "Tanakh", "contents": [{"title": "Genesis"}]}],
                status=200,
            )
            result = sl.get_category_contents("Tanakh")
            assert result["category"] == "Tanakh"
            assert result["contents"][0]["title"] == "Genesis"

    def test_empty_category_path_returns_empty_list(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, f"{sl.SEFARIA_API}/index/", json={"error": "x"}, status=200)
            assert sl.get_category_contents("") == []


class TestGetText:
    def test_v3_happy_path(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(re.escape(sl.SEFARIA_V3_API) + r"/texts/.*"),
                json={
                    "ref": "Genesis 1:1",
                    "title": "Genesis",
                    "versions": [
                        {"language": "he", "direction": "rtl", "isSource": True, "text": ["בְּרֵאשִׁית"]},
                        {"language": "en", "direction": "ltr", "isSource": False, "text": ["In the beginning"]},
                    ],
                },
                status=200,
            )
            result = sl.get_text("Genesis 1:1")
            assert result["ref"] == "Genesis 1:1"
            assert result["he"] == ["בְּרֵאשִׁית"]
            assert result["en"] == ["In the beginning"]

    def test_v3_fails_falls_back_to_v2(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/texts/.*"),
                json={"error": "not found"}, status=200,
            )
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/texts/.*"),
                json={
                    "ref": "Genesis 1:1", "he": ["בְּרֵאשִׁית"], "text": ["In the beginning"],
                    "title": "Genesis", "categories": ["Tanakh", "Torah"],
                },
                status=200,
            )
            result = sl.get_text("Genesis 1:1")
            assert result["ref"] == "Genesis 1:1"
            assert result["he"] == ["בְּרֵאשִׁית"]
            assert result["en"] == ["In the beginning"]

    def test_empty_ref_returns_error_shape(self):
        result = sl.get_text("")
        assert result["error"] == "Text not found"
        assert result["he"] == []
        assert result["en"] == []

    def test_not_found_when_all_attempts_fail(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/.*"),
                json={"error": "nope"}, status=200,
            )
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/.*"),
                json={"error": "nope"}, status=200,
            )
            result = sl.get_text("Nonexistent Work 99:99")
            assert result["error_type"] == "not_found"
            assert result["he"] == []
            assert result["en"] == []

    def test_blocked_status_surfaces_specific_error(self):
        # Real HTTP 403s (not 200-with-error-body) so _cached_get's block-status
        # handling actually fires instead of clearing the flag on a "successful" 200.
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/.*"),
                status=403,
            )
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/.*"),
                status=403,
            )
            result = sl.get_text("Genesis 1:1")
            assert result["error_type"] == "sefaria_blocked"
            assert "Cloudflare" in result["error"]


class TestCheckSefariaAvailability:
    def test_both_apis_available(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/.*"),
                json={"ref": "Genesis 1"}, status=200,
            )
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/.*"),
                json={"ref": "Genesis 1"}, status=200,
            )
            result = sl.check_sefaria_availability()
            assert result["overall_available"] is True
            assert result["block_type"] == ""

    def test_both_apis_blocked_by_cloudflare(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/.*"), status=403)
            rsps.add(responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/.*"), status=403)
            result = sl.check_sefaria_availability()
            assert result["overall_available"] is False
            assert result["block_type"] == "cloudflare_403"

    def test_both_apis_down_marks_unavailable(self):
        import requests as _requests
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_V3_API) + r"/.*"),
                body=_requests.ConnectionError("down"),
            )
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/.*"),
                body=_requests.ConnectionError("down"),
            )
            result = sl.check_sefaria_availability()
            assert result["overall_available"] is False
            assert result["block_type"] == "unavailable"


class TestSearchLibrary:
    def test_empty_query_returns_empty_list(self):
        assert sl.search_library("") == []
        assert sl.search_library("   ") == []

    def test_direct_ref_completion(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/name/.*"),
                json={"is_ref": True, "ref": "Genesis 1:1", "book": "Genesis", "completion_objects": []},
                status=200,
            )
            result = sl.search_library("Genesis 1:1", size=5)
            assert len(result) == 1
            assert result[0]["ref"] == "Genesis 1:1"

    def test_size_caps_results(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/name/.*"),
                json={
                    "is_ref": False,
                    "completion_objects": [
                        {"type": "ref", "key": f"Book {i}", "title": f"Book {i}"} for i in range(5)
                    ],
                },
                status=200,
            )
            result = sl.search_library("book", size=2)
            assert len(result) <= 2


class TestGetLinkedTexts:
    def test_groups_links_by_category(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/related/.*"),
                json={"links": [
                    {"type": "commentary", "category": "Commentary", "ref": "Rashi on Genesis 1:1", "heRef": "", "anchorRef": "Genesis 1:1"},
                ]},
                status=200,
            )
            result = sl.get_linked_texts("Genesis 1:1")
            assert "Commentary" in result
            assert result["Commentary"][0]["ref"] == "Rashi on Genesis 1:1"

    def test_injects_rashi_for_tanakh_ref_when_missing(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/related/.*"),
                json={"links": []}, status=200,
            )
            result = sl.get_linked_texts("Genesis 1:1")
            assert "Commentary" in result
            assert result["Commentary"][0]["ref"] == "Rashi on Genesis 1:1"

    def test_no_data_returns_empty_dict(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/related/.*"),
                status=500,
            )
            assert sl.get_linked_texts("Genesis 1:1") == {}

    def test_non_tanakh_ref_does_not_inject_rashi(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/related/.*"),
                json={"links": []}, status=200,
            )
            result = sl.get_linked_texts("Mishnah Berakhot 1:1")
            assert result == {}


class TestGetPopularTexts:
    def test_returns_expected_category_shape(self):
        result = sl.get_popular_texts()
        assert set(result.keys()) == {"Tanakh", "Mishnah", "Talmud", "Halakhah"}
        for category, items in result.items():
            assert isinstance(items, list) and items
            for item in items:
                assert {"title", "ref", "he", "description"} <= set(item.keys())


class TestGetIndexEntry:
    def test_returns_dict_on_success(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/index/.*"),
                json={"title": "Genesis", "categories": ["Tanakh", "Torah"]}, status=200,
            )
            result = sl.get_index_entry("Genesis")
            assert result["title"] == "Genesis"

    def test_returns_empty_dict_on_failure(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(re.escape(sl.SEFARIA_API) + r"/index/.*"),
                status=404,
            )
            assert sl.get_index_entry("Nonexistent") == {}


class TestGetLiturgyBooks:
    def test_filters_liturgy_category_and_excludes_non_loading(self, monkeypatch):
        non_loading_title = "Kinnot for Tisha B'Av (Ashkenaz)"
        index = [
            {"title": "Weekday Siddur", "categories": ["Liturgy"], "dependence": None},
            {"title": non_loading_title, "categories": ["Liturgy"], "dependence": None},
            {"title": "Commentary on Siddur", "categories": ["Liturgy"], "dependence": "Commentary"},
            {"title": "Genesis", "categories": ["Tanakh"]},
        ]
        monkeypatch.setattr(sl, "get_library_index", lambda: index)
        result = sl.get_liturgy_books()
        titles = {b["title"] for b in result}
        assert "Weekday Siddur" in titles
        assert non_loading_title not in titles
        assert "Commentary on Siddur" not in titles
        assert "Genesis" not in titles

    def test_include_commentary_true_includes_commentary_works(self, monkeypatch):
        index = [{"title": "Commentary on Siddur", "categories": ["Liturgy"], "dependence": "Commentary"}]
        monkeypatch.setattr(sl, "get_library_index", lambda: index)
        result = sl.get_liturgy_books(include_commentary=True)
        assert any(b["title"] == "Commentary on Siddur" for b in result)

    def test_non_list_index_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: {"not": "a list"})
        assert sl.get_liturgy_books() == []


class TestGetIndexLeafRefs:
    def test_no_schema_returns_empty(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {})
        assert sl.get_index_leaf_refs("Genesis") == []

    def test_leaf_node_without_children_becomes_ref(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {"key": "default", "nodes": []},
        })
        result = sl.get_index_leaf_refs("Genesis")
        assert result == ["Genesis"]

    def test_walks_nested_schema_nodes(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {
                "titles": [{"lang": "en", "text": "Siddur"}],
                "nodes": [
                    {"titles": [{"lang": "en", "text": "Morning"}], "key": "morning", "nodes": []},
                    {"titles": [{"lang": "en", "text": "Evening"}], "key": "evening", "nodes": []},
                ],
            },
        })
        result = sl.get_index_leaf_refs("Siddur")
        assert "Siddur, Morning" in result
        assert "Siddur, Evening" in result
