"""
Behavioural tests for the second half of backend/sefaria_library.py (the
catalog search collector, get_library_index / category-path walking, the v2
text-fetch attempt helpers, the search-result assembly helpers behind
search_library, the liturgy/index-schema walkers, ...).

Everything Sefaria-facing (HTTP, index entries, the adjustments report, the
shared Redis tier) is stubbed per test, so nothing here touches the network
or disk. Module-level mutable caches are reset around each test.
"""

from __future__ import annotations

import time

import pytest

import backend.sefaria_library as sl


@pytest.fixture(autouse=True)
def _isolated_sefaria_state(monkeypatch):
    sl._cache.clear()
    sl._resolved_title_ref_cache.clear()
    sl._resolved_query_ref_cache.clear()
    sl._search_query_cache.clear()
    monkeypatch.setattr(sl, "_library_index_view_cache",
                        {"ts": 0.0, "report_mtime": 0.0, "data": None})
    monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
        "loaded": True, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    yield
    sl._cache.clear()
    sl._resolved_title_ref_cache.clear()
    sl._resolved_query_ref_cache.clear()
    sl._search_query_cache.clear()


def _recording_cached_get(monkeypatch, responder):
    """Replace sl._cached_get with a recorder; `responder(url)` supplies the
    payload. Returns the list of (url, ttl) calls."""
    calls = []

    def fake(url, ttl=sl.CACHE_TTL):
        calls.append((url, ttl))
        return responder(url)

    monkeypatch.setattr(sl, "_cached_get", fake)
    return calls


def _stub_index_entries(monkeypatch, entries):
    """Replace sl.get_index_entry with a dict lookup; returns the list of
    titles it was asked for."""
    lookups = []

    def fake(title):
        lookups.append(title)
        return entries.get(title, {})

    monkeypatch.setattr(sl, "get_index_entry", fake)
    return lookups


# ─── _collect_catalog_search_results ───────────────────────────────────────

class TestCollectCatalogSearchResults:
    def test_stops_building_once_size_results_are_collected(self, monkeypatch):
        built = []

        def fake_build(row, seen_refs, metadata_filters):
            built.append(row["title"])
            return {"ref": row["title"]}

        monkeypatch.setattr(sl, "_build_catalog_search_result", fake_build)
        rows = [(100, {"title": t}) for t in ("A", "B", "C", "D")]

        results = sl._collect_catalog_search_results(rows, 2, None)

        assert [r["ref"] for r in results] == ["A", "B"]
        assert built == ["A", "B"]

    def test_skipped_rows_do_not_count_toward_size(self, monkeypatch):
        def fake_build(row, seen_refs, metadata_filters):
            return None if row["title"] == "A" else {"ref": row["title"]}

        monkeypatch.setattr(sl, "_build_catalog_search_result", fake_build)
        rows = [(100, {"title": t}) for t in ("A", "B", "C", "D")]

        results = sl._collect_catalog_search_results(rows, 2, None)

        assert [r["ref"] for r in results] == ["B", "C"]

    def test_metadata_filters_and_shared_seen_set_reach_every_build(self, monkeypatch):
        seen_ids = set()
        filters_seen = []

        def fake_build(row, seen_refs, metadata_filters):
            seen_ids.add(id(seen_refs))
            filters_seen.append(metadata_filters)
            return {"ref": row["title"]}

        monkeypatch.setattr(sl, "_build_catalog_search_result", fake_build)
        rows = [(1, {"title": "A"}), (1, {"title": "B"})]

        sl._collect_catalog_search_results(rows, 5, {"era": "x"})

        assert len(seen_ids) == 1
        assert filters_seen == [{"era": "x"}, {"era": "x"}]

    @pytest.mark.parametrize("size,window", [(2, 30), (10, 50)])
    def test_scan_window_is_the_larger_of_thirty_and_five_times_size(
            self, monkeypatch, size, window):
        built = []

        def fake_build(row, seen_refs, metadata_filters):
            built.append(row["title"])
            return None

        monkeypatch.setattr(sl, "_build_catalog_search_result", fake_build)
        rows = [(1, {"title": f"T{i}"}) for i in range(80)]

        assert sl._collect_catalog_search_results(rows, size, None) == []
        assert len(built) == window


# ─── get_library_index: adjusted-view edge cases ───────────────────────────

