"""
Tests for the ref-resolution helpers in backend/sefaria_library.py that turn a
title or loosely-typed ref into refs Sefaria's /texts endpoint can load:
_is_specific_ref_query, _cache_resolved_ref, _compute_opening_ref_from_entry,
_resolve_opening_ref_for_title and the _resolve_ref_candidates helper chain.

All Sefaria access (index entries, leaf refs, /name lookups, the adjustments
report) is stubbed per test, so nothing here touches the network or disk.
"""

from __future__ import annotations

import pytest

import backend.sefaria_library as sl


@pytest.fixture(autouse=True)
def _isolated_sefaria_state(monkeypatch):
    sl._resolved_title_ref_cache.clear()
    monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
        "loaded": True, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    yield
    sl._resolved_title_ref_cache.clear()


def _collector():
    """An `add()` callback like _resolve_ref_candidates' closure, exposing the
    list it appends to (no dedup: the helpers under test add unconditionally)."""
    candidates: list[str] = []
    return candidates, candidates.append


# ─── _is_specific_ref_query: branches the plain-digit regex does not reach ──

class TestIsSpecificRefQueryFallbackBranches:
    def test_colon_followed_by_a_digit_counts_even_without_a_word_boundary(self):
        # "1x" defeats the `\d+[ab]?\b` check ("x" continues the word).
        assert sl._is_specific_ref_query("Foo:1x") is True

    def test_digit_in_the_comma_suffix_counts(self):
        assert sl._is_specific_ref_query("Foo, 3rd part") is True

    def test_digit_only_before_the_comma_does_not_count_as_a_suffix(self):
        assert sl._is_specific_ref_query("Foo1x, Bar") is False

    def test_comma_with_a_suffix_without_digits_is_not_specific(self):
        assert sl._is_specific_ref_query("Shulchan Arukh, Orach Chayim") is False


# ─── _cache_resolved_ref ────────────────────────────────────────────────────

class TestCacheResolvedRef:
    def test_stores_and_returns_the_resolved_ref(self):
        assert sl._cache_resolved_ref("genesis", "Genesis 1") == "Genesis 1"
        assert sl._resolved_title_ref_cache.get("genesis") == "Genesis 1"

    def test_an_empty_key_returns_without_caching(self):
        assert sl._cache_resolved_ref("", "Genesis 1") == "Genesis 1"
        assert sl._resolved_title_ref_cache.get("") is None


# ─── _compute_opening_ref_from_entry ────────────────────────────────────────

class TestComputeOpeningRefFromEntry:
    @pytest.fixture(autouse=True)
    def _no_leaf_refs_unless_a_test_says_otherwise(self, monkeypatch):
        self.leaf_calls = []

        def fake_leaf_refs(title, max_refs=None):
            self.leaf_calls.append((title, max_refs))
            return []

        monkeypatch.setattr(sl, "get_index_leaf_refs", fake_leaf_refs)

    @pytest.mark.parametrize("key", ["firstSectionRef", "firstSection"])
    def test_first_section_wins_and_is_stripped(self, key):
        entry = {"title": "Genesis", key: "  Genesis 1:1  ", "sectionNames": ["Chapter"]}

        assert sl._compute_opening_ref_from_entry(entry, "genesis") == "Genesis 1:1"
        assert self.leaf_calls == []

    def test_blank_first_section_is_ignored(self):
        entry = {"title": "Genesis", "firstSectionRef": "   ", "sectionNames": ["Chapter"]}

        assert sl._compute_opening_ref_from_entry(entry, "genesis") == "Genesis 1"

    def test_a_title_that_is_already_a_specific_ref_is_used_as_is(self):
        entry = {"title": "Berakhot 2a"}

        assert sl._compute_opening_ref_from_entry(entry, "berakhot") == "Berakhot 2a"
        assert self.leaf_calls == []

    def test_first_leaf_ref_is_used_when_the_index_has_one(self, monkeypatch):
        monkeypatch.setattr(
            sl, "get_index_leaf_refs",
            lambda title, max_refs=None: ["Pirkei Avot 1:1", "Pirkei Avot 1:2"],
        )

        assert sl._compute_opening_ref_from_entry({"title": "Pirkei Avot"}, "avot") == "Pirkei Avot 1:1"

    def test_section_names_give_chapter_one(self):
        entry = {"title": "Genesis", "sectionNames": ["Chapter", "Verse"]}

        assert sl._compute_opening_ref_from_entry(entry, "genesis") == "Genesis 1"
        assert self.leaf_calls == [("Genesis", 1)]

    def test_falls_back_to_the_bare_canonical_title(self):
        assert sl._compute_opening_ref_from_entry({"title": "Siddur Ashkenaz"}, "siddur") == "Siddur Ashkenaz"

    def test_missing_title_uses_the_requested_title(self):
        assert sl._compute_opening_ref_from_entry({}, "  Zohar  ") == "Zohar"


