"""
Behavioural tests for the previously-uncovered corners of the first half of
backend/sefaria_library.py:

  * the on-disk response cache (_disk_cache_get / _disk_cache_set)
  * _parse_library_adjustments_payload / _load_library_index_adjustments
    failure handling
  * _cached_get's catch-all error branch
  * _matches_metadata_filters empty-filter handling
  * _resolve_ref_candidates ignoring blank candidates
  * _flatten_index_titles ignoring non-container nodes
  * catalog scoring (_score_catalog_title_match / _apply_catalog_row_score_boosts
    / _score_catalog_row) and _build_catalog_search_result's skip paths

Every test isolates the module-level state it touches through monkeypatch so
it cannot leak into (or depend on) any other test module.
"""

from __future__ import annotations

import json
import os
import time

import pytest

import backend.sefaria_library as sl


# ─────────────────────────── shared fixtures ───────────────────────────

@pytest.fixture
def disk_dir(tmp_path, monkeypatch):
    """Point the real disk cache at a throw-away directory."""
    target = tmp_path / "sefaria_cache"
    monkeypatch.setattr(sl, "_DISK_CACHE_DIR", target)
    return target


@pytest.fixture
def isolated_network_cache(monkeypatch):
    """Run _cached_get with no memory/disk hits and no disk writes, and a
    pristine block-status record that is restored afterwards."""
    saved_status = dict(sl._sefaria_block_status)
    sl._cache.clear()
    monkeypatch.setattr(sl, "_disk_cache_get", lambda url: None)
    monkeypatch.setattr(sl, "_disk_cache_set", lambda url, data: None)
    sl._sefaria_block_status.update({
        "is_blocked": False, "http_status": None, "block_reason": "",
        "last_blocked_ts": 0.0, "consecutive_blocks": 0,
    })
    yield
    sl._cache.clear()
    sl._sefaria_block_status.clear()
    sl._sefaria_block_status.update(saved_status)


