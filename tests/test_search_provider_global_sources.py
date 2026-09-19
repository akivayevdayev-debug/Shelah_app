"""
Tests for the global-discovery helpers in backend/utils/search_provider.py:
Sefaria global-search hit filtering/dedup, the external-provider (Halachipedia /
HebrewBooks) fetch + trust + URL helpers behind the shared "web" circuit
breaker, and the last-resort web candidate builders.

Search backends are stubbed per test; the conftest `_reset_api_health` autouse
fixture resets the shared health singleton so breaker state never leaks.
"""

from __future__ import annotations

import pytest
import requests

from backend import search
from backend.health_check import FAIL_THRESHOLD, health
from backend.utils import search_provider as sp


# ─── Sefaria hit relevance ──────────────────────────────────────────────────

class TestNormalizeRelevanceQueryTerms:
    def test_lowercases_and_strips_terms(self):
        assert sp._normalize_relevance_query_terms(["  Shabbat ", "CANDLES"]) == ["shabbat", "candles"]

    def test_drops_short_empty_and_none_terms(self):
        assert sp._normalize_relevance_query_terms(["ab", "", None, "  ", "tefillin"]) == ["tefillin"]

    def test_drops_stopwords(self):
        stopword = next(iter(w for w in sp.QUERY_STOPWORDS if len(w) >= 3))

        assert sp._normalize_relevance_query_terms([stopword.upper(), "kashrut"]) == ["kashrut"]


class TestIsSefariaHitRelevant:
    HIT = {
        "ref": "Shulchan Arukh, Orach Chayim 242:1",
        "path": "Halakhah/Shulchan_Arukh",
        "categories": ["Halakhah"],
        "titleVariants": ["Orach Chayim"],
        "exact": "Candles are lit before sunset",
    }

    def test_no_query_terms_means_everything_is_relevant(self):
        assert sp._is_sefaria_hit_relevant(self.HIT, []) is True

    def test_terms_that_are_all_noise_mean_everything_is_relevant(self):
        assert sp._is_sefaria_hit_relevant(self.HIT, ["a", "", None]) is True

    @pytest.mark.parametrize("term", ["candles", "orach", "halakhah", "shulchan arukh"])
    def test_a_term_found_in_any_hit_field_is_relevant(self, term):
        assert sp._is_sefaria_hit_relevant(self.HIT, [term]) is True

    def test_underscores_in_the_path_match_as_spaces(self):
        hit = {"ref": "Some Ref", "path": "Halakhah/Mishneh_Torah"}

        assert sp._is_sefaria_hit_relevant(hit, ["mishneh torah"]) is True

    def test_no_term_found_is_irrelevant(self):
        assert sp._is_sefaria_hit_relevant(self.HIT, ["mikvah", "eruv"]) is False

    def test_non_list_categories_and_variants_are_tolerated(self):
        hit = {"ref": "Genesis 1", "categories": "Tanakh", "titleVariants": None, "exact": "bereshit"}

        assert sp._is_sefaria_hit_relevant(hit, ["bereshit"]) is True
        assert sp._is_sefaria_hit_relevant(hit, ["tanakh"]) is False


class TestResolveNewSefariaHitRef:
    def test_returns_and_marks_a_new_relevant_ref(self):
        seen: set[str] = set()

        ref = sp._resolve_new_sefaria_hit_ref({"ref": " Genesis 1:1 ", "exact": "bereshit"}, ["bereshit"], seen)

        assert ref == "Genesis 1:1"
        assert seen == {"Genesis 1:1"}

    def test_irrelevant_hit_is_rejected_and_not_marked(self):
        seen: set[str] = set()

        assert sp._resolve_new_sefaria_hit_ref({"ref": "Genesis 1:1", "exact": "x"}, ["mikvah"], seen) is None
        assert seen == set()

    @pytest.mark.parametrize("hit", [{"exact": "mikvah"}, {"ref": "  ", "exact": "mikvah"}])
    def test_hit_without_a_ref_is_rejected(self, hit):
        assert sp._resolve_new_sefaria_hit_ref(hit, ["mikvah"], set()) is None

    def test_an_already_seen_ref_is_rejected(self):
        assert sp._resolve_new_sefaria_hit_ref({"ref": "Genesis 1:1", "exact": "x"}, [], {"Genesis 1:1"}) is None