class TestGetLibraryIndexAdjustedView:
    def _wire(self, monkeypatch, *, remove_keys=(), mtime=5.0, payload=None):
        payload = [{"title": "X"}] if payload is None else payload
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=sl.CACHE_TTL: payload)
        monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
            "loaded": True, "mtime": mtime,
            "remove_keys": set(remove_keys), "fix_map": {},
        })
        monkeypatch.setattr(sl, "redis_cache_get", lambda key: None)
        stores = []
        monkeypatch.setattr(
            sl, "redis_cache_set",
            lambda key, value, ttl: stores.append((key, value, ttl)))
        return stores

    def test_fully_pruned_root_becomes_an_empty_list_and_is_cached(self, monkeypatch):
        stores = self._wire(monkeypatch, remove_keys={sl._normalize_title_key("Bad Work")},
                            payload={"title": "Bad Work"})
        prune_calls = []
        real_prune = sl._prune_and_fix_library_index

        def counting_prune(*args):
            prune_calls.append(1)
            return real_prune(*args)

        monkeypatch.setattr(sl, "_prune_and_fix_library_index", counting_prune)

        assert sl.get_library_index() == []
        assert sl._library_index_view_cache["data"] == []
        assert sl._library_index_view_cache["report_mtime"] == 5.0
        assert stores == [(sl._LIBRARY_INDEX_VIEW_REDIS_KEY,
                           sl._library_index_view_cache, sl.CACHE_TTL)]

        # [] is a real (fresh) snapshot: a second call must not re-prune.
        assert sl.get_library_index() == []
        assert len(prune_calls) == 1

    def test_fresher_concurrent_snapshot_wins_over_the_stale_local_result(self, monkeypatch):
        stores = self._wire(monkeypatch)
        concurrent = {"ts": 0.0, "report_mtime": 5.0,
                      "data": [{"title": "from-other-thread"}]}

        def racing_prune(node, remove_keys, fix_map):
            # Another thread publishes a fresh view while we compute off-lock.
            concurrent["ts"] = time.time()
            sl._library_index_view_cache = concurrent
            return [{"title": "local"}]

        monkeypatch.setattr(sl, "_prune_and_fix_library_index", racing_prune)

        assert sl.get_library_index() == [{"title": "from-other-thread"}]
        assert sl._library_index_view_cache is concurrent
        assert stores == []  # the slower thread must not clobber Redis either

    def test_concurrent_snapshot_from_an_older_report_does_not_win(self, monkeypatch):
        stores = self._wire(monkeypatch, mtime=5.0)
        stale = {"ts": 0.0, "report_mtime": 1.0, "data": [{"title": "stale"}]}

        def racing_prune(node, remove_keys, fix_map):
            stale["ts"] = time.time()
            sl._library_index_view_cache = stale
            return [{"title": "local"}]

        monkeypatch.setattr(sl, "_prune_and_fix_library_index", racing_prune)

        assert sl.get_library_index() == [{"title": "local"}]
        assert sl._library_index_view_cache["data"] == [{"title": "local"}]
        assert sl._library_index_view_cache["report_mtime"] == 5.0
        assert len(stores) == 1 and stores[0][1]["data"] == [{"title": "local"}]


# ─── _find_category_child_node ─────────────────────────────────────────────

class TestFindCategoryChildNode:
    def test_non_dict_candidates_are_skipped(self):
        target = {"category": "Torah"}
        # A bare "Torah" string would blow up on .get() if it were not skipped.
        assert sl._find_category_child_node(
            ["Torah", None, 7, ["Torah"], target], "torah") is target

    def test_only_non_dict_candidates_yield_none(self):
        assert sl._find_category_child_node(["Torah", None, 3], "torah") is None

    def test_matches_on_title_and_hebrew_category_labels(self):
        by_title = {"title": "Genesis"}
        by_he = {"heCategory": "תורה"}
        assert sl._find_category_child_node([{"category": "X"}, by_title], "genesis") is by_title
        assert sl._find_category_child_node([{"category": "X"}, by_he], "תורה") is by_he


# ─── _walk_library_index_for_category_path / get_category_contents ─────────

_TANAKH_INDEX = [{
    "category": "Tanakh",
    "contents": [
        {"category": "Torah", "contents": [{"title": "Genesis"}]},
        {"category": "Prophets", "contents": None},
    ],
}]


class TestWalkLibraryIndexForCategoryPath:
    def test_nested_dict_nodes_are_descended_by_contents(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: _TANAKH_INDEX)

        node = sl._walk_library_index_for_category_path(["Tanakh", "Torah"])

        assert node == {"category": "Torah", "contents": [{"title": "Genesis"}]}

    def test_single_part_returns_the_top_level_node(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: _TANAKH_INDEX)

        assert sl._walk_library_index_for_category_path(["tanakh"]) is _TANAKH_INDEX[0]

    def test_unknown_child_returns_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: _TANAKH_INDEX)

        assert sl._walk_library_index_for_category_path(["Tanakh", "Nope"]) == []

    def test_null_contents_on_an_intermediate_node_returns_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: _TANAKH_INDEX)

        assert sl._walk_library_index_for_category_path(["Tanakh", "Prophets", "Isaiah"]) == []

    def test_a_dict_root_is_walked_through_its_contents(self, monkeypatch):
        root = {"contents": [{"category": "Tanakh", "marker": 1}]}
        monkeypatch.setattr(sl, "get_library_index", lambda: root)

        assert sl._walk_library_index_for_category_path(["Tanakh"]) == {
            "category": "Tanakh", "marker": 1}

    def test_a_root_that_is_neither_list_nor_dict_returns_an_empty_list(self, monkeypatch):
        monkeypatch.setattr(sl, "get_library_index", lambda: "garbage")

        assert sl._walk_library_index_for_category_path(["Tanakh"]) == []


class TestGetCategoryContentsFallback:
    def test_multi_part_path_falls_back_to_the_library_index_walk(self, monkeypatch):
        calls = _recording_cached_get(monkeypatch, lambda url: {"error": "not found"})
        monkeypatch.setattr(sl, "get_library_index", lambda: _TANAKH_INDEX)

        result = sl.get_category_contents("Tanakh/Torah")

        assert result == {"category": "Torah", "contents": [{"title": "Genesis"}]}
        assert calls == [(f"{sl.SEFARIA_API}/index/Tanakh,Torah", sl.CACHE_TTL)]

    def test_blank_path_segments_yield_no_parts(self, monkeypatch):
        _recording_cached_get(monkeypatch, lambda url: None)
        monkeypatch.setattr(sl, "get_library_index", lambda: pytest.fail("must not walk"))

        assert sl.get_category_contents("/ / ") == []