@pytest.fixture
def adjustments_report(tmp_path, monkeypatch):
    """A real report file on disk plus a pristine adjustments cache."""
    report = tmp_path / "library_leaf_remove_fix_report.full.json"
    monkeypatch.setattr(sl, "_LIBRARY_REPORT_PATH", report)
    monkeypatch.setattr(sl, "_library_index_adjustments_cache", {
        "loaded": False, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    return report


# ─────────────────────────── _disk_cache_get ───────────────────────────

class TestDiskCacheGet:
    URL = "https://mock.sefaria.org/api/index/Genesis"

    def _write(self, payload):
        sl._DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        sl._disk_cache_path(self.URL).write_text(
            json.dumps(payload), encoding="utf-8")

    def test_missing_file_is_a_miss(self, disk_dir):
        assert sl._disk_cache_get(self.URL) is None

    def test_fresh_entry_returns_its_data(self, disk_dir):
        self._write({"ts": time.time(), "data": {"title": "Genesis"}})
        assert sl._disk_cache_get(self.URL) == {"title": "Genesis"}

    def test_entry_older_than_the_ttl_is_a_miss(self, disk_dir):
        self._write({"ts": time.time() - sl.DISK_CACHE_TTL - 60,
                     "data": {"title": "Genesis"}})
        assert sl._disk_cache_get(self.URL) is None

    def test_entry_just_inside_the_ttl_is_still_served(self, disk_dir):
        self._write({"ts": time.time() - sl.DISK_CACHE_TTL + 600,
                     "data": {"title": "Genesis"}})
        assert sl._disk_cache_get(self.URL) == {"title": "Genesis"}

    def test_entry_without_a_timestamp_counts_as_stale(self, disk_dir):
        self._write({"data": {"title": "Genesis"}})
        assert sl._disk_cache_get(self.URL) is None

    def test_fresh_entry_without_data_yields_none(self, disk_dir):
        self._write({"ts": time.time()})
        assert sl._disk_cache_get(self.URL) is None

    @pytest.mark.parametrize("raw", [b"{not valid json", b"\xff\xfe\x00garbage", b""])
    def test_unreadable_or_corrupt_file_is_swallowed_as_a_miss(self, disk_dir, raw):
        disk_dir.mkdir(parents=True)
        sl._disk_cache_path(self.URL).write_bytes(raw)
        assert sl._disk_cache_get(self.URL) is None

    def test_non_numeric_timestamp_is_swallowed_as_a_miss(self, disk_dir):
        self._write({"ts": "yesterday", "data": {"title": "Genesis"}})
        assert sl._disk_cache_get(self.URL) is None


# ─────────────────────────── _disk_cache_set ───────────────────────────

class TestDiskCacheSet:
    URL = "https://mock.sefaria.org/api/texts/Genesis.1"

    def test_creates_the_cache_directory_and_writes_ts_and_data(self, disk_dir):
        assert not disk_dir.exists()
        before = time.time()

        sl._disk_cache_set(self.URL, {"he": ["בראשית"], "n": 1})

        written = json.loads(sl._disk_cache_path(self.URL).read_text(encoding="utf-8"))
        assert set(written) == {"ts", "data"}
        assert written["data"] == {"he": ["בראשית"], "n": 1}
        assert before <= written["ts"] <= time.time()

    def test_hebrew_is_stored_unescaped(self, disk_dir):
        sl._disk_cache_set(self.URL, {"he": "בראשית"})
        raw = sl._disk_cache_path(self.URL).read_text(encoding="utf-8")
        assert "בראשית" in raw
        assert "\\u05" not in raw

    def test_round_trips_through_disk_cache_get(self, disk_dir):
        sl._disk_cache_set(self.URL, {"versions": [1, 2, 3]})
        assert sl._disk_cache_get(self.URL) == {"versions": [1, 2, 3]}

    def test_overwrites_an_earlier_entry_for_the_same_url(self, disk_dir):
        sl._disk_cache_set(self.URL, {"v": 1})
        sl._disk_cache_set(self.URL, {"v": 2})
        assert sl._disk_cache_get(self.URL) == {"v": 2}

    def test_different_urls_use_different_files(self, disk_dir):
        sl._disk_cache_set(self.URL, {"v": "a"})
        sl._disk_cache_set(self.URL + "?x=1", {"v": "b"})
        assert sl._disk_cache_get(self.URL) == {"v": "a"}
        assert sl._disk_cache_get(self.URL + "?x=1") == {"v": "b"}

    def test_unwritable_location_is_swallowed(self, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory", encoding="utf-8")
        monkeypatch.setattr(sl, "_DISK_CACHE_DIR", blocker / "cache")

        assert sl._disk_cache_set(self.URL, {"v": 1}) is None

        assert blocker.read_text(encoding="utf-8") == "i am a file, not a directory"
        assert sl._disk_cache_get(self.URL) is None

    def test_unserialisable_data_is_swallowed_and_leaves_no_file(self, disk_dir):
        assert sl._disk_cache_set(self.URL, {"bad": object()}) is None
        assert not sl._disk_cache_path(self.URL).exists()


# ─────────────────────── adjustments payload parsing ───────────────────────

class TestParseLibraryAdjustmentsPayload:
    def test_fix_rows_without_a_suggested_ref_are_skipped(self):
        payload = {"fixes": [
            {"title": "Blank", "suggested_ref": ""},
            {"title": "Missing"},
            {"title": "Nothing", "suggested_ref": None},
            {"title": "Spaces", "suggested_ref": "   "},
            {"title": "Real Book", "name_ref": "Real Name", "suggested_ref": "  Real Book 1  "},
        ]}

        remove_keys, fix_map = sl._parse_library_adjustments_payload(payload)

        assert remove_keys == set()
        assert fix_map == {"realbook": "Real Book 1", "realname": "Real Book 1"}
        for skipped in ("blank", "missing", "nothing", "spaces"):
            assert skipped not in fix_map

    def test_removals_and_fixes_are_kept_apart(self):
        payload = {
            "removals": [{"title": "Kinnot (Ashkenaz)", "initial_ref": "Kinnot 1"}],
            "fixes": [{"title": "Fixed", "suggested_ref": "Fixed 1"}],
        }

        remove_keys, fix_map = sl._parse_library_adjustments_payload(payload)

        assert remove_keys == {"kinnotashkenaz", "kinnot1"}
        assert fix_map == {"fixed": "Fixed 1"}

    def test_null_sections_are_tolerated(self):
        assert sl._parse_library_adjustments_payload(
            {"removals": None, "fixes": None}) == (set(), {})


# ───────────────── _load_library_index_adjustments failures ─────────────────

class TestLoadAdjustmentsUnreadableReport:
    @pytest.mark.parametrize("raw", [b"{ this is not json", b"\xff\xfe\x00garbage"])
    def test_unreadable_report_yields_empty_loaded_state_and_logs(
            self, adjustments_report, monkeypatch, raw):
        adjustments_report.write_bytes(raw)
        logged = []
        monkeypatch.setattr(sl.logger, "exception", lambda msg, *a, **k: logged.append(msg))

        state = sl._load_library_index_adjustments()

        assert state["loaded"] is True
        assert state["remove_keys"] == set()
        assert state["fix_map"] == {}
        assert state["mtime"] == adjustments_report.stat().st_mtime
        assert "absent" not in state
        assert len(logged) == 1
        assert "Failed loading index adjustments" in logged[0]

    def test_failed_state_is_published_to_the_module_cache(self, adjustments_report):
        adjustments_report.write_text("not json", encoding="utf-8")

        state = sl._load_library_index_adjustments()

        assert sl._library_index_adjustments_cache is state

    def test_corrupt_report_is_not_reparsed_until_its_mtime_advances(
            self, adjustments_report):
        adjustments_report.write_text("not json", encoding="utf-8")
        first = sl._load_library_index_adjustments()
        stamp = adjustments_report.stat().st_mtime

        # Same mtime, now-valid content: the cached failure must still win.
        adjustments_report.write_text(json.dumps(
            {"fixes": [{"title": "Fixed", "suggested_ref": "Fixed 1"}]}), encoding="utf-8")
        os.utime(adjustments_report, (stamp, stamp))
        assert sl._load_library_index_adjustments() is first
        assert first["fix_map"] == {}

        # A newer mtime forces a fresh, successful parse.
        os.utime(adjustments_report, (stamp + 100, stamp + 100))
        refreshed = sl._load_library_index_adjustments()
        assert refreshed is not first
        assert refreshed["fix_map"] == {"fixed": "Fixed 1"}
        assert refreshed["mtime"] == stamp + 100


# ───────────────────────── _cached_get catch-all branch ─────────────────────────

class _OkResponse:
    def __init__(self, payload=None, json_exc=None):
        self._payload = payload
        self._json_exc = json_exc

    def raise_for_status(self):
        return None

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


class TestCachedGetUnexpectedError:
    URL = "https://mock.sefaria.org/api/index/Explode"

    @pytest.mark.parametrize("exc", [RuntimeError("kaboom"), KeyError("k")])
    def test_non_requests_exception_from_session_returns_none_and_logs(
            self, isolated_network_cache, monkeypatch, exc):
        def boom(url, timeout=None):
            raise exc

        logged = []
        monkeypatch.setattr(sl._http_session, "get", boom)
        monkeypatch.setattr(sl.logger, "exception", lambda msg, *a, **k: logged.append(msg))

        assert sl._cached_get(self.URL) is None

        assert len(logged) == 1
        assert "Unexpected error occurred" in logged[0]
        assert self.URL in logged[0]
        assert type(exc).__name__ in logged[0]
        assert sl._cache.get(self.URL) is None

    def test_json_decoding_failure_is_not_cached_and_does_not_flag_a_block(
            self, isolated_network_cache, monkeypatch):
        monkeypatch.setattr(
            sl._http_session, "get",
            lambda url, timeout=None: _OkResponse(json_exc=ValueError("bad body")))
        monkeypatch.setattr(sl.logger, "exception", lambda *a, **k: None)

        assert sl._cached_get(self.URL) is None

        assert sl._cache.get(self.URL) is None
        assert sl._sefaria_block_status["is_blocked"] is False
        assert sl._sefaria_block_status["consecutive_blocks"] == 0

    def test_failure_is_not_sticky_the_next_call_retries_the_network(
            self, isolated_network_cache, monkeypatch):
        calls = []

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) == 1:
                raise RuntimeError("first call dies")
            return _OkResponse({"title": "Explode"})

        monkeypatch.setattr(sl._http_session, "get", flaky)
        monkeypatch.setattr(sl.logger, "exception", lambda *a, **k: None)

        assert sl._cached_get(self.URL) is None
        assert sl._cached_get(self.URL) == {"title": "Explode"}
        assert calls == [self.URL, self.URL]


# ───────────────────── _matches_metadata_filters empty needles ─────────────────────

class TestMetadataFiltersEmptyNeedles:
    RESULT = {"era": "", "authors": [], "categories": ["Tanakh"], "path": "Genesis",
              "geography": "", "ref": "Genesis 1", "nusach": ""}

    @pytest.mark.parametrize("empty", ["", "   ", " , ,", [], [" ", ""], None])
    def test_a_filter_with_no_usable_values_is_ignored(self, empty):
        # 'author' has no haystack at all: a real value would reject the row,
        # an empty one must be skipped rather than treated as "no match".
        assert sl._matches_metadata_filters(self.RESULT, {"author": empty}) is True

    def test_real_value_on_the_same_missing_field_rejects(self):
        assert sl._matches_metadata_filters(self.RESULT, {"author": "rashi"}) is False

    def test_empty_filter_is_skipped_but_later_filters_still_apply(self):
        assert sl._matches_metadata_filters(
            self.RESULT, {"era": "", "category": "tanakh"}) is True
        assert sl._matches_metadata_filters(
            self.RESULT, {"era": "", "category": "talmud"}) is False


# ─────────────── _resolve_ref_candidates ignores blank candidates ───────────────

class TestResolveRefCandidatesBlankCompletions:
    def test_blank_completion_entries_are_not_added(self, monkeypatch):
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=None: {
            "completion_objects": [
                {"type": "ref"},
                {"type": "ref", "key": "   "},
                {"type": "ref", "key": None, "title": ""},
                {"type": "ref", "key": "Genesis 2"},
                {"type": "ref", "title": "Genesis 3"},
            ],
        })
        monkeypatch.setattr(sl, "get_index_entry", lambda title: None)

        result = sl._resolve_ref_candidates("Genesis")

        assert result == ["Genesis", "Genesis 2", "Genesis 3"]

    def test_blank_leaf_refs_from_the_index_are_dropped(self, monkeypatch):
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=None: None)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis"})
        monkeypatch.setattr(
            sl, "get_index_leaf_refs",
            lambda canonical, max_refs=None: ["", None, "  ", "Genesis 1:1"])

        result = sl._resolve_ref_candidates("Genesis")

        assert result == ["Genesis", "Genesis 1:1"]