class TestBuildGlobalSefariaSourceEntry:
    def test_uses_the_snippet_and_hit_score(self):
        hit = {"_score": 7.5}
        source = {"heRef": " בראשית א ", "path": " Tanakh/Torah ", "exact": "In the beginning"}

        entry = sp._build_global_sefaria_source_entry(hit, source, "Genesis 1:1", "beginning", "broad-api", 2)

        assert entry == {
            "ref": "Genesis 1:1",
            "title": "Genesis 1:1",
            "lines": [{"en": "In the beginning", "he": "בראשית א"}],
            "domain": "Sefaria",
            "corpus": "sefaria-global-search",
            "path": "Tanakh/Torah",
            "priority": 2,
            "status": "fallback",
            "discovery_stage": "broad-api",
            "search_query": "beginning",
            "score": 7.5,
        }

    def test_falls_back_to_a_generic_line_and_no_score_for_non_dict_hits(self):
        entry = sp._build_global_sefaria_source_entry("raw-hit", {}, "Exodus 1", "q", "specific-api", 1)

        assert entry["lines"] == [{"en": "Matched via global search: q", "he": ""}]
        assert entry["score"] is None


def _hit(ref, text="shabbat"):
    return {"_score": 1.0, "_source": {"ref": ref, "exact": text}}


class TestCollectSefariaSourcesForQuery:
    def test_skips_malformed_hits_and_dedups_across_calls(self, monkeypatch):
        monkeypatch.setattr(sp, "_query_search_wrapper", lambda query, size=80: [
            "not-a-dict",
            {"_source": "not-a-dict"},
            {"_source": {"exact": "shabbat"}},   # no ref
            _hit("Shabbat 1a"),
            _hit("Shabbat 1a"),                  # duplicate ref
            _hit("Shabbat 2a"),
        ])
        seen: set[str] = set()

        sources = sp._collect_sefaria_sources_for_query("shabbat", [], "broad-api", 2, 10, 10, seen)

        assert [s["ref"] for s in sources] == ["Shabbat 1a", "Shabbat 2a"]
        assert seen == {"Shabbat 1a", "Shabbat 2a"}

    def test_stops_at_the_per_query_limit(self, monkeypatch):
        monkeypatch.setattr(sp, "_query_search_wrapper", lambda query, size=80: [_hit(f"Shabbat {n}a") for n in range(2, 8)])

        sources = sp._collect_sefaria_sources_for_query("shabbat", [], "broad-api", 2, 2, 10, set())

        assert [s["ref"] for s in sources] == ["Shabbat 2a", "Shabbat 3a"]

    def test_stops_at_the_remaining_slots(self, monkeypatch):
        monkeypatch.setattr(sp, "_query_search_wrapper", lambda query, size=80: [_hit(f"Shabbat {n}a") for n in range(2, 8)])

        sources = sp._collect_sefaria_sources_for_query("shabbat", [], "broad-api", 2, 5, 1, set())

        assert [s["ref"] for s in sources] == ["Shabbat 2a"]

    def test_falls_back_to_the_supplied_terms_when_the_query_has_no_keywords(self, monkeypatch):
        monkeypatch.setattr(sp, "_extract_query_keywords", lambda text: [])
        monkeypatch.setattr(sp, "_query_search_wrapper", lambda query, size=80: [
            _hit("Berakhot 2a", "shema"), _hit("Shabbat 2a", "candles"),
        ])

        sources = sp._collect_sefaria_sources_for_query("??", ["shema"], "broad-api", 2, 10, 10, set())

        assert [s["ref"] for s in sources] == ["Berakhot 2a"]