# ─── _flatten_text_with_path ───────────────────────────────────────────────

class TestFlattenTextWithPath:
    @pytest.mark.parametrize("value", [None, 7, 3.5, {"a": "b"}, True])
    def test_non_text_non_list_values_flatten_to_nothing(self, value):
        assert sl._flatten_text_with_path(value) == []

    def test_mixed_nested_lists_keep_one_based_paths_of_surviving_leaves(self):
        arr = ["a", None, ["  ", "b"], 5, "c"]

        assert sl._flatten_text_with_path(arr) == [
            ((1,), "a"), ((3, 2), "b"), ((5,), "c")]

    def test_strings_are_stripped(self):
        assert sl._flatten_text_with_path("  hi  ") == [((), "hi")]


# ─── _try_initial_text_attempts ────────────────────────────────────────────

class TestTryInitialTextAttempts:
    def test_blank_attempts_are_skipped_without_fetching(self, monkeypatch):
        calls = _recording_cached_get(monkeypatch, lambda url: {"ref": "Genesis 1", "text": []})
        tried = set()

        data, resolved = sl._try_initial_text_attempts(
            ["", None, "   ", "Genesis 1"], tried, "both", 0)

        assert calls == [(sl._build_text_url("Genesis 1", "both", 0), 86400)]
        assert data == {"ref": "Genesis 1", "text": []}
        assert resolved == "Genesis 1"
        assert tried == {"genesis 1"}

    def test_an_already_tried_attempt_is_not_fetched_again(self, monkeypatch):
        calls = _recording_cached_get(monkeypatch, lambda url: {"ref": "Genesis 1"})
        tried = {"genesis 1"}

        assert sl._try_initial_text_attempts(["Genesis 1"], tried, "both", 0) == (None, "")
        assert calls == []

    def test_case_variants_of_a_failed_attempt_are_fetched_only_once(self, monkeypatch):
        calls = _recording_cached_get(monkeypatch, lambda url: {"error": "nope"})

        result = sl._try_initial_text_attempts(
            ["Genesis 1", "GENESIS 1"], set(), "both", 0)

        assert result == (None, "")
        assert len(calls) == 1

    def test_error_payload_moves_on_to_the_next_attempt(self, monkeypatch):
        def responder(url):
            return {"error": "nope"} if "Cached" in url else {"ref": "Genesis 1:1"}

        calls = _recording_cached_get(monkeypatch, responder)

        data, resolved = sl._try_initial_text_attempts(
            ["Cached Ref", "Genesis 1"], set(), "en", 2)

        assert resolved == "Genesis 1:1"
        assert data == {"ref": "Genesis 1:1"}
        assert [u for u, _ in calls] == [
            sl._build_text_url("Cached Ref", "en", 2),
            sl._build_text_url("Genesis 1", "en", 2),
        ]

    def test_data_without_a_ref_resolves_to_the_attempted_ref(self, monkeypatch):
        _recording_cached_get(monkeypatch, lambda url: {"text": ["x"]})

        data, resolved = sl._try_initial_text_attempts(["  Genesis 1  "], set(), "both", 0)

        assert data == {"text": ["x"]}
        assert resolved == "Genesis 1"


# ─── _try_candidate_text_refs ──────────────────────────────────────────────

class TestTryCandidateTextRefs:
    def _candidates(self, monkeypatch, values):
        seen_requests = []

        def fake(raw_ref):
            seen_requests.append(raw_ref)
            return list(values)

        monkeypatch.setattr(sl, "_resolve_ref_candidates", fake)
        return seen_requests

    def test_returns_the_first_successful_candidates_own_ref(self, monkeypatch):
        requested = self._candidates(monkeypatch, ["Bereshit 1", "Genesis 1"])
        calls = _recording_cached_get(
            monkeypatch,
            lambda url: {"error": "x"} if "Bereshit" in url else {"ref": "Genesis 1:1-31"})
        tried = set()

        data, resolved = sl._try_candidate_text_refs("bereshit", tried, "both", 0)

        assert requested == ["bereshit"]
        assert (data, resolved) == ({"ref": "Genesis 1:1-31"}, "Genesis 1:1-31")
        assert [u for u, _ in calls] == [
            sl._build_text_url("Bereshit 1", "both", 0),
            sl._build_text_url("Genesis 1", "both", 0),
        ]
        assert all(ttl == 86400 for _, ttl in calls)
        assert tried == {"bereshit 1", "genesis 1"}

    def test_falls_back_to_the_candidate_when_the_payload_has_no_ref(self, monkeypatch):
        self._candidates(monkeypatch, ["Genesis 1"])
        _recording_cached_get(monkeypatch, lambda url: {"text": ["x"]})

        assert sl._try_candidate_text_refs("g", set(), "both", 0) == (
            {"text": ["x"]}, "Genesis 1")

    def test_candidates_already_tried_are_not_fetched(self, monkeypatch):
        self._candidates(monkeypatch, ["Genesis 1", "Exodus 1"])
        calls = _recording_cached_get(monkeypatch, lambda url: {"ref": "Exodus 1"})

        data, resolved = sl._try_candidate_text_refs("g", {"genesis 1"}, "both", 0)

        assert resolved == "Exodus 1"
        assert [u for u, _ in calls] == [sl._build_text_url("Exodus 1", "both", 0)]

    def test_no_successful_candidate_returns_none_and_empty_ref(self, monkeypatch):
        self._candidates(monkeypatch, ["A", "B"])
        calls = _recording_cached_get(monkeypatch, lambda url: {"error": "x"})

        assert sl._try_candidate_text_refs("g", set(), "both", 0) == (None, "")
        assert len(calls) == 2