# ─────────────────────── _flatten_index_titles non-containers ───────────────────────

class TestFlattenIndexTitlesNonContainers:
    @pytest.mark.parametrize("node", ["a stray string", None, 42, 3.5, True])
    def test_scalar_nodes_add_nothing(self, node):
        rows, seen = [], set()

        assert sl._flatten_index_titles(node, rows, seen) is None

        assert rows == []
        assert seen == set()

    def test_scalars_nested_in_lists_and_children_are_skipped_around_real_rows(self):
        tree = [
            "junk",
            {
                "title": "Tanakh",
                "categories": ["Tanakh"],
                "contents": ["stray", None, 7, {"title": "Genesis", "categories": ["Tanakh"]}],
            },
            {"children": ["also junk", {"title": "Exodus", "categories": []}]},
        ]
        rows, seen = [], set()

        sl._flatten_index_titles(tree, rows, seen)

        assert [row["title"] for row in rows] == ["Tanakh", "Genesis", "Exodus"]
        assert seen == {"tanakh", "genesis", "exodus"}


# ─────────────────────────── catalog scoring ───────────────────────────

class TestScoreCatalogTitleMatch:
    def test_exact_match_scores_120(self):
        assert sl._score_catalog_title_match("genesis", "genesis") == 120

    def test_prefix_match_scores_100(self):
        assert sl._score_catalog_title_match("genesis rabbah", "genesis") == 100

    def test_substring_match_scores_85(self):
        assert sl._score_catalog_title_match("midrash genesis rabbah", "genesis") == 85

    def test_match_only_via_metadata_scores_60(self):
        assert sl._score_catalog_title_match("zohar", "kabbalah") == 60

    def test_exact_outranks_prefix_outranks_substring_outranks_other(self):
        scores = [
            sl._score_catalog_title_match("torah", "torah"),
            sl._score_catalog_title_match("torah ohr", "torah"),
            sl._score_catalog_title_match("ohr torah", "torah"),
            sl._score_catalog_title_match("ohr", "torah"),
        ]
        assert scores == sorted(scores, reverse=True)
        assert len(set(scores)) == 4