class TestCollectGlobalSefariaSources:
    @pytest.fixture
    def queried(self, monkeypatch):
        calls = []

        def fake_query(query, size=80):
            calls.append(query)
            return [_hit(f"{query} {n}") for n in range(1, 6)]

        monkeypatch.setattr(sp, "_query_search_wrapper", fake_query)
        monkeypatch.setattr(sp, "_extract_query_keywords", lambda text: [])
        return calls

    def test_blank_queries_are_skipped(self, queried):
        sources = sp._collect_global_sefaria_sources(["", None, "  ", "shabbat"], [], "broad-api", 2)

        assert queried == ["shabbat"]
        assert len(sources) == 2  # broad stage: 2 per query

    def test_specific_stage_allows_three_per_query(self, queried):
        sources = sp._collect_global_sefaria_sources(["shabbat"], [], "specific-api", 1)

        assert len(sources) == 3

    def test_stops_querying_once_max_results_is_reached(self, queried):
        sources = sp._collect_global_sefaria_sources(["a", "b", "c"], [], "specific-api", 1, max_results=4)

        assert len(sources) == 4
        assert queried == ["a", "b"]  # 3 from "a", the 4th from "b"; "c" never queried


# ─── External providers behind the "web" breaker ────────────────────────────

class TestFetchProviderSearchPayload:
    def test_returns_the_payload_and_records_success(self):
        payload = {"title": "Shabbat"}

        assert sp._fetch_provider_search_payload("Halachipedia", lambda q: payload, "shabbat") is payload
        assert health._circuits["web"].failures == 0

    def test_open_breaker_skips_the_call(self):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("web")
        calls = []

        result = sp._fetch_provider_search_payload("Halachipedia", lambda q: calls.append(q), "shabbat")

        assert result is None
        assert calls == []

    @pytest.mark.parametrize("error", [requests.ConnectionError("down"), TimeoutError("slow")])
    def test_request_errors_record_a_failure_and_return_none(self, error):
        def failing(query):
            raise error

        assert sp._fetch_provider_search_payload("HebrewBooks", failing, "shabbat") is None
        assert health._circuits["web"].failures == 1

    def test_a_non_dict_payload_is_discarded_but_still_counts_as_success(self):
        assert sp._fetch_provider_search_payload("HebrewBooks", lambda q: ["x"], "shabbat") is None
        assert health._circuits["web"].failures == 0


class TestExtractTrustedWebTitleSummary:
    def test_returns_title_and_summary_for_a_trusted_provider(self):
        payload = {"title": " Shabbat candles ", "summary": " Lit before sunset "}

        assert sp._extract_trusted_web_title_summary("HebrewBooks", payload, []) == (
            "Shabbat candles", "Lit before sunset",
        )

    def test_strips_the_halachipedia_prefix(self):
        payload = {"title": "[Halachipedia] Shabbat candles", "summary": "Lit before sunset"}

        assert sp._extract_trusted_web_title_summary("Halachipedia", payload, []) == (
            "Shabbat candles", "Lit before sunset",
        )

    @pytest.mark.parametrize("payload", [{}, {"title": "", "summary": None}, {"title": "  ", "summary": " "}])
    def test_payloads_with_neither_title_nor_summary_are_rejected(self, payload):
        assert sp._extract_trusted_web_title_summary("HebrewBooks", payload, ["shabbat"]) is None

    def test_a_missing_summary_fails_the_trust_check(self):
        assert sp._extract_trusted_web_title_summary("HebrewBooks", {"title": "Shabbat"}, ["shabbat"]) is None

    def test_a_blocklisted_match_is_rejected(self):
        blocked = next(iter(sp.WEB_FALLBACK_BLOCKLIST_TERMS))

        payload = {"title": "Shabbat", "summary": f"about {blocked}"}

        assert sp._extract_trusted_web_title_summary("Halachipedia", payload, ["shabbat"]) is None