# ─── _probe_sefaria_endpoint ───────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class _FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self._error is not None:
            raise self._error
        return self._response


class TestProbeSefariaEndpoint:
    URL = "https://example.test/api/texts/Genesis_1"

    def test_unparseable_json_on_a_200_is_reported_unavailable(self, monkeypatch):
        session = _FakeSession(_FakeResponse(200, json_error=ValueError("not json")))
        monkeypatch.setattr(sl, "_http_session", session)

        assert sl._probe_sefaria_endpoint(self.URL) == {
            "available": False, "http_status": 200, "error": "HTTP 200", "url": self.URL}

    def test_valid_body_is_available_and_uses_the_given_timeout(self, monkeypatch):
        session = _FakeSession(_FakeResponse(200, payload={"ref": "Genesis 1"}))
        monkeypatch.setattr(sl, "_http_session", session)

        result = sl._probe_sefaria_endpoint(self.URL, timeout=3)

        assert result == {"available": True, "http_status": 200, "error": "", "url": self.URL}
        assert session.calls == [(self.URL, 3)]

    def test_default_timeout_is_ten_seconds(self, monkeypatch):
        session = _FakeSession(_FakeResponse(200, payload={"ok": 1}))
        monkeypatch.setattr(sl, "_http_session", session)

        sl._probe_sefaria_endpoint(self.URL)

        assert session.calls == [(self.URL, 10)]

    @pytest.mark.parametrize("payload", [{}, None, {"error": "bad ref"}])
    def test_empty_or_error_body_on_a_200_is_unavailable(self, monkeypatch, payload):
        monkeypatch.setattr(sl, "_http_session", _FakeSession(_FakeResponse(200, payload=payload)))

        result = sl._probe_sefaria_endpoint(self.URL)

        assert result["available"] is False
        assert result["error"] == "HTTP 200"

    def test_non_200_status_is_unavailable_with_its_code(self, monkeypatch):
        monkeypatch.setattr(sl, "_http_session", _FakeSession(_FakeResponse(403)))

        assert sl._probe_sefaria_endpoint(self.URL) == {
            "available": False, "http_status": 403, "error": "HTTP 403", "url": self.URL}

    def test_request_exception_is_reported_with_no_status(self, monkeypatch):
        monkeypatch.setattr(sl, "_http_session", _FakeSession(error=RuntimeError("boom")))

        assert sl._probe_sefaria_endpoint(self.URL) == {
            "available": False, "http_status": None, "error": "boom", "url": self.URL}


# ─── _get_search_index_metadata ────────────────────────────────────────────

class TestGetSearchIndexMetadata:
    @pytest.mark.parametrize("book", [None, "", "   "])
    def test_blank_title_returns_empty_dict_without_a_lookup(self, monkeypatch, book):
        lookups = _stub_index_entries(monkeypatch, {})
        cache = {}

        assert sl._get_search_index_metadata(book, cache) == {}
        assert lookups == []
        assert cache == {}

    def test_lookup_is_cached_under_the_stripped_title(self, monkeypatch):
        lookups = _stub_index_entries(monkeypatch, {"Genesis": {"categories": ["Tanakh"]}})
        cache = {}

        first = sl._get_search_index_metadata(" Genesis ", cache)
        second = sl._get_search_index_metadata("Genesis", cache)

        assert first == second == {"categories": ["Tanakh"]}
        assert lookups == ["Genesis"]
        assert list(cache) == ["Genesis"]

    def test_non_dict_entry_is_stored_as_an_empty_dict(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: ["weird"])
        cache = {}

        assert sl._get_search_index_metadata("Genesis", cache) == {}
        assert cache == {"Genesis": {}}


# ─── _resolve_search_result_fixed_ref / _is_search_result_removed ──────────

class TestResolveSearchResultFixedRef:
    def test_fix_matched_through_the_label_replaces_the_ref_stripped(self):
        fix_map = {sl._normalize_title_key("Kitzur Shulchan Arukh"): "  Kitzur Shulchan Arukh 1 "}

        fixed, candidates = sl._resolve_search_result_fixed_ref(
            "Kitzur Shulchan Arukh, Foo", "Kitzur Shulchan Arukh", "Kitzur", {}, fix_map)

        assert fixed == "Kitzur Shulchan Arukh 1"
        assert candidates == [
            "Kitzur Shulchan Arukh, Foo", "Kitzur Shulchan Arukh", "Kitzur", None]

    def test_fix_matched_through_the_index_entry_title(self):
        fix_map = {"canon": "Canon 1"}

        fixed, candidates = sl._resolve_search_result_fixed_ref(
            "Old 3", "", "Old", {"title": "Canon"}, fix_map)

        assert fixed == "Canon 1"
        assert candidates[-1] == "Canon"

    def test_ref_itself_is_the_first_candidate(self):
        fix_map = {sl._normalize_title_key("Exact Ref"): "Fixed 1",
                   sl._normalize_title_key("label"): "Other 1"}

        fixed, _ = sl._resolve_search_result_fixed_ref(
            "Exact Ref", "label", "Exact", {}, fix_map)

        assert fixed == "Fixed 1"

    def test_no_match_returns_the_original_ref_and_candidates(self):
        fixed, candidates = sl._resolve_search_result_fixed_ref(
            "Genesis 1", "Genesis", "Genesis", ["not", "a", "dict"], {"other": "x"})

        assert fixed == "Genesis 1"
        assert candidates == ["Genesis 1", "Genesis", "Genesis", ""]

    def test_blank_candidates_never_match_an_empty_fix_key(self):
        fixed, _ = sl._resolve_search_result_fixed_ref(
            "Genesis 1", "", "", {}, {"": "bogus"})

        assert fixed == "Genesis 1"