# ─── _resolve_opening_ref_for_title ─────────────────────────────────────────

class TestResolveOpeningRefForTitle:
    def test_a_cached_resolution_short_circuits_every_lookup(self, monkeypatch):
        sl._resolved_title_ref_cache.set("genesis", "Genesis 1")

        def _no_lookup(title):
            raise AssertionError("index must not be consulted on a cache hit")

        monkeypatch.setattr(sl, "get_index_entry", _no_lookup)

        assert sl._resolve_opening_ref_for_title("Genesis") == "Genesis 1"

    def test_an_admin_fix_takes_precedence_and_is_cached(self, monkeypatch):
        monkeypatch.setattr(sl, "_load_library_index_adjustments", lambda: {
            "loaded": True, "mtime": 0.0, "remove_keys": set(),
            "fix_map": {"genesis": "Bereshit 1"},
        })

        def _no_lookup(title):
            raise AssertionError("index must not be consulted when a fix exists")

        monkeypatch.setattr(sl, "get_index_entry", _no_lookup)

        assert sl._resolve_opening_ref_for_title("Genesis") == "Bereshit 1"
        assert sl._resolved_title_ref_cache.get("genesis") == "Bereshit 1"

    @pytest.mark.parametrize(
        "entry",
        [None, "not-a-dict", {"error": "Index not found"}],
        ids=["none", "non-dict", "error-entry"],
    )
    def test_unknown_titles_resolve_to_themselves(self, monkeypatch, entry):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: entry)

        assert sl._resolve_opening_ref_for_title("  Made Up Book  ") == "Made Up Book"
        assert sl._resolved_title_ref_cache.get("madeupbook") == "Made Up Book"

    def test_a_found_entry_is_resolved_and_cached(self, monkeypatch):
        monkeypatch.setattr(
            sl, "get_index_entry",
            lambda title: {"title": "Genesis", "sectionNames": ["Chapter", "Verse"]},
        )
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda title, max_refs=None: [])

        assert sl._resolve_opening_ref_for_title("genesis") == "Genesis 1"
        assert sl._resolved_title_ref_cache.get("genesis") == "Genesis 1"

    def test_a_title_with_no_alphanumerics_is_resolved_but_not_cached(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: None)

        assert sl._resolve_opening_ref_for_title("  --  ") == "--"
        assert len(sl._resolved_title_ref_cache) == 0


# ─── _resolve_ref_candidates helper chain ───────────────────────────────────

class TestAddNameCompletionRefs:
    def test_only_ref_completions_are_added_preferring_key_over_title(self):
        candidates, add = _collector()
        name_data = {"completion_objects": [
            {"type": "ref", "key": "Genesis 1", "title": "ignored"},
            {"type": "ref", "title": "Exodus 1"},
            {"type": "Topic", "key": "Shabbat"},
        ]}

        assert sl._add_name_completion_refs(name_data, add, candidates, 10) is False
        assert candidates == ["Genesis 1", "Exodus 1"]

    def test_stops_and_says_so_once_max_candidates_is_reached(self):
        candidates, add = _collector()
        name_data = {"completion_objects": [
            {"type": "ref", "key": "A 1"}, {"type": "ref", "key": "B 1"}, {"type": "ref", "key": "C 1"},
        ]}

        assert sl._add_name_completion_refs(name_data, add, candidates, 2) is True
        assert candidates == ["A 1", "B 1"]

    @pytest.mark.parametrize("name_data", [{}, {"completion_objects": None}])
    def test_missing_completions_add_nothing(self, name_data):
        candidates, add = _collector()

        assert sl._add_name_completion_refs(name_data, add, candidates, 10) is False
        assert candidates == []


class TestBuildRefCandidateTitles:
    def test_dedups_case_insensitively_keeping_first_spelling_in_order(self):
        name_data = {"index": "genesis", "book": "Exodus"}

        assert sl._build_ref_candidate_titles("Genesis", name_data) == ["Genesis", "Exodus"]

    def test_blank_values_are_skipped(self):
        assert sl._build_ref_candidate_titles("  ", {"index": "", "book": None}) == []

    @pytest.mark.parametrize("name_data", [None, [], "text"])
    def test_a_non_dict_name_response_contributes_only_the_title_part(self, name_data):
        assert sl._build_ref_candidate_titles("Leviticus", name_data) == ["Leviticus"]