class TestApplyCatalogRowScoreBoosts:
    SACKS_ROW = {"categories": ["Modern Commentary", "Jonathan Sacks"]}

    def test_sacks_query_on_a_sacks_category_row_gets_25(self):
        assert sl._apply_catalog_row_score_boosts(
            self.SACKS_ROW, ["jonathan", "sacks"], "covenant & conversation", 60) == 85

    def test_sacks_category_match_is_case_insensitive(self):
        row = {"categories": ["JONATHAN SACKS"]}
        assert sl._apply_catalog_row_score_boosts(
            row, ["sacks", "jonathan"], "x", 10) == 35

    def test_sacks_query_on_a_row_outside_the_sacks_category_gets_nothing(self):
        row = {"categories": ["Talmud"]}
        assert sl._apply_catalog_row_score_boosts(
            row, ["jonathan", "sacks"], "x", 60) == 60

    def test_sacks_query_on_a_row_with_no_categories_gets_nothing(self):
        assert sl._apply_catalog_row_score_boosts({}, ["jonathan", "sacks"], "x", 60) == 60

    @pytest.mark.parametrize("tokens", [["jonathan"], ["sacks"], ["rabbi", "sacks"]])
    def test_both_name_tokens_are_required_for_the_sacks_boost(self, tokens):
        assert sl._apply_catalog_row_score_boosts(
            self.SACKS_ROW, tokens, "x", 60) == 60

    def test_essay_token_and_essay_title_gets_15(self):
        assert sl._apply_catalog_row_score_boosts(
            {"categories": []}, ["essay"], "an essay on faith", 60) == 75

    def test_essay_token_without_essay_in_the_title_gets_nothing(self):
        assert sl._apply_catalog_row_score_boosts(
            {"categories": ["Essays"]}, ["essay"], "on faith", 60) == 60

    def test_essay_in_the_title_without_the_token_gets_nothing(self):
        assert sl._apply_catalog_row_score_boosts(
            {"categories": []}, ["faith"], "an essay on faith", 60) == 60

    def test_both_boosts_stack(self):
        assert sl._apply_catalog_row_score_boosts(
            self.SACKS_ROW, ["jonathan", "sacks", "essay"], "sacks essay", 100) == 140