class TestIsSearchResultRemoved:
    def test_matching_candidate_title_is_removed(self):
        assert sl._is_search_result_removed("Foo 1", ["Foo 1", "", "Foo"], {"foo"}) is True

    def test_matching_ref_itself_is_removed(self):
        assert sl._is_search_result_removed("Foo 1", ["Other"], {"foo1"}) is True

    def test_no_match_is_kept(self):
        assert sl._is_search_result_removed("Foo 1", ["Other", ""], {"bar"}) is False

    def test_blank_candidates_never_match_an_empty_remove_key(self):
        assert sl._is_search_result_removed("", ["", None], {""}) is False


# ─── _resolve_search_result_authors ────────────────────────────────────────

class TestResolveSearchResultAuthors:
    def test_string_author_is_wrapped_in_a_list(self):
        assert sl._resolve_search_result_authors({"authors": "Rashi"}) == ["Rashi"]

    def test_list_authors_pass_through(self):
        assert sl._resolve_search_result_authors({"authors": ["A", "B"]}) == ["A", "B"]

    @pytest.mark.parametrize("bad", [{"name": "Rashi"}, 7, None])
    def test_other_author_shapes_become_an_empty_list(self, bad):
        assert sl._resolve_search_result_authors({"authors": bad}) == []

    @pytest.mark.parametrize("entry", [{}, None, "Rashi", ["Rashi"]])
    def test_missing_or_non_dict_entry_gives_no_authors(self, entry):
        assert sl._resolve_search_result_authors(entry) == []


# ─── _add_search_library_result ────────────────────────────────────────────

def _run_add(monkeypatch, ref, entries=None, *, seen=None, fix_map=None,
             remove_keys=None, category_filters=None, metadata_filters=None, **kwargs):
    lookups = _stub_index_entries(monkeypatch, entries or {})
    results = []
    seen = set() if seen is None else seen
    sl._add_search_library_result(
        ref, results, seen, {}, fix_map or {}, remove_keys or set(),
        category_filters or [], metadata_filters, **kwargs)
    return results, seen, lookups


_GENESIS_ENTRY = {
    "categories": ["Tanakh", "Torah"],
    "authors": "Moses",
    "compDateString": "1300 BCE",
    "compPlaceString": "Sinai",
}


class TestAddSearchLibraryResult:
    @pytest.mark.parametrize("ref", ["", "   ", None])
    def test_blank_ref_adds_nothing_and_does_no_lookup(self, monkeypatch, ref):
        results, seen, lookups = _run_add(monkeypatch, ref)

        assert results == []
        assert seen == set()
        assert lookups == []

    def test_successful_result_carries_all_metadata(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY},
            label="Genesis", explicit_he_ref="  בראשית א  ")

        assert results == [{
            "ref": "Genesis 1", "heRef": "בראשית א", "text": "Genesis",
            "categories": ["Tanakh", "Torah"], "path": "Genesis",
            "authors": ["Moses"], "era": "1300 BCE", "geography": "Sinai",
            "nusach": "",
        }]
        assert seen == {"Genesis 1"}

    def test_text_and_path_fall_back_to_the_ref_and_explicit_categories_win(self, monkeypatch):
        results, _, _ = _run_add(
            monkeypatch, "Siddur Ashkenaz, Weekday", {"Siddur Ashkenaz": _GENESIS_ENTRY},
            explicit_categories=["Liturgy", "", None])

        assert len(results) == 1
        assert results[0]["text"] == "Siddur Ashkenaz, Weekday"
        assert results[0]["path"] == "Siddur Ashkenaz, Weekday"
        assert results[0]["categories"] == ["Liturgy"]
        assert results[0]["nusach"] == "Ashkenaz"

    def test_removed_ref_is_dropped_and_not_marked_seen(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Removed Work, Ch 1", {"Removed Work": _GENESIS_ENTRY},
            remove_keys={sl._normalize_title_key("Removed Work")})

        assert results == []
        assert seen == set()

    def test_removal_is_checked_against_the_fixed_ref(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Old Work", {},
            fix_map={sl._normalize_title_key("Old Work"): "Doomed Work 1"},
            remove_keys={sl._normalize_title_key("Doomed Work 1")})

        assert results == []
        assert seen == set()

    def test_already_seen_ref_is_not_added_again(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY}, seen={"Genesis 1"})

        assert results == []
        assert seen == {"Genesis 1"}

    def test_seen_check_uses_the_fix_mapped_ref(self, monkeypatch):
        fix_map = {sl._normalize_title_key("Old Work"): "Genesis 1"}

        dup, _, _ = _run_add(
            monkeypatch, "Old Work", {"Genesis 1": _GENESIS_ENTRY},
            fix_map=fix_map, seen={"Genesis 1"})
        fresh, seen, _ = _run_add(
            monkeypatch, "Old Work", {"Genesis 1": _GENESIS_ENTRY}, fix_map=fix_map)

        assert dup == []
        assert [r["ref"] for r in fresh] == ["Genesis 1"]
        assert seen == {"Genesis 1"}

    def test_category_filter_mismatch_drops_the_result(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY},
            category_filters=["talmud"])

        assert results == []
        assert seen == set()

    def test_category_filter_match_keeps_the_result(self, monkeypatch):
        results, _, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY},
            category_filters=["talmud", "torah"])

        assert [r["ref"] for r in results] == ["Genesis 1"]

    def test_metadata_filter_mismatch_drops_the_result_and_leaves_it_unseen(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY},
            metadata_filters={"era": "Rishonim"})

        assert results == []
        assert seen == set()

    def test_metadata_filter_match_keeps_the_result(self, monkeypatch):
        results, seen, _ = _run_add(
            monkeypatch, "Genesis 1", {"Genesis 1": _GENESIS_ENTRY},
            metadata_filters={"era": "bce"})

        assert [r["ref"] for r in results] == ["Genesis 1"]
        assert seen == {"Genesis 1"}