class TestAddCanonicalAndSuffixRefs:
    def test_adds_canonical_then_canonical_with_suffix(self):
        candidates, add = _collector()

        sl._add_canonical_and_suffix_refs("Shulchan Arukh", "Orach Chayim", add)

        assert candidates == ["Shulchan Arukh", "Shulchan Arukh, Orach Chayim"]

    def test_does_not_repeat_a_suffix_the_canonical_already_ends_with(self):
        candidates, add = _collector()

        sl._add_canonical_and_suffix_refs("Shulchan Arukh, Orach Chayim", "orach chayim", add)

        assert candidates == ["Shulchan Arukh, Orach Chayim"]

    def test_no_suffix_adds_only_the_canonical(self):
        candidates, add = _collector()

        sl._add_canonical_and_suffix_refs("Genesis", "", add)

        assert candidates == ["Genesis"]

    def test_empty_canonical_adds_nothing(self):
        candidates, add = _collector()

        sl._add_canonical_and_suffix_refs("", "Orach Chayim", add)

        assert candidates == []


class TestAddFirstSectionRef:
    @pytest.mark.parametrize("key", ["firstSectionRef", "firstSection"])
    def test_adds_the_stripped_first_section(self, key):
        candidates, add = _collector()

        sl._add_first_section_ref({key: " Genesis 1 "}, add)

        assert candidates == ["Genesis 1"]

    @pytest.mark.parametrize("entry", [{}, {"firstSectionRef": "  "}, {"firstSectionRef": 7}])
    def test_ignores_a_missing_blank_or_non_string_first_section(self, entry):
        candidates, add = _collector()

        sl._add_first_section_ref(entry, add)

        assert candidates == []


class TestAddLeafSectionRefs:
    def test_adds_chapter_one_then_the_leaf_refs(self, monkeypatch):
        seen = []

        def fake_leaf_refs(canonical, max_refs=None):
            seen.append((canonical, max_refs))
            return ["Genesis 1:1", "Genesis 1:2"]

        monkeypatch.setattr(sl, "get_index_leaf_refs", fake_leaf_refs)
        candidates, add = _collector()

        stop = sl._add_leaf_section_refs({"sectionNames": ["Chapter"]}, "Genesis", add, candidates, 10)

        assert stop is False
        assert candidates == ["Genesis 1", "Genesis 1:1", "Genesis 1:2"]
        assert seen == [("Genesis", 4)]

    def test_no_section_names_skips_the_chapter_one_guess(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda canonical, max_refs=None: ["Zohar 1"])
        candidates, add = _collector()

        assert sl._add_leaf_section_refs({}, "Zohar", add, candidates, 10) is False
        assert candidates == ["Zohar 1"]

    def test_stops_when_the_candidate_budget_is_spent(self, monkeypatch):
        monkeypatch.setattr(
            sl, "get_index_leaf_refs", lambda canonical, max_refs=None: ["A 1", "A 2", "A 3"],
        )
        candidates, add = _collector()

        assert sl._add_leaf_section_refs({}, "A", add, candidates, 2) is True
        assert candidates == ["A 1", "A 2"]