class TestBuildExternalSourceUrl:
    def test_uses_the_payload_url_when_there_is_one(self):
        payload = {"url": " https://example.org/page "}

        assert sp._build_external_source_url("Halachipedia", "Title", "q", payload) == "https://example.org/page"

    def test_halachipedia_builds_a_wiki_url_from_the_title(self):
        url = sp._build_external_source_url("Halachipedia", "Shabbat candles/lighting", "q", {})

        assert url == "https://halachipedia.com/wiki/Shabbat_candles%2Flighting"

    def test_halachipedia_without_a_title_has_no_url(self):
        assert sp._build_external_source_url("Halachipedia", "", "q", {}) == ""

    def test_hebrewbooks_links_to_a_full_text_search_for_the_query(self):
        url = sp._build_external_source_url("HebrewBooks", "Title", "shabbat & candles", {})

        assert url == "https://www.hebrewbooks.org/search.aspx?st=FT&q=shabbat%20%26%20candles"

    def test_unknown_providers_get_an_empty_url(self):
        assert sp._build_external_source_url("Other", "Title", "q", {}) == ""


# ─── Last-resort web candidates ─────────────────────────────────────────────

class TestFetchTrustedWebCandidate:
    @staticmethod
    def _build(payload, keywords):
        return {"built_from": payload, "keywords": keywords}

    def test_builds_a_candidate_from_the_payload(self):
        result = sp._fetch_trusted_web_candidate(
            lambda q: {"title": q}, "shabbat", "wikipedia", ["shabbat"], self._build)

        assert result == {"built_from": {"title": "shabbat"}, "keywords": ["shabbat"]}

    def test_blank_query_makes_no_call(self):
        calls = []

        assert sp._fetch_trusted_web_candidate(calls.append, "", "wikipedia", [], self._build) is None
        assert calls == []

    def test_open_breaker_makes_no_call(self):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("web")
        calls = []

        assert sp._fetch_trusted_web_candidate(calls.append, "shabbat", "wikipedia", [], self._build) is None
        assert calls == []

    @pytest.mark.parametrize("error", [requests.Timeout("slow"), TimeoutError("slow")])
    def test_request_errors_record_a_failure_and_return_none(self, error):
        def failing(query):
            raise error

        assert sp._fetch_trusted_web_candidate(failing, "shabbat", "wikipedia", [], self._build) is None
        assert health._circuits["web"].failures == 1

    def test_a_non_dict_payload_yields_no_candidate(self):
        assert sp._fetch_trusted_web_candidate(lambda q: None, "shabbat", "wikipedia", [], self._build) is None


class TestWebCandidateBuilders:
    def test_halachipedia_candidate_strips_prefix_and_builds_the_wiki_url(self):
        payload = {"title": "[Halachipedia] Shabbat candles", "summary": "Lit before sunset"}

        assert sp._build_halachipedia_candidate(payload, ["shabbat"]) == {
            "provider": "Halachipedia",
            "domain": "halachipedia.com",
            "title": "Shabbat candles",
            "summary": "Lit before sunset",
            "url": "https://halachipedia.com/wiki/Shabbat_candles",
        }

    def test_halachipedia_prefix_only_title_falls_back_to_the_raw_title_and_home_url(self):
        payload = {"title": "[Halachipedia]", "summary": "Lit before sunset"}

        candidate = sp._build_halachipedia_candidate(payload, ["shabbat"])

        # The prefix-stripped title is empty, which fails the trust check
        # (a match needs both a title and a summary).
        assert candidate is None

    def test_untrusted_halachipedia_payload_is_rejected(self):
        blocked = next(iter(sp.WEB_FALLBACK_BLOCKLIST_TERMS))

        assert sp._build_halachipedia_candidate({"title": "T", "summary": blocked}, []) is None

    def test_wikipedia_candidate_needs_a_trust_term_or_keyword(self):
        payload = {"title": "Sabbath", "summary": "A day of rest"}

        assert sp._build_wikipedia_candidate(payload, ["mikvah"]) is None
        candidate = sp._build_wikipedia_candidate(payload, ["sabbath"])
        assert candidate == {
            "provider": "Wikipedia",
            "domain": "en.wikipedia.org",
            "title": "Sabbath",
            "summary": "A day of rest",
            "url": "https://en.wikipedia.org/wiki/Sabbath",
        }