# ─── _add_direct_name_match ────────────────────────────────────────────────

def _recorder():
    calls = []

    def add(*args, **kwargs):
        calls.append((args, kwargs))

    return calls, add


class TestAddDirectNameMatch:
    def test_book_match_is_resolved_to_its_opening_ref(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda title: f"{title} 1")
        calls, add = _recorder()

        sl._add_direct_name_match(
            {"is_ref": True, "is_book": True, "ref": "Genesis", "book": "Genesis"}, add)

        assert calls == [(("Genesis 1", "Genesis"), {})]

    def test_non_book_ref_is_added_as_is_without_resolution(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title",
                            lambda title: pytest.fail("must not resolve a non-book"))
        calls, add = _recorder()

        sl._add_direct_name_match({"is_ref": True, "ref": "Genesis 1:1"}, add)

        assert calls == [(("Genesis 1:1", ""), {})]

    @pytest.mark.parametrize("name_data", [
        {"is_ref": False, "ref": "Genesis 1"},
        {"is_ref": True, "ref": ""},
        {"is_ref": True},
        {},
    ])
    def test_non_ref_or_missing_ref_adds_nothing(self, name_data):
        calls, add = _recorder()

        sl._add_direct_name_match(name_data, add)

        assert calls == []


# ─── _add_name_search_matches ──────────────────────────────────────────────

class TestAddNameSearchMatches:
    @pytest.mark.parametrize("name_data", [None, "Genesis", [], 3])
    def test_non_dict_payload_adds_nothing_and_does_not_stop(self, name_data):
        calls, add = _recorder()

        assert sl._add_name_search_matches(name_data, add, [], 5) is False
        assert calls == []

    def test_completion_objects_of_other_types_are_skipped(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda t: t)
        calls, add = _recorder()
        name_data = {"completion_objects": [
            {"type": "ToC", "key": "Tanakh", "title": "Tanakh"},
            {"type": "ref", "key": "Genesis 1", "title": "Genesis 1"},
        ]}

        assert sl._add_name_search_matches(name_data, add, [], 5) is False
        assert calls == [(("Genesis 1", "Genesis 1"), {})]

    def test_book_completion_is_resolved_to_its_opening_ref(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda t: f"{t} 1")
        calls, add = _recorder()
        name_data = {"completion_objects": [
            {"type": "ref", "key": "Genesis", "title": "Genesis (Bereshit)", "is_book": True},
            {"type": "ref", "title": "Exodus 2:1"},
        ]}

        sl._add_name_search_matches(name_data, add, [], 5)

        assert calls == [
            (("Genesis 1", "Genesis (Bereshit)"), {}),
            (("Exodus 2:1", "Exodus 2:1"), {}),
        ]

    def test_reaching_size_stops_and_reports_it(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda t: t)
        results = []
        added = []

        def add(ref, label=""):
            added.append(ref)
            results.append(ref)

        name_data = {"completion_objects": [
            {"type": "ref", "key": "A", "title": "A"},
            {"type": "ref", "key": "B", "title": "B"},
            {"type": "ref", "key": "C", "title": "C"},
        ]}

        assert sl._add_name_search_matches(name_data, add, results, 2) is True
        assert added == ["A", "B"]

    def test_direct_match_alone_does_not_stop_the_search(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda t: t)
        results = []
        name_data = {"is_ref": True, "ref": "Genesis 1", "book": "Genesis",
                     "completion_objects": None}

        assert sl._add_name_search_matches(
            name_data, lambda ref, label="": results.append(ref), results, 1) is False
        assert results == ["Genesis 1"]


# ─── _add_catalog_search_matches ───────────────────────────────────────────