class TestScoreCatalogRow:
    def test_row_missing_any_token_from_its_haystack_does_not_match(self):
        row = {"title": "Genesis", "search": "genesis tanakh"}
        assert sl._score_catalog_row(row, ["genesis", "rashi"], "genesis rashi") is None

    def test_row_without_a_search_field_does_not_match(self):
        assert sl._score_catalog_row({"title": "Genesis"}, ["genesis"], "genesis") is None

    def test_exact_title_match_scores_120(self):
        row = {"title": "Genesis", "search": "genesis", "categories": []}
        assert sl._score_catalog_row(row, ["genesis"], "genesis") == 120

    def test_substring_title_match_scores_85(self):
        row = {"title": "Midrash Genesis Rabbah", "search": "midrash genesis rabbah",
               "categories": []}
        assert sl._score_catalog_row(row, ["genesis"], "genesis") == 85

    def test_metadata_only_match_scores_60(self):
        row = {"title": "Zohar", "search": "zohar kabbalah", "categories": ["Kabbalah"]}
        assert sl._score_catalog_row(row, ["kabbalah"], "kabbalah") == 60

    def test_boosts_are_added_on_top_of_the_base_score(self):
        row = {"title": "Essay on Faith", "search": "essay on faith", "categories": []}
        assert sl._score_catalog_row(row, ["essay"], "essay") == 100 + 15

    def test_sacks_row_gets_base_plus_category_boost(self):
        row = {"title": "Covenant & Conversation",
               "search": "covenant & conversation jonathan sacks",
               "categories": ["Jonathan Sacks"]}
        assert sl._score_catalog_row(
            row, ["jonathan", "sacks"], "jonathan sacks") == 60 + 25


