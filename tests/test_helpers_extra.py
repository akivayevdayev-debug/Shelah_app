"""
Supplementary coverage tests for backend/helpers.py, targeting branches
tests/test_helpers.py doesn't reach: _looks_like_transliteration edge cases,
_decode_route_ref no-op decode, translation-cache hit/echo-clear branches,
the full _lookup_hebrew_word_meaning fallback chain, _collect_word_meaning_alternatives
branches, _fill_missing_english_lines translation loop, _compact_ai_sources
long-line truncation, and _canonicalize_community_name's canonical-name match.

Lower-level network-touching dependencies (_lookup_sefaria_lexicon,
_translate_hebrew_text_online, _translate_text_google/_mymemory) are
monkeypatched directly rather than mocked at the HTTP layer, since these
tests target orchestration/branching logic in the callers, not the
lower-level functions themselves (already covered elsewhere).
"""

from __future__ import annotations

import pytest

import backend.helpers as helpers


class TestLooksLikeTransliterationEdgeCases:
    def test_empty_after_normalization_returns_false(self):
        assert helpers._looks_like_transliteration("   ") is False

    def test_single_short_vowel_ending_token_is_transliteration(self):
        assert helpers._looks_like_transliteration("amah") is True

    def test_non_matching_fullmatch_returns_false(self):
        assert helpers._looks_like_transliteration("word123!") is False


class TestDecodeRouteRef:
    def test_plain_value_returned_unchanged(self):
        assert helpers._decode_route_ref("Genesis 1:1") == "Genesis 1:1"

    def test_double_encoded_value_fully_decoded(self):
        assert helpers._decode_route_ref("Genesis%2520Chapter") == "Genesis Chapter"


class TestTranslateEnglishTextOnlineCache:
    def test_cache_hit_skips_network(self, monkeypatch):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        cache_key = "en-he::rest"
        helpers.TRANSLATION_CACHE[cache_key] = "מנוחה"
        helpers.TRANSLATION_SOURCE_CACHE[cache_key] = "google-translate"

        def _raise(*a, **k):
            raise AssertionError("should not call network on cache hit")

        monkeypatch.setattr(helpers, "_translate_text_google", _raise)
        result, source = helpers._translate_english_text_online("rest")
        assert result == "מנוחה"
        assert source == "google-translate"
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()

    def test_echo_result_cleared_to_empty(self, monkeypatch):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        monkeypatch.setattr(helpers, "_translate_text_google", lambda v, s, t: "sameword")
        monkeypatch.setattr(helpers, "_translate_text_mymemory", lambda v, s, t: "sameword")
        monkeypatch.setattr(helpers, "_is_translation_echo", lambda a, b: True)
        result, source = helpers._translate_english_text_online("sameword")
        assert result == ""
        assert source == ""
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()


class TestLookupHebrewWordMeaningFullChain:
    def test_empty_word_returns_empty(self):
        assert helpers._lookup_hebrew_word_meaning("") == ("", "")

    def test_local_glossary_hit(self):
        meaning, source = helpers._lookup_hebrew_word_meaning("שבת")
        assert source == "local-hebrew-glossary"
        assert "rest" in meaning.lower()

    def test_falls_through_to_sefaria_lexicon(self, monkeypatch):
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("Lexicon meaning", "sefaria-lexicon"))
        meaning, source = helpers._lookup_hebrew_word_meaning("לגמגם")
        assert meaning == "Lexicon meaning"
        assert source == "sefaria-lexicon"

    def test_falls_through_to_online_translation(self, monkeypatch):
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("", ""))
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda variant: ("A translated meaning", "google-translate"))
        monkeypatch.setattr(helpers, "_looks_like_transliteration", lambda text: False)
        meaning, _ = helpers._lookup_hebrew_word_meaning("לגמגם")
        assert meaning == "A translated meaning"

    def test_all_sources_fail_returns_empty(self, monkeypatch):
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("", ""))
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda variant: ("", ""))
        meaning, _ = helpers._lookup_hebrew_word_meaning("לגמגם")
        assert meaning == ""

    def test_multi_word_hebrew_input_generates_prefix_variants(self, monkeypatch):
        seen_variants = []

        def fake_lexicon(variant):
            seen_variants.append(variant)
            return ("", "")

        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", fake_lexicon)
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda v: ("", ""))
        # Neither word nor its stripped-prefix form is a glossary key, so the
        # local-glossary loop can't short-circuit before the lexicon loop runs.
        helpers._lookup_hebrew_word_meaning("ולגמגם המדברים")
        assert len(seen_variants) > 1