class TestAddCatalogSearchMatches:
    ROWS = [
        {"ref": "A 1", "text": "A", "categories": ["Cat"], "heRef": "א"},
        {"ref": "B 1", "text": "B", "categories": [], "heRef": "ב"},
        {"ref": "C 1", "text": "C", "categories": [], "heRef": "ג"},
    ]

    def _wire(self, monkeypatch, rows):
        searches = []

        def fake_search(query, size=10, metadata_filters=None):
            searches.append((query, size, metadata_filters))
            return rows

        monkeypatch.setattr(sl, "_search_index_catalog", fake_search)
        return searches

    def test_stops_adding_once_size_results_exist(self, monkeypatch):
        searches = self._wire(monkeypatch, self.ROWS)
        results = []
        calls = []

        def add(ref, label, explicit_categories=None, explicit_he_ref=""):
            calls.append((ref, label, explicit_categories, explicit_he_ref))
            results.append(ref)

        sl._add_catalog_search_matches("torah", add, results, 2, {"era": "x"})

        assert calls == [("A 1", "A", ["Cat"], "א"), ("B 1", "B", [], "ב")]
        assert searches == [("torah", 16, {"era": "x"})]

    def test_search_size_scales_with_size_above_the_floor(self, monkeypatch):
        searches = self._wire(monkeypatch, [])

        sl._add_catalog_search_matches("q", lambda *a, **k: None, [], 10, None)

        assert searches == [("q", 20, None)]

    def test_rows_missing_keys_are_added_with_blank_defaults(self, monkeypatch):
        self._wire(monkeypatch, [{}])
        calls = []

        def add(ref, label, explicit_categories=None, explicit_he_ref="x"):
            calls.append((ref, label, explicit_categories, explicit_he_ref))

        sl._add_catalog_search_matches("q", add, [], 5, None)

        assert calls == [("", "", [], "")]


# ─── _resolve_tanakh_chapter_verse ─────────────────────────────────────────

class TestResolveTanakhChapterVerse:
    def test_chapter_and_verse_are_split_off(self):
        assert sl._resolve_tanakh_chapter_verse("Song of Songs 2:3") == (
            "Song of Songs", "2", "3")

    def test_verse_defaults_to_one_when_only_a_chapter_is_given(self):
        assert sl._resolve_tanakh_chapter_verse("Genesis 3") == ("Genesis", "3", "1")

    def test_embedded_newline_is_rejected_even_when_the_tail_would_parse(self):
        assert sl._resolve_tanakh_chapter_verse("Genesis\n1:2") is None
        assert sl._resolve_tanakh_chapter_verse("Gen\nesis 1:2") is None

    @pytest.mark.parametrize("ref", ["Genesis abc", "Genesis 1:2:3", "Genesis 1a", "Berakhot 2a"])
    def test_non_numeric_tail_is_rejected(self, ref):
        assert sl._resolve_tanakh_chapter_verse(ref) is None

    @pytest.mark.parametrize("ref", ["Genesis", "", None])
    def test_a_ref_without_a_trailing_token_is_rejected(self, ref):
        assert sl._resolve_tanakh_chapter_verse(ref) is None


# ─── _walk_liturgy_books ───────────────────────────────────────────────────

class TestWalkLiturgyBooksNonDictNodes:
    def test_non_container_nodes_are_ignored(self):
        books, seen = [], set()

        sl._walk_liturgy_books(
            ["junk", None, 5, {"title": "Siddur", "categories": ["Liturgy"]}],
            books, seen, False)

        assert books == [{"name": "Siddur", "title": "Siddur", "categories": ["Liturgy"]}]
        assert seen == {"Siddur"}

    def test_a_bare_non_container_root_adds_nothing(self):
        books, seen = [], set()

        sl._walk_liturgy_books("Siddur", books, seen, False)
        sl._walk_liturgy_books(None, books, seen, False)

        assert books == []
        assert seen == set()

    def test_non_dict_contents_of_a_node_are_ignored(self):
        books, seen = [], set()
        node = {"title": "Liturgy Root", "categories": ["Liturgy"], "contents": "oops"}

        sl._walk_liturgy_books(node, books, seen, False)

        assert [b["title"] for b in books] == ["Liturgy Root"]


# ─── _walk_index_schema_for_leaf_refs / get_index_leaf_refs ────────────────

class TestWalkIndexSchemaForLeafRefs:
    def test_a_non_dict_node_adds_nothing(self):
        refs, seen = [], set()

        sl._walk_index_schema_for_leaf_refs("junk", [], "Siddur", "siddur", seen, refs, 5)

        assert refs == []
        assert seen == set()

    def test_full_refs_list_stops_the_walk_immediately(self):
        refs, seen = ["Existing"], {"Existing"}

        sl._walk_index_schema_for_leaf_refs(
            {"title": "Leaf"}, [], "Siddur", "siddur", seen, refs, 1)

        assert refs == ["Existing"]
        assert seen == {"Existing"}

    def test_remaining_children_are_skipped_once_max_refs_is_reached(self):
        refs, seen = [], set()
        node = {"title": "Siddur", "nodes": [
            {"title": "Alpha"}, {"title": "Beta"}, {"title": "Gamma"}]}

        sl._walk_index_schema_for_leaf_refs(node, [], "Siddur", "siddur", seen, refs, 2)

        assert refs == ["Siddur, Alpha", "Siddur, Beta"]

    def test_non_dict_children_are_skipped_among_real_ones(self):
        refs, seen = [], set()
        node = {"title": "Siddur", "nodes": ["junk", None, {"title": "Alpha"}]}

        sl._walk_index_schema_for_leaf_refs(node, [], "Siddur", "siddur", seen, refs, 5)

        assert refs == ["Siddur, Alpha"]


