"""
Golden-master characterization tests for the retrieval & corpus-matching layer
being extracted from app.py into backend/utils/search_provider.py (Phase 2 of
the backend refactor in plan.md).

These pin the exact current output of the lemmatization, discovery-query,
global-source-collection, local-corpus-match, and reconciled translation
functions so the move is provably behavior-preserving. Values were captured by
exercising the pre-move app.py / backend/helpers.py functions directly (with
tests/conftest.py's offline HTTP mocking active). Do not "fix" any assertion
here without a deliberate, separately-reviewed behavior change — this file's
job is to prove the move changed nothing, not to improve matching quality.

Note: `_strip_common_hebrew_prefixes` strips any leading character present in
HEBREW_PREFIXES in a loop, including when that character is actually part of a
word's root rather than a grammatical prefix (e.g. "שבת" -> "בת"). This is
existing, pinned behavior — not a bug to fix here.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib
from backend.health_check import FAIL_THRESHOLD

from backend.utils.search_provider import (
    HEBREW_WORD_GLOSSARY,
    HALAKHIC_CORPUS_ALIASES,
    QUERY_STOPWORDS,
    HEBREW_PREFIXES,
    _strip_common_hebrew_prefixes,
    _expand_hebrew_keyword_forms,
    _extract_query_keywords,
    _query_search_wrapper,
    _match_corpus,
    _extract_hit_snippet,
    _dedupe_ordered_text,
    _match_direct_topics,
    _build_discovery_queries,
    _is_sefaria_hit_relevant,
    _collect_global_sefaria_sources,
    _collect_external_global_sources,
    _iter_local_json_matches,
    _find_local_custom_matches,
    _looks_like_trusted_web_match,
    _build_last_resort_web_sources,
    get_halakhic_sources,
    _translate_text_google,
    _translate_text_mymemory,
)
from backend.utils import search_provider as sp


# ── HEBREW_PREFIXES / _strip_common_hebrew_prefixes ───────────────────────────

def test_strip_common_hebrew_prefixes_strips_stacked_prefixes():
    assert _strip_common_hebrew_prefixes("ולשבת") == "בת"


def test_strip_common_hebrew_prefixes_single_prefix():
    assert _strip_common_hebrew_prefixes("השבת") == "בת"
    assert _strip_common_hebrew_prefixes("בבית") == "ית"
    assert _strip_common_hebrew_prefixes("מהעולם") == "עולם"


def test_strip_common_hebrew_prefixes_keeps_short_or_nonhebrew_tokens():
    assert _strip_common_hebrew_prefixes("תורה") == "תורה"
    assert _strip_common_hebrew_prefixes("וה") == "וה"
    assert _strip_common_hebrew_prefixes("x") == "x"
    assert _strip_common_hebrew_prefixes("") == ""
    assert _strip_common_hebrew_prefixes(None) == ""


def test_hebrew_prefixes_constant_unchanged():
    assert HEBREW_PREFIXES == ("ו", "ה", "ל", "ב", "ש", "מ")


# ── _expand_hebrew_keyword_forms ──────────────────────────────────────────────

def test_expand_hebrew_keyword_forms_adds_stripped_variant():
    assert _expand_hebrew_keyword_forms("השבת") == ["השבת", "בת"]
    assert _expand_hebrew_keyword_forms("בבית") == ["בבית", "ית"]


def test_expand_hebrew_keyword_forms_non_hebrew_returns_single_form():
    assert _expand_hebrew_keyword_forms("shabbat") == ["shabbat"]


def test_expand_hebrew_keyword_forms_empty_input():
    assert _expand_hebrew_keyword_forms("") == []


# ── QUERY_STOPWORDS / _extract_query_keywords ─────────────────────────────────

def test_extract_query_keywords_english_drops_stopwords():
    result = _extract_query_keywords("What is the halacha about Shabbat candles?")
    assert result == ["shabbat", "candles"]


def test_extract_query_keywords_hebrew_expands_prefixed_forms():
    result = _extract_query_keywords("האם מותר להדליק נר בשבת")
    assert result == ["אם", "מותר", "תר", "להדליק", "דליק", "בשבת", "בת"]


def test_extract_query_keywords_all_stopwords_returns_empty():
    assert _extract_query_keywords("the and for with") == []


def test_extract_query_keywords_respects_max_keywords():
    result = _extract_query_keywords("alpha beta gamma delta epsilon", max_keywords=2)
    assert len(result) == 2


def test_query_stopwords_contains_expected_bilingual_terms():
    assert "the" in QUERY_STOPWORDS
    assert "halacha" in QUERY_STOPWORDS
    assert "האם" in QUERY_STOPWORDS


# ── HEBREW_WORD_GLOSSARY / HALAKHIC_CORPUS_ALIASES ────────────────────────────

def test_hebrew_word_glossary_has_expected_entries():
    assert HEBREW_WORD_GLOSSARY["שבת"] == "Shabbat, the seventh day of rest."
    assert HEBREW_WORD_GLOSSARY["תורה"] == "Torah, the Five Books of Moses and Torah teaching."
    assert len(HEBREW_WORD_GLOSSARY) == 18


def test_halakhic_corpus_aliases_has_expected_keys():
    assert set(HALAKHIC_CORPUS_ALIASES.keys()) == {
        "Shulchan Arukh", "Rambam", "Mishnah Berurah", "Talmud", "Gemara",
    }


# ── _match_corpus (unused by any current caller, moved verbatim) ─────────────

def test_match_corpus_matches_alias_in_ref_or_categories():
    hit_source = {
        "ref": "Shulchan Arukh, Orach Chayim 242",
        "path": "Halakhah/Shulchan Arukh/Orach Chayim",
        "categories": ["Halakhah"],
        "titleVariants": ["Shulchan Arukh"],
    }
    assert _match_corpus(hit_source, HALAKHIC_CORPUS_ALIASES["Shulchan Arukh"]) is True
    assert _match_corpus(hit_source, HALAKHIC_CORPUS_ALIASES["Rambam"]) is False


def test_match_corpus_non_dict_returns_false():
    assert _match_corpus("not a dict", ["x"]) is False


# ── _extract_hit_snippet ───────────────────────────────────────────────────────

def test_extract_hit_snippet_prefers_naive_lemmatizer_and_collapses_whitespace():
    assert _extract_hit_snippet({"naive_lemmatizer": "  some   text   here  "}) == "some text here"


def test_extract_hit_snippet_falls_back_to_exact():
    assert _extract_hit_snippet({"exact": "exact text"}) == "exact text"


def test_extract_hit_snippet_empty_source_returns_empty_string():
    assert _extract_hit_snippet({}) == ""


# ── _is_sefaria_hit_relevant ───────────────────────────────────────────────────

def test_is_sefaria_hit_relevant_true_when_term_present():
    hit_source = {
        "ref": "Shulchan Arukh 242", "categories": ["Halakhah"], "titleVariants": [],
        "naive_lemmatizer": "candle lighting shabbat",
    }
    assert _is_sefaria_hit_relevant(hit_source, ["shabbat"]) is True


def test_is_sefaria_hit_relevant_false_when_term_absent():
    hit_source = {
        "ref": "Shulchan Arukh 242", "categories": ["Halakhah"], "titleVariants": [],
        "naive_lemmatizer": "candle lighting shabbat",
    }
    assert _is_sefaria_hit_relevant(hit_source, ["kashrut"]) is False


def test_is_sefaria_hit_relevant_true_when_no_query_terms():
    assert _is_sefaria_hit_relevant({"ref": "x", "categories": [], "titleVariants": []}, []) is True


# ── _dedupe_ordered_text ───────────────────────────────────────────────────────

def test_dedupe_ordered_text_normalizes_whitespace_and_dedups_case_insensitively():
    result = _dedupe_ordered_text(["Shabbat", "shabbat ", " Shabbat", "Torah", ""])
    assert result == ["Shabbat", "Torah"]


def test_dedupe_ordered_text_respects_max_items():
    assert _dedupe_ordered_text(["a", "b", "c", "d"], max_items=2) == ["a", "b"]


# ── _match_direct_topics / DIRECT_TOPIC_SOURCE_MAP ────────────────────────────

def test_match_direct_topics_matches_omer_trigger():
    assert _match_direct_topics("Can I get a haircut during the omer?", ["omer", "haircut"]) == ["omer"]


def test_match_direct_topics_no_match_returns_empty():
    assert _match_direct_topics("What blessing do I say over bread?", ["bread"]) == []


# ── _build_discovery_queries ───────────────────────────────────────────────────

def test_build_discovery_queries_omer_topic_shape():
    result = _build_discovery_queries("Can I get a haircut during the omer?", ["omer", "haircut"])
    assert result["topics"] == ["omer"]
    assert result["specific_queries"] == [
        "Shulchan Arukh, Orach Chayim 489",
        "Shulchan Arukh, Orach Chayim 493",
        "omer shulchan arukh",
        "Can I get a haircut during the omer?",
    ]
    assert result["broad_queries"][0] == "omer haircut sefirat haomer sefira haircuts"


def test_build_discovery_queries_no_topic_falls_back_to_question():
    result = _build_discovery_queries("What blessing do I say over bread?", ["bread"])
    assert result["topics"] == []
    assert result["specific_queries"] == ["What blessing do I say over bread?"]


# ── _query_search_wrapper (network) ────────────────────────────────────────────

def test_query_search_wrapper_returns_hits_on_success(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            "https://www.sefaria.org.il/api/search-wrapper",
            json={"hits": {"hits": [{"_source": {"ref": "X"}, "_score": 1.0}]}},
            status=200,
        )
        assert _query_search_wrapper("shabbat") == [{"_source": {"ref": "X"}, "_score": 1.0}]


def test_query_search_wrapper_upstream_failure_returns_empty_list(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses_lib.POST, "https://www.sefaria.org.il/api/search-wrapper", status=500)
        assert _query_search_wrapper("shabbat") == []


def test_query_search_wrapper_unmocked_domain_returns_empty_list(mock_outbound_http):
    # No sefaria.org.il registration at all (only conftest's default mocks) -> the
    # nested-network exception is swallowed and an empty list is returned.
    assert _query_search_wrapper("shabbat") == []


# ── _looks_like_trusted_web_match / WEB_FALLBACK_*_TERMS ──────────────────────

def test_looks_like_trusted_web_match_true_for_known_provider():
    assert _looks_like_trusted_web_match("halachipedia", "Shabbat", "some summary", ["shabbat"]) is True


def test_looks_like_trusted_web_match_true_for_trust_term_in_text():
    assert _looks_like_trusted_web_match("wikipedia", "Random Title", "no trust terms here", ["shabbat"]) is False
    assert _looks_like_trusted_web_match("wikipedia", "Shabbat customs", "halakh practices", ["shabbat"]) is True


def test_looks_like_trusted_web_match_false_for_blocklist_terms():
    assert _looks_like_trusted_web_match("wikipedia", "King James Bible Title", "gospel content", ["shabbat"]) is False


def test_looks_like_trusted_web_match_false_for_empty_title_or_summary():
    assert _looks_like_trusted_web_match("wikipedia", "", "", ["shabbat"]) is False


# ── _collect_global_sefaria_sources ────────────────────────────────────────────

def test_collect_global_sefaria_sources_shapes_hits_into_sources(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            "https://www.sefaria.org.il/api/search-wrapper",
            json={
                "hits": {"hits": [{
                    "_source": {
                        "ref": "Shulchan Arukh 242", "heRef": "he-ref", "path": "p",
                        "categories": [], "titleVariants": [], "naive_lemmatizer": "shabbat candles",
                    },
                    "_score": 2.0,
                }]}
            },
            status=200,
        )
        result = _collect_global_sefaria_sources(
            ["shabbat candles"], fallback_terms=["shabbat"], discovery_stage="specific-api", priority=1,
        )
    assert result == [{
        "ref": "Shulchan Arukh 242",
        "title": "Shulchan Arukh 242",
        "lines": [{"en": "shabbat candles", "he": "he-ref"}],
        "domain": "Sefaria",
        "corpus": "sefaria-global-search",
        "path": "p",
        "priority": 1,
        "status": "fallback",
        "discovery_stage": "specific-api",
        "search_query": "shabbat candles",
        "score": 2.0,
    }]


def test_collect_external_global_sources_unmocked_domain_returns_empty(mock_outbound_http):
    result = _collect_external_global_sources(
        ["shabbat"], keywords=["shabbat"], discovery_stage="specific-api", priority=1,
    )
    assert result == []


def test_collect_external_global_sources_builds_entries_from_both_providers(mock_outbound_http):
    """Anchors the D->B _collect_external_global_sources refactor (plan.md
    §32.1): exercises the health-gated fetch, title/summary trust check, and
    both branches of the per-provider URL fallback (Halachipedia has no
    payload "url" so builds a /wiki/ slug; HebrewBooks' payload already
    carries one from urljoin, so no fallback needed) in one pass."""
    import backend.search as search_module
    search_module._HALACHIPEDIA_CACHE.clear()
    search_module._HEBREWBOOKS_CACHE.clear()

    mock_outbound_http.add(
        responses_lib.GET, re.compile(r"https://halachipedia\.com/api\.php.*"),
        json={"query": {"search": [{"title": "Shabbat Candles"}]}},
        status=200,
    )
    mock_outbound_http.add(
        responses_lib.GET, re.compile(r"https://halachipedia\.com/api\.php.*"),
        json={"query": {"pages": {"1": {
            "title": "Shabbat Candles",
            "extract": "Light candles before sunset for Shabbat.",
        }}}},
        status=200,
    )
    mock_outbound_http.add(
        responses_lib.GET, re.compile(r"https://www\.hebrewbooks\.org/search\.aspx.*"),
        body='<a href="/pdfpager.aspx?req=1&st=FT">Shabbat Candles Commentary</a>',
        status=200,
    )

    result = _collect_external_global_sources(
        ["candle lighting"], keywords=["candle"], discovery_stage="specific-api", priority=1,
    )

    assert [r["source_provider"] for r in result] == ["Halachipedia", "HebrewBooks"]
    halachipedia_entry, hebrewbooks_entry = result

    assert halachipedia_entry["title"] == "Shabbat Candles"
    assert halachipedia_entry["domain"] == "halachipedia.com"
    assert halachipedia_entry["url"] == "https://halachipedia.com/wiki/Shabbat_Candles"
    assert halachipedia_entry["corpus"] == "external-global-search"
    assert halachipedia_entry["search_query"] == "candle lighting"

    assert hebrewbooks_entry["title"] == "[HebrewBooks] Shabbat Candles Commentary"
    assert hebrewbooks_entry["domain"] == "hebrewbooks.org"
    assert hebrewbooks_entry["url"] == "https://www.hebrewbooks.org/pdfpager.aspx?req=1&st=FT"


def test_build_last_resort_web_sources_unmocked_domain_returns_empty(mock_outbound_http):
    assert _build_last_resort_web_sources("Shabbat candles", ["shabbat"]) == []


# ── _iter_local_json_matches / _find_local_custom_matches (fixture corpus) ────

def test_iter_local_json_matches_finds_nested_title_and_minhag_fields():
    payload = {
        "Minhag": "Light candles 18 minutes before sunset",
        "nested": {"Title": "Shabbat Candle Lighting Custom"},
        "list_field": [{"minhag": "Some other custom about Shabbat"}],
    }
    result = _iter_local_json_matches(payload, ["shabbat"], "fixture.json")
    assert result == [
        {
            "file": "fixture.json", "field": "Title", "value": "Shabbat Candle Lighting Custom",
            "match_keywords": ["shabbat"], "pointer": "root.nested",
        },
        {
            "file": "fixture.json", "field": "minhag", "value": "Some other custom about Shabbat",
            "match_keywords": ["shabbat"], "pointer": "root.list_field[0]",
        },
    ]


def test_iter_local_json_matches_no_keyword_hit_returns_empty():
    payload = {"Minhag": "Light candles 18 minutes before sunset"}
    assert _iter_local_json_matches(payload, ["shabbat"], "fixture.json") == []


@pytest.fixture
def fixture_customs_corpus(tmp_path, monkeypatch):
    """A two-file customs corpus under a temp APP_ROOT, for local-match tests."""
    customs_dir = tmp_path / "customs"
    customs_dir.mkdir()
    (customs_dir / "ashkenaz.json").write_text(
        '{"Title": "Ashkenaz Shabbat Candle Custom", "Minhag": "Light early"}',
        encoding="utf-8",
    )
    (customs_dir / "sefardi.json").write_text(
        '{"Title": "Sefardi Havdalah Custom", "Minhag": "Use spices"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(sp, "APP_ROOT", tmp_path)
    return tmp_path


def test_find_local_custom_matches_matches_correct_file(fixture_customs_corpus):
    result = _find_local_custom_matches(["shabbat"])
    assert result == [{
        "file": "ashkenaz.json", "field": "Title", "value": "Ashkenaz Shabbat Candle Custom",
        "match_keywords": ["shabbat"], "pointer": "root",
    }]


def test_find_local_custom_matches_different_keyword_matches_other_file(fixture_customs_corpus):
    result = _find_local_custom_matches(["havdalah"])
    assert result == [{
        "file": "sefardi.json", "field": "Title", "value": "Sefardi Havdalah Custom",
        "match_keywords": ["havdalah"], "pointer": "root",
    }]


def test_find_local_custom_matches_no_match_returns_empty(fixture_customs_corpus):
    assert _find_local_custom_matches(["nonexistentterm"]) == []


# ── get_halakhic_sources (full fallback ladder) ───────────────────────────────

def test_get_halakhic_sources_empty_result_falls_back_to_internal_ai(mock_outbound_http):
    # No sefaria.org.il / halachipedia / hebrewbooks mock registered -> every
    # external provider silently fails closed -> internal-ai-needed fallback.
    result = get_halakhic_sources("What is the halacha about Shabbat candles?")
    assert result["status"] == "internal-ai-needed"
    assert result["fallback_level"] == "internal-ai-knowledge"
    assert result["counts"] == {
        "specific_api": 0, "broad_api": 0, "internal_ai": 1, "sefaria": 0, "external": 0,
    }
    assert result["source_count"] == 1
    assert result["sources"][0]["domain"] == "internal-ai"
    assert result["internal_disclaimer"]


def test_get_halakhic_sources_happy_path_returns_specific_api_sources(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            "https://www.sefaria.org.il/api/search-wrapper",
            json={
                "hits": {"hits": [{
                    "_source": {
                        "ref": "Shulchan Arukh, Orach Chayim 242",
                        "heRef": "שולחן ערוך, אורח חיים רמב",
                        "path": "Halakhah/Shulchan Arukh/Orach Chayim",
                        "categories": ["Halakhah"],
                        "titleVariants": ["Shulchan Arukh"],
                        "naive_lemmatizer": "Laws of Shabbat candle lighting time",
                    },
                    "_score": 5.0,
                }]}
            },
            status=200,
        )
        result = get_halakhic_sources("Shabbat candle lighting")

    assert result["status"] == "fallback"
    assert result["fallback_level"] == "specific-api"
    assert result["counts"] == {
        "specific_api": 1, "broad_api": 0, "internal_ai": 0, "sefaria": 1, "external": 0,
    }
    assert result["source_count"] == 1
    assert result["sources"][0]["ref"] == "Shulchan Arukh, Orach Chayim 242"
    assert result["sources"][0]["corpus"] == "sefaria-global-search"
    assert result["internal_disclaimer"] == ""


# ── Reconciled duplicate: _translate_text_google / _translate_text_mymemory ──
# Canonical home is now backend/utils/search_provider.py (previously diverged
# copies lived in both app.py and backend/helpers.py — see plan.md §2).

def test_translate_text_google_empty_text_returns_empty_string():
    assert _translate_text_google("", "he", "en") == ""
    assert _translate_text_google(None, "he", "en") == ""


def test_translate_text_google_success_returns_translated_text(mock_outbound_http):
    assert _translate_text_google("שבת", "he", "en") == "mock translation"


def test_translate_text_google_upstream_failure_returns_empty_string(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://translate\.googleapis\.com/.*"),
            status=500,
        )
        assert _translate_text_google("שבת", "he", "en") == ""


def test_translate_text_google_echo_translation_returns_empty_string(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://translate\.googleapis\.com/.*"),
            json=[[["Hello", "source text", None, None, 0]], None, "auto"],
            status=200,
        )
        assert _translate_text_google("Hello", "en", "he") == ""


def test_translate_text_mymemory_empty_text_returns_empty_string():
    assert _translate_text_mymemory("", "he", "en") == ""


def test_translate_text_mymemory_success_returns_translated_text(mock_outbound_http):
    assert _translate_text_mymemory("שבת", "he", "en") == "mock translation"


def test_translate_text_mymemory_missing_response_data_returns_empty_string(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://api\.mymemory\.translated\.net/.*"),
            json={},
            status=200,
        )
        assert _translate_text_mymemory("שבת", "he", "en") == ""


# ─────────────── Phase 3: circuit-breaker hardening on network calls ───────────
#
# The `_reset_api_health` autouse fixture in conftest.py resets the shared
# `backend.health_check.health` singleton around every test, so these tests
# can freely trip circuits without leaking state into unrelated tests.


def test_query_search_wrapper_skips_call_when_sefaria_circuit_open(mock_outbound_http):
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("sefaria")

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            sp.SEFARIA_SEARCH_WRAPPER_URL,
            json={"hits": {"hits": [{"_source": {"ref": "Should Not Be Reached"}}]}},
            status=200,
        )
        assert _query_search_wrapper("shabbat") == []
        assert len(rsps.calls) == 0


def test_query_search_wrapper_upstream_failure_records_health_failure(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            sp.SEFARIA_SEARCH_WRAPPER_URL,
            status=500,
        )
        for _ in range(FAIL_THRESHOLD):
            _query_search_wrapper("shabbat")

    assert sp.health.is_healthy("sefaria") is False


def test_query_search_wrapper_success_records_health_success(mock_outbound_http):
    sp.health.record_failure("sefaria")
    sp.health.record_failure("sefaria")

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.POST,
            sp.SEFARIA_SEARCH_WRAPPER_URL,
            json={"hits": {"hits": []}},
            status=200,
        )
        _query_search_wrapper("shabbat")

    assert sp.health._circuits["sefaria"].failures == 0


def test_collect_external_global_sources_skips_when_web_circuit_open(mock_outbound_http):
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("web")

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://halachipedia\.com/api\.php.*"),
            json={"query": {"search": [{"title": "Should Not Be Reached"}]}},
            status=200,
        )
        result = _collect_external_global_sources(
            ["shabbat"], keywords=["shabbat"], discovery_stage="specific-api", priority=1,
        )
        assert result == []
        assert len(rsps.calls) == 0


def test_build_last_resort_web_sources_skips_when_web_circuit_open(mock_outbound_http):
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("web")

    result = _build_last_resort_web_sources("shabbat candle lighting", ["shabbat"])
    assert result == []


def test_translate_text_google_skips_call_when_circuit_open(mock_outbound_http):
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("translate_google")

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://translate\.googleapis\.com/.*"),
            json=[[["should not be reached", "שבת", None, None, 0]], None, "auto"],
            status=200,
        )
        assert _translate_text_google("שבת", "he", "en") == ""
        assert len(rsps.calls) == 0


def test_translate_text_mymemory_skips_call_when_circuit_open(mock_outbound_http):
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("translate_mymemory")

    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://api\.mymemory\.translated\.net/.*"),
            json={"responseData": {"translatedText": "should not be reached"}},
            status=200,
        )
        assert _translate_text_mymemory("שבת", "he", "en") == ""
        assert len(rsps.calls) == 0


def test_translate_text_google_upstream_failure_opens_circuit_after_threshold(mock_outbound_http):
    with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            responses_lib.GET,
            re.compile(r"https://translate\.googleapis\.com/.*"),
            status=500,
        )
        for _ in range(FAIL_THRESHOLD):
            _translate_text_google("שבת", "he", "en")

    assert sp.health.is_healthy("translate_google") is False


def test_get_halakhic_sources_fails_open_to_internal_ai_when_all_circuits_down(mock_outbound_http):
    """Total fallback guarantee (plan.md §3.2): with sefaria/web circuits open,
    get_halakhic_sources still returns a well-formed payload, never raising."""
    for _ in range(FAIL_THRESHOLD):
        sp.health.record_failure("sefaria")
        sp.health.record_failure("web")

    result = get_halakhic_sources("some obscure halachic question xyzzy")

    assert result["status"] == "internal-ai-needed"
    assert result["sources"]