# ───────────────────── _build_catalog_search_result skip paths ─────────────────────

class TestBuildCatalogSearchResult:
    ROW = {"title": "Siddur Ashkenaz", "heTitle": "סידור אשכנז",
           "categories": ["Liturgy", "Siddur"]}

    def test_title_with_no_opening_ref_is_skipped(self, monkeypatch):
        asked = []
        monkeypatch.setattr(
            sl, "_resolve_opening_ref_for_title", lambda title: asked.append(title) or "")
        seen = set()

        assert sl._build_catalog_search_result(self.ROW, seen, None) is None

        assert asked == ["Siddur Ashkenaz"]
        assert seen == set()

    def test_already_seen_opening_ref_is_skipped(self, monkeypatch):
        monkeypatch.setattr(
            sl, "_resolve_opening_ref_for_title", lambda title: "Siddur Ashkenaz, Weekday Shacharit")
        seen = {"Siddur Ashkenaz, Weekday Shacharit"}

        assert sl._build_catalog_search_result(self.ROW, seen, None) is None

        assert seen == {"Siddur Ashkenaz, Weekday Shacharit"}

    def test_metadata_filter_mismatch_is_skipped_and_ref_not_marked_seen(self, monkeypatch):
        monkeypatch.setattr(
            sl, "_resolve_opening_ref_for_title", lambda title: "Siddur Ashkenaz, Weekday Shacharit")
        seen = set()

        assert sl._build_catalog_search_result(
            self.ROW, seen, {"nusach": "yemenite"}) is None

        assert seen == set()

    def test_matching_row_builds_the_result_and_marks_the_ref_seen(self, monkeypatch):
        monkeypatch.setattr(
            sl, "_resolve_opening_ref_for_title", lambda title: "Siddur Ashkenaz, Weekday Shacharit")
        seen = set()

        result = sl._build_catalog_search_result(
            self.ROW, seen, {"nusach": "ashkenaz", "category": "liturgy"})

        assert result == {
            "ref": "Siddur Ashkenaz, Weekday Shacharit",
            "heRef": "סידור אשכנז",
            "text": "Siddur Ashkenaz",
            "categories": ["Liturgy", "Siddur"],
            "path": "Siddur Ashkenaz",
            "authors": [],
            "era": "",
            "geography": "",
            "nusach": "Ashkenaz",
        }
        assert seen == {"Siddur Ashkenaz, Weekday Shacharit"}

    def test_sparse_row_falls_back_to_empty_fields(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda title: "Zohar 1")
        seen = set()

        result = sl._build_catalog_search_result({"title": "Zohar"}, seen, None)

        assert result["heRef"] == ""
        assert result["categories"] == []
        assert result["nusach"] == ""
        assert result["ref"] == "Zohar 1"
        assert seen == {"Zohar 1"}

    def test_second_row_resolving_to_the_same_ref_is_deduplicated(self, monkeypatch):
        monkeypatch.setattr(sl, "_resolve_opening_ref_for_title", lambda title: "Genesis 1")
        seen = set()

        first = sl._build_catalog_search_result({"title": "Genesis"}, seen, None)
        second = sl._build_catalog_search_result({"title": "Bereshit"}, seen, None)

        assert first is not None
        assert second is None