class TestGetIndexLeafRefs:
    def test_leaf_refs_follow_the_schema_path(self, monkeypatch):
        schema = {"title": "Siddur", "nodes": [
            {"title": "Shacharit", "nodes": [{"title": "Blessings"}, {"title": "Pesukei"}]},
            {"title": "Minchah"},
        ]}
        _stub_index_entries(monkeypatch, {"Siddur": {"schema": schema}})

        assert sl.get_index_leaf_refs("Siddur") == [
            "Siddur, Shacharit, Blessings",
            "Siddur, Shacharit, Pesukei",
            "Siddur, Minchah",
        ]

    def test_max_refs_caps_the_result(self, monkeypatch):
        schema = {"title": "Siddur", "nodes": [
            {"title": "Alpha"}, {"title": "Beta"}, {"title": "Gamma"}]}
        _stub_index_entries(monkeypatch, {"Siddur": {"schema": schema}})

        assert sl.get_index_leaf_refs("Siddur", max_refs=2) == [
            "Siddur, Alpha", "Siddur, Beta"]

    def test_a_schema_that_yields_no_leaves_falls_back_to_the_title(self, monkeypatch):
        # A truthy schema that is not a node dict is walked to nothing.
        _stub_index_entries(monkeypatch, {"Weird Title": {"schema": ["junk"]}})

        assert sl.get_index_leaf_refs("Weird Title") == ["Weird Title"]

    def test_missing_schema_returns_an_empty_list(self, monkeypatch):
        _stub_index_entries(monkeypatch, {"Empty": {}})
        monkeypatch.setattr(sl, "_lookup_canonical_index_title", lambda title: "")

        assert sl.get_index_leaf_refs("Empty") == []


class TestTrySchemaFallbackTitle:
    """`_try_schema_fallback_title` is the shared step of the index-schema title
    fallbacks. Its success branch was only ever reached incidentally, through
    leftover on-disk cache files from an earlier run, so it is pinned here with
    a stubbed `get_index_entry`."""

    def _stub(self, monkeypatch, entries):
        looked_up = []

        def fake_get_index_entry(title):
            looked_up.append(title)
            return entries.get(title)

        monkeypatch.setattr(sl, "get_index_entry", fake_get_index_entry)
        return looked_up

    def test_candidate_with_a_schema_replaces_the_title(self, monkeypatch):
        looked_up = self._stub(monkeypatch, {"Canonical": {"schema": {"key": "default"}}})

        assert sl._try_schema_fallback_title("Alias", "Canonical") == ({"key": "default"}, "Canonical")
        assert looked_up == ["Canonical"]

    @pytest.mark.parametrize("candidate", ["", None])
    def test_blank_candidate_keeps_the_title_without_a_lookup(self, monkeypatch, candidate):
        looked_up = self._stub(monkeypatch, {})

        assert sl._try_schema_fallback_title("Alias", candidate) == ({}, "Alias")
        assert looked_up == []

    def test_candidate_equal_to_the_current_title_is_not_looked_up_again(self, monkeypatch):
        looked_up = self._stub(monkeypatch, {"Alias": {"schema": {"key": "default"}}})

        assert sl._try_schema_fallback_title("Alias", "Alias") == ({}, "Alias")
        assert looked_up == []

    @pytest.mark.parametrize("entry", [None, {}, {"schema": {}}, ["not", "a", "dict"]])
    def test_candidate_without_a_usable_schema_keeps_the_title(self, monkeypatch, entry):
        looked_up = self._stub(monkeypatch, {"Canonical": entry})

        assert sl._try_schema_fallback_title("Alias", "Canonical") == ({}, "Alias")
        assert looked_up == ["Canonical"]


class TestCachedGetDiskHit:
    """The disk tier of `_cached_get` (memory -> disk -> network), exercised
    through the real on-disk layer in the per-test temp dir that conftest
    provides, with no leftover files needed."""

    URL = "https://www.sefaria.org/api/texts/Genesis.1.1"

    @pytest.fixture(autouse=True)
    def _clean_memory_cache(self):
        sl._cache.clear()
        yield
        sl._cache.clear()

    @staticmethod
    def _network_must_not_be_used(monkeypatch):
        def boom(*args, **kwargs):
            raise AssertionError("network used although the disk cache held the entry")

        monkeypatch.setattr(sl._http_session, "get", boom)

    def test_a_disk_entry_is_returned_without_touching_the_network(self, monkeypatch):
        payload = {"ref": "Genesis 1:1", "text": ["In the beginning"]}
        sl._disk_cache_set(self.URL, payload)
        self._network_must_not_be_used(monkeypatch)

        assert sl._cached_get(self.URL) == payload

    def test_a_disk_hit_is_promoted_to_the_memory_cache(self, monkeypatch):
        payload = {"ref": "Genesis 1:1"}
        sl._disk_cache_set(self.URL, payload)
        self._network_must_not_be_used(monkeypatch)
        assert sl._cache.get(self.URL) is None

        sl._cached_get(self.URL)

        assert sl._cache.get(self.URL) == payload
        sl._disk_cache_path(self.URL).unlink()
        assert sl._cached_get(self.URL) == payload  # served from memory now

    def test_an_expired_disk_entry_is_not_used(self, monkeypatch):
        sl._disk_cache_set(self.URL, {"stale": True})
        monkeypatch.setattr(sl, "DISK_CACHE_TTL", -1)
        fetched = []

        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"fresh": True}

        monkeypatch.setattr(sl._http_session, "get", lambda url, timeout: fetched.append(url) or _Response())

        assert sl._cached_get(self.URL) == {"fresh": True}
        assert fetched == [self.URL]