class TestCollectWordMeaningAlternatives:
    def test_hebrew_word_uses_interpretive_glossary(self, monkeypatch):
        monkeypatch.setattr(helpers, "_hebrew_word_variant_candidates", lambda raw: ["ברא"])
        monkeypatch.setattr(helpers, "_parse_meaning_candidates", lambda meaning: [])
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("", ""))
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda variant: ("", ""))
        result = helpers._collect_word_meaning_alternatives(
            raw_word="ברא", primary_meaning="create", word_is_hebrew=True,
        )
        assert "create" in result or "fashion" in result

    def test_transliteration_option_skipped_for_hebrew(self, monkeypatch):
        monkeypatch.setattr(helpers, "_hebrew_word_variant_candidates", lambda raw: [])
        monkeypatch.setattr(helpers, "_parse_meaning_candidates", lambda meaning: ["amah"])
        result = helpers._collect_word_meaning_alternatives(
            raw_word="x", primary_meaning="amah", word_is_hebrew=True,
        )
        assert "amah" not in result

    def test_duplicate_options_deduped_case_insensitively(self, monkeypatch):
        monkeypatch.setattr(helpers, "_hebrew_word_variant_candidates", lambda raw: [])
        monkeypatch.setattr(helpers, "_parse_meaning_candidates", lambda meaning: ["Rest", "rest"])
        result = helpers._collect_word_meaning_alternatives(
            raw_word="x", primary_meaning="Rest", word_is_hebrew=True,
        )
        assert result.count("Rest") == 1

    def test_lexicon_fallback_to_translation_when_few_options(self, monkeypatch):
        monkeypatch.setattr(helpers, "_hebrew_word_variant_candidates", lambda raw: ["שבת"])
        monkeypatch.setattr(helpers, "_parse_meaning_candidates", lambda meaning: [])
        monkeypatch.setattr(helpers, "_lookup_sefaria_lexicon", lambda variant: ("", ""))
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda variant: ("Rest day", "google"))
        result = helpers._collect_word_meaning_alternatives(
            raw_word="שבת", primary_meaning="", word_is_hebrew=True,
        )
        assert "Rest day" in result

    def test_non_hebrew_word_only_uses_parsed_candidates(self, monkeypatch):
        monkeypatch.setattr(helpers, "_parse_meaning_candidates", lambda meaning: ["definition one", "definition two"])
        result = helpers._collect_word_meaning_alternatives(
            raw_word="rest", primary_meaning="a period of inactivity", word_is_hebrew=False,
        )
        assert "definition one" in result


class TestFillMissingEnglishLines:
    def test_non_dict_payload_returned_unchanged(self):
        assert helpers._fill_missing_english_lines("not a dict") == "not a dict"

    def test_no_lines_key_returned_unchanged(self):
        payload = {"ref": "Genesis 1:1"}
        assert helpers._fill_missing_english_lines(payload) == payload

    def test_translates_missing_english_lines(self, monkeypatch):
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda text: ("Translated text", "google-translate"))
        payload = {
            "lines": [{"he": "טקסט בעברית", "en": ""}],
        }
        result = helpers._fill_missing_english_lines(payload)
        assert result["translation_generated"] is True
        assert result["translation_generated_count"] == 1
        assert "google-translate" in result["translation_source"]
        assert result["lines"][0]["en"] == "Translated text"

    def test_skips_lines_that_already_have_english(self, monkeypatch):
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda text: (_ for _ in ()).throw(AssertionError("should not be called")))
        payload = {"lines": [{"he": "טקסט", "en": "Already translated"}]}
        result = helpers._fill_missing_english_lines(payload)
        assert "translation_generated" not in result

    def test_skips_lines_without_hebrew_letters(self, monkeypatch):
        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", lambda text: (_ for _ in ()).throw(AssertionError("should not be called")))
        payload = {"lines": [{"he": "123", "en": ""}]}
        result = helpers._fill_missing_english_lines(payload)
        assert "translation_generated" not in result

    def test_respects_max_lines_limit(self, monkeypatch):
        call_count = {"n": 0}

        def fake_translate(text):
            call_count["n"] += 1
            return "translated", "google"

        monkeypatch.setattr(helpers, "_translate_hebrew_text_online", fake_translate)
        payload = {"lines": [{"he": "טקסט", "en": ""} for _ in range(5)]}
        helpers._fill_missing_english_lines(payload, max_lines=2)
        assert call_count["n"] == 2

    def test_non_dict_line_skipped(self):
        payload = {"lines": ["not a dict"]}
        result = helpers._fill_missing_english_lines(payload)
        assert "translation_generated" not in result


class TestCompactAiSourcesLongHebrewTruncation:
    def test_long_hebrew_line_truncated(self):
        sources = [{
            "ref": "Genesis 1:1",
            "lines": [{"en": "short", "he": "א" * 500}],
        }]
        result = helpers._compact_ai_sources(sources, max_chars=50)
        assert result[0]["lines"][0]["he"].endswith("...")
        assert len(result[0]["lines"][0]["he"]) <= 53


class TestCanonicalizeCommunityNameExactMatch:
    def test_exact_canonical_name_normalized_case(self):
        result = helpers._canonicalize_community_name("ASHKENAZ")
        assert result == "Ashkenaz"