class TestProcessRefCandidateTitle:
    def test_unknown_title_adds_nothing_and_does_not_stop(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"error": "not found"})
        candidates, add = _collector()

        assert sl._process_ref_candidate_title("Nope", "", False, add, candidates, 5) is False
        assert candidates == []

    def test_adds_canonical_suffix_first_section_and_leaves(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "title": "Shulchan Arukh", "firstSectionRef": "Shulchan Arukh, Orach Chayim 1",
        })
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda canonical, max_refs=None: ["Shulchan Arukh, Orach Chayim 1:1"])
        candidates, add = _collector()

        stop = sl._process_ref_candidate_title("shulchan arukh", "Orach Chayim", False, add, candidates, 10)

        assert stop is False
        assert candidates == [
            "Shulchan Arukh",
            "Shulchan Arukh, Orach Chayim",
            "Shulchan Arukh, Orach Chayim 1",
            "Shulchan Arukh, Orach Chayim 1:1",
        ]

    def test_a_specific_ref_query_skips_leaf_expansion(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis"})

        def _no_leaves(canonical, max_refs=None):
            raise AssertionError("leaf refs must not be fetched for a specific ref")

        monkeypatch.setattr(sl, "get_index_leaf_refs", _no_leaves)
        candidates, add = _collector()

        assert sl._process_ref_candidate_title("Genesis", "", True, add, candidates, 10) is False
        assert candidates == ["Genesis"]

    def test_reports_that_the_budget_was_reached_by_the_canonical_refs_alone(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis"})
        candidates, add = _collector()

        assert sl._process_ref_candidate_title("Genesis", "", True, add, candidates, 1) is True

    def test_reports_that_the_budget_was_reached_while_adding_leaves(self, monkeypatch):
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis"})
        monkeypatch.setattr(
            sl, "get_index_leaf_refs", lambda canonical, max_refs=None: ["Genesis 1:1", "Genesis 1:2"],
        )
        candidates, add = _collector()

        assert sl._process_ref_candidate_title("Genesis", "", False, add, candidates, 2) is True
        assert candidates == ["Genesis", "Genesis 1:1"]


class TestCorpusAndDirectNameVariants:
    def test_corpus_prefix_is_stripped_and_colon_variant_added(self):
        candidates, add = _collector()

        sl._add_corpus_stripped_ref_variants("Talmud Bavli, Chullin 113a:2", add)

        assert candidates == ["Chullin 113a:2", "Chullin 113a.2"]

    def test_no_corpus_prefix_adds_nothing(self):
        candidates, add = _collector()

        sl._add_corpus_stripped_ref_variants("Chullin 113a", add)

        assert candidates == []

    def test_direct_name_ref_is_added_only_for_is_ref_responses(self):
        candidates, add = _collector()

        sl._add_direct_name_ref({"is_ref": True, "ref": "Genesis 1:1"}, add)
        sl._add_direct_name_ref({"is_ref": False, "ref": "Ignored"}, add)
        sl._add_direct_name_ref({"is_ref": True}, add)

        assert candidates == ["Genesis 1:1"]


class TestResolveRefCandidates:
    def test_blank_ref_has_no_candidates(self):
        assert sl._resolve_ref_candidates("   ") == []

    def test_builds_deduplicated_candidates_from_name_lookup_and_index(self, monkeypatch):
        urls = []

        def fake_cached_get(url, ttl=None):
            urls.append(url)
            return {
                "is_ref": True,
                "ref": "Genesis 1",
                "completion_objects": [{"type": "ref", "key": "Genesis 1"}, {"type": "ref", "key": "Genesis 2"}],
                "index": "Genesis",
            }

        monkeypatch.setattr(sl, "_cached_get", fake_cached_get)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis", "sectionNames": ["Chapter"]})
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda canonical, max_refs=None: [])

        result = sl._resolve_ref_candidates("genesis 1:1")

        assert result[0] == "genesis 1:1"
        assert "genesis 1.1" in result
        assert result.count("Genesis 1") == 1  # from is_ref, completion and section guess: one entry
        assert "Genesis 2" in result
        assert [c.lower() for c in result] == list(dict.fromkeys(c.lower() for c in result))
        assert urls == [f"{sl.SEFARIA_API}/name/genesis_1:1"]

    def test_stops_early_when_completions_fill_the_budget(self, monkeypatch):
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=None: {
            "completion_objects": [{"type": "ref", "key": f"Book {n}"} for n in range(6)],
        })

        def _no_index(title):
            raise AssertionError("index must not be consulted once the budget is spent")

        monkeypatch.setattr(sl, "get_index_entry", _no_index)

        result = sl._resolve_ref_candidates("book", max_candidates=3)

        assert result == ["book", "Book 0", "Book 1"]

    def test_stops_early_when_index_expansion_fills_the_budget(self, monkeypatch):
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=None: None)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {"title": "Genesis"})
        monkeypatch.setattr(
            sl, "get_index_leaf_refs",
            lambda canonical, max_refs=None: ["Genesis 1:1", "Genesis 1:2", "Genesis 1:3", "Genesis 1:4"],
        )

        result = sl._resolve_ref_candidates("Genesis", max_candidates=3)

        assert result == ["Genesis", "Genesis 1:1", "Genesis 1:2"]

    def test_no_name_response_still_yields_the_raw_ref_and_corpus_variants(self, monkeypatch):
        monkeypatch.setattr(sl, "_cached_get", lambda url, ttl=None: None)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: None)

        assert sl._resolve_ref_candidates("Talmud Bavli, Chullin 113a") == [
            "Talmud Bavli, Chullin 113a",
            "Chullin 113a",
        ]