class TestDedupeAndFormatWebSources:
    def test_dedupes_by_provider_and_title_case_insensitively_up_to_the_limit(self):
        candidates = [
            {"provider": "Wikipedia", "title": "Sabbath"},
            {"provider": "wikipedia", "title": " SABBATH "},
            {"provider": "Halachipedia", "title": "Sabbath"},
            {"provider": "Halachipedia", "title": "Candles"},
        ]

        assert sp._dedupe_web_candidates(candidates, 10) == [candidates[0], candidates[2], candidates[3]]
        assert sp._dedupe_web_candidates(candidates, 2) == [candidates[0], candidates[2]]

    def test_formats_sources_and_truncates_long_fields(self):
        item = {
            "title": "T" * 200, "summary": "S" * 1200, "domain": "en.wikipedia.org",
            "provider": "Wikipedia", "url": "https://en.wikipedia.org/wiki/T",
        }

        [source] = sp._format_web_sources([item])

        assert len(source["ref"]) == 140
        assert len(source["title"]) == 160
        assert len(source["lines"][0]["en"]) == 1000
        assert source["corpus"] == "general-web"
        assert source["status"] == "fallback-web"
        assert source["priority"] == 3
        assert source["source_provider"] == "Wikipedia"

    def test_an_item_without_a_title_is_labelled_web_source(self):
        [source] = sp._format_web_sources([{"summary": "text"}])

        assert source["ref"] == source["title"] == "Web Source"


class TestBuildLastResortWebSources:
    def test_queries_halachipedia_and_wikipedia_and_returns_deduped_web_sources(self, monkeypatch):
        halachipedia_queries, wikipedia_queries = [], []

        def fake_halachipedia(query):
            halachipedia_queries.append(query)
            return {"title": "[Halachipedia] Shabbat candles", "summary": "Lit before sunset"}

        def fake_wikipedia(query):
            wikipedia_queries.append(query)
            return {"title": "Sabbath", "summary": "A Jewish day of rest, halakha"}

        monkeypatch.setattr(search, "search_halachipedia", fake_halachipedia)
        monkeypatch.setattr(search, "search_wikipedia", fake_wikipedia)

        sources = sp._build_last_resort_web_sources("When do I light Shabbat candles?", ["shabbat", "candles"])

        assert halachipedia_queries == [
            "shabbat candles",
            "halakha shabbat candles",
            "When do I light Shabbat candles?",
        ]
        assert wikipedia_queries == [
            "Peninei Halakha",
            "Yeshivat Har Bracha",
            "HebrewBooks",
            "Halakha shabbat candles",
            "Halakha When do I light Shabbat candles?",
        ]
        # Every call returned the same title per provider, so dedup leaves one each.
        assert [(s["source_provider"], s["title"]) for s in sources] == [
            ("Halachipedia", "Shabbat candles"),
            ("Wikipedia", "Sabbath"),
        ]

    def test_untrusted_or_failed_lookups_yield_no_sources(self, monkeypatch):
        def failing(query):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(search, "search_halachipedia", failing)
        monkeypatch.setattr(search, "search_wikipedia", lambda query: None)

        assert sp._build_last_resort_web_sources("anything", ["anything"]) == []
