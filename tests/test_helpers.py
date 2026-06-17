"""
Tests for backend/helpers.py — Hebrew text utilities, normalization, translation
(Google/MyMemory), Sefaria lexicon lookup, source-attribution formatting, and
community-name canonicalization.

`extract_ai_cited` already has route-level coverage via tests/test_ask.py and is
intentionally not duplicated here.

Network-touching functions (translation, Sefaria lexicon, dictionary lookup) rely
on the autouse `mock_outbound_http` fixture from conftest.py for their happy-path
responses. Failure/malformed-response paths use a nested `responses.RequestsMock`
context to override the outer fixture's registration for one specific domain.

Important: a nested RequestsMock does NOT fall back to the outer fixture's
registrations for URLs it doesn't itself register — it takes over interception
entirely for its scope. Any test that opens a nested block and expects a
fallback call to a DIFFERENT domain (e.g. Google fails -> code falls back to
MyMemory) must explicitly re-register that other domain inside the same nested
block too, or the fallback call has nothing to match and raises instead of
succeeding.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib
import requests

from backend import helpers


# ── Bounded cache ──────────────────────────────────────────────────────────────


class TestBoundedCacheSet:
    def test_sets_new_key(self):
        cache: dict = {}
        helpers._bounded_cache_set(cache, "a", 1)
        assert cache == {"a": 1}

    def test_overwrites_existing_key_without_eviction(self):
        cache = {"a": 1}
        helpers._bounded_cache_set(cache, "a", 2, maxsize=1)
        assert cache == {"a": 2}

    def test_evicts_oldest_when_full_and_key_is_new(self):
        cache = {"a": 1, "b": 2}
        helpers._bounded_cache_set(cache, "c", 3, maxsize=2)
        assert len(cache) == 2
        assert "a" not in cache
        assert cache.get("c") == 3

    def test_does_not_evict_when_under_maxsize(self):
        cache = {"a": 1}
        helpers._bounded_cache_set(cache, "b", 2, maxsize=5)
        assert cache == {"a": 1, "b": 2}


# ── Hebrew text utilities ────────────────────────────────────────────────────


class TestStripHebrewDiacritics:
    def test_strips_nikud_from_pointed_text(self):
        pointed = "בְּרֵאשִׁית"
        result = helpers._strip_hebrew_diacritics(pointed)
        assert result == "בראשית"

    def test_leaves_unpointed_text_unchanged(self):
        assert helpers._strip_hebrew_diacritics("שבת") == "שבת"

    def test_handles_none(self):
        assert helpers._strip_hebrew_diacritics(None) == ""

    def test_handles_empty_string(self):
        assert helpers._strip_hebrew_diacritics("") == ""

    def test_leaves_english_text_unchanged(self):
        assert helpers._strip_hebrew_diacritics("Shabbat") == "Shabbat"


class TestContainsHebrewLetters:
    def test_true_for_hebrew_word(self):
        assert helpers._contains_hebrew_letters("שבת") is True

    def test_true_for_mixed_text(self):
        assert helpers._contains_hebrew_letters("Shabbat שבת") is True

    def test_false_for_english_only(self):
        assert helpers._contains_hebrew_letters("Shabbat") is False

    def test_false_for_empty_string(self):
        assert helpers._contains_hebrew_letters("") is False

    def test_false_for_none(self):
        assert helpers._contains_hebrew_letters(None) is False

    def test_false_for_punctuation_and_numbers(self):
        assert helpers._contains_hebrew_letters("123 !@#") is False


class TestNormalizeLookupWord:
    def test_strips_diacritics_and_trims(self):
        assert helpers._normalize_lookup_word("  בְּרֵאשִׁית  ") == "בראשית"

    def test_collapses_internal_whitespace(self):
        assert helpers._normalize_lookup_word("שבת   שלום") == "שבת שלום"

    def test_handles_none(self):
        assert helpers._normalize_lookup_word(None) == ""

    def test_handles_plain_english(self):
        assert helpers._normalize_lookup_word("  hello   world  ") == "hello world"


class TestNormalizeGlossaryMeaning:
    def test_strips_short_alpha_prefix_before_comma(self):
        # "Shabbat" matches the alpha-prefix pattern, tail kept
        result = helpers._normalize_glossary_meaning("Shabbat, the seventh day of rest.")
        assert result == "the seventh day of rest."

    def test_keeps_full_text_when_prefix_not_alpha_only(self):
        # head "123" does not match [A-Za-z'\-\s]{2,40}, so whole text retained
        result = helpers._normalize_glossary_meaning("123, something else")
        assert result == "123, something else"

    def test_keeps_text_without_comma_unchanged(self):
        assert helpers._normalize_glossary_meaning("Truth.") == "Truth."

    def test_collapses_whitespace(self):
        assert helpers._normalize_glossary_meaning("  Truth   indeed  ") == "Truth indeed"

    def test_empty_value_returns_empty_string(self):
        assert helpers._normalize_glossary_meaning("") == ""
        assert helpers._normalize_glossary_meaning(None) == ""

    def test_long_head_before_comma_is_not_stripped(self):
        # head exceeds 40 chars, so the fullmatch fails and original text returned
        long_head = "a" * 41
        text = f"{long_head}, tail"
        assert helpers._normalize_glossary_meaning(text) == text

    def test_empty_tail_after_comma_keeps_original(self):
        # tail.strip() is falsy, so condition fails and original returned
        text = "Shabbat,   "
        result = helpers._normalize_glossary_meaning(text)
        assert result == "Shabbat,"


class TestLooksLikeTransliteration:
    def test_false_for_empty_string(self):
        assert helpers._looks_like_transliteration("") is False

    def test_false_for_none(self):
        assert helpers._looks_like_transliteration(None) is False

    def test_false_for_hebrew_text(self):
        assert helpers._looks_like_transliteration("שבת") is False

    def test_true_for_apostrophe_token(self):
        assert helpers._looks_like_transliteration("b'rosh") is True

    def test_true_for_hyphenated_token(self):
        assert helpers._looks_like_transliteration("kavod-melech") is True

    def test_true_for_short_phrase_with_translit_marker(self):
        # "Shabbat" contains "sh" marker, <= 3 tokens
        assert helpers._looks_like_transliteration("Shabbat") is True

    def test_true_for_translit_suffix_pattern(self):
        # single short token ending in vowel: "ach" -> Wait, must end in suffix list
        # use two tokens both ending in suffixes from translit_suffixes, <=2 tokens
        assert helpers._looks_like_transliteration("Shabbat Shalom") is True

    def test_true_for_single_short_vowel_ending_token(self):
        # len <= 4, ends in vowel
        assert helpers._looks_like_transliteration("ima") is True

    def test_false_for_plain_english_word(self):
        # "create" -- no apostrophe/hyphen, no translit marker matches within len<=3
        # tokens, doesn't end in vowel-suffix shortlist, len 6 > 4
        assert helpers._looks_like_transliteration("create") is False

    def test_false_for_too_long_phrase(self):
        long_phrase = "this is a very long phrase that exceeds the eighty char limit for sure yes indeed"
        assert helpers._looks_like_transliteration(long_phrase) is False

    def test_false_for_non_alpha_characters(self):
        assert helpers._looks_like_transliteration("hello123") is False


# ── Translation: Google ──────────────────────────────────────────────────────


class TestTranslateTextGoogle:
    def test_empty_text_returns_empty_string(self):
        assert helpers._translate_text_google("", "he", "en") == ""
        assert helpers._translate_text_google(None, "he", "en") == ""

    def test_success_returns_translated_text(self, mock_outbound_http):
        # autouse fixture mocks translate.googleapis.com -> ["mock translation"]
        result = helpers._translate_text_google("שבת", "he", "en")
        assert result == "mock translation"

    def test_upstream_failure_status_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                status=500,
            )
            assert helpers._translate_text_google("שבת", "he", "en") == ""

    def test_malformed_payload_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                json={"unexpected": "shape"},
                status=200,
            )
            assert helpers._translate_text_google("שבת", "he", "en") == ""

    def test_network_exception_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                body=requests.exceptions.ConnectionError("boom"),
            )
            assert helpers._translate_text_google("שבת", "he", "en") == ""

    def test_echo_translation_returns_empty_string(self, mock_outbound_http):
        # source == translated (case/space-insensitive) => treated as untranslated
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                json=[[["Hello", "Hello", None, None]]],
                status=200,
            )
            assert helpers._translate_text_google("Hello", "en", "he") == ""


class TestExtractGoogleTranslatedText:
    def test_extracts_and_joins_segments(self):
        # Each segment's first element is individually stripped before being
        # concatenated with no separator, so the space must live mid-chunk
        # (not at a chunk boundary) to survive into the joined result.
        payload = [[["Hello world", "שלום עולם"], ["!", "!"]]]
        assert helpers._extract_google_translated_text(payload) == "Hello world!"

    def test_concatenates_without_separator_between_chunks(self):
        # Leading/trailing whitespace at chunk boundaries is stripped per-chunk
        # before joining, so adjacent chunks butt up against each other.
        payload = [[["Hello ", "שלום"], ["world", "עולם"]]]
        assert helpers._extract_google_translated_text(payload) == "Helloworld"

    def test_returns_empty_for_non_list_payload(self):
        assert helpers._extract_google_translated_text({"a": 1}) == ""
        assert helpers._extract_google_translated_text(None) == ""

    def test_returns_empty_for_empty_payload(self):
        assert helpers._extract_google_translated_text([]) == ""

    def test_returns_empty_when_segments_not_a_list(self):
        assert helpers._extract_google_translated_text(["not-a-list"]) == ""

    def test_skips_empty_chunks(self):
        payload = [[["", "x"], ["real text", "y"]]]
        assert helpers._extract_google_translated_text(payload) == "real text"


class TestIsTranslationEcho:
    def test_true_for_identical_text(self):
        assert helpers._is_translation_echo("Hello", "hello") is True

    def test_true_ignoring_whitespace_differences(self):
        assert helpers._is_translation_echo("Hello  World", "hello world") is True

    def test_false_for_different_text(self):
        assert helpers._is_translation_echo("Hello", "Shalom") is False

    def test_false_when_either_side_empty(self):
        assert helpers._is_translation_echo("", "") is False
        assert helpers._is_translation_echo("Hello", "") is False


# ── Translation: MyMemory ────────────────────────────────────────────────────


class TestTranslateTextMymemory:
    def test_empty_text_returns_empty_string(self):
        assert helpers._translate_text_mymemory("", "he", "en") == ""

    def test_success_returns_translated_text(self, mock_outbound_http):
        result = helpers._translate_text_mymemory("שבת", "he", "en")
        assert result == "mock translation"

    def test_upstream_failure_status_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                status=503,
            )
            assert helpers._translate_text_mymemory("שבת", "he", "en") == ""

    def test_missing_response_data_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                json={"responseStatus": 200},
                status=200,
            )
            assert helpers._translate_text_mymemory("שבת", "he", "en") == ""

    def test_network_exception_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                body=requests.exceptions.Timeout("timed out"),
            )
            assert helpers._translate_text_mymemory("שבת", "he", "en") == ""

    def test_echo_translation_returns_empty_string(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                json={"responseData": {"translatedText": "Hello"}, "responseStatus": 200},
                status=200,
            )
            assert helpers._translate_text_mymemory("Hello", "en", "he") == ""


# ── Hebrew/English translate wrappers (Google-first, MyMemory-fallback) ──────


class TestTranslateHebrewTextGoogle:
    def test_returns_empty_for_non_hebrew_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_google("Shabbat") == ""

    def test_returns_empty_for_empty_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_google("") == ""

    def test_success_for_hebrew_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_google("שבת") == "mock translation"


class TestTranslateHebrewTextMymemory:
    def test_returns_empty_for_non_hebrew_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_mymemory("Shabbat") == ""

    def test_success_for_hebrew_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_mymemory("שבת") == "mock translation"


class TestTranslateHebrewTextOnline:
    def test_returns_empty_tuple_for_non_hebrew_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_online("hello") == ("", "")

    def test_returns_empty_tuple_for_empty_text(self, mock_outbound_http):
        assert helpers._translate_hebrew_text_online("") == ("", "")

    def test_success_uses_google_first(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        translated, source = helpers._translate_hebrew_text_online("שבת")
        assert translated == "mock translation"
        assert source == "google-translate"

    def test_result_is_cached_on_second_call(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        first = helpers._translate_hebrew_text_online("תורה")
        # Disable Google entirely; if caching works, second call doesn't need network
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                body=requests.exceptions.ConnectionError("no network"),
            )
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                body=requests.exceptions.ConnectionError("no network"),
            )
            second = helpers._translate_hebrew_text_online("תורה")
        assert second == first

    def test_falls_back_to_mymemory_when_google_fails(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                status=500,
            )
            # Must re-register MyMemory here too — this nested mock replaces
            # the outer autouse fixture's registrations entirely, it doesn't
            # layer on top of them.
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                json={"responseData": {"translatedText": "mock translation", "match": 1.0}, "responseStatus": 200},
                status=200,
            )
            translated, source = helpers._translate_hebrew_text_online("שלום")
        assert translated == "mock translation"
        assert source == "mymemory-translate"

    def test_both_providers_fail_returns_empty_tuple(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                status=500,
            )
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                status=500,
            )
            result = helpers._translate_hebrew_text_online("חסד")
        assert result == ("", "")


class TestTranslateEnglishTextOnline:
    def test_returns_empty_tuple_for_empty_text(self, mock_outbound_http):
        assert helpers._translate_english_text_online("") == ("", "")

    def test_success_uses_google_first(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        translated, source = helpers._translate_english_text_online("peace")
        assert translated == "mock translation"
        assert source == "google-translate"

    def test_falls_back_to_mymemory_on_echo(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            # Google echoes input back, triggering MyMemory fallback
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://translate\.googleapis\.com/.*"),
                json=[[["peace", "peace"]]],
                status=200,
            )
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.mymemory\.translated\.net/.*"),
                json={"responseData": {"translatedText": "mock translation", "match": 1.0}, "responseStatus": 200},
                status=200,
            )
            translated, source = helpers._translate_english_text_online("peace")
        assert translated == "mock translation"
        assert source == "mymemory-translate"

    def test_result_is_cached_with_en_he_prefix(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        helpers._translate_english_text_online("love")
        assert "en-he::love" in helpers.TRANSLATION_CACHE


# ── Sefaria BDB/Jastrow lexicon ──────────────────────────────────────────────


class TestLookupSefariaLexicon:
    def test_returns_empty_for_non_hebrew_word(self, mock_outbound_http):
        assert helpers._lookup_sefaria_lexicon("hello") == ("", "")

    def test_returns_empty_for_empty_word(self, mock_outbound_http):
        assert helpers._lookup_sefaria_lexicon("") == ("", "")

    def test_success_returns_definition_and_lexicon_name(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[
                    {
                        "lexicon_name": "BDB Dictionary",
                        "content": {"definitions": [{"definition": "to create, fashion"}]},
                    }
                ],
                status=200,
            )
            definition, lex_name = helpers._lookup_sefaria_lexicon("ברא")
        assert definition == "to create, fashion"
        assert lex_name == "BDB Dictionary"

    def test_prefers_bdb_over_other_lexicon(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[
                    {
                        "lexicon_name": "Some Other Lexicon",
                        "content": {"definitions": [{"definition": "other definition"}]},
                    },
                    {
                        "lexicon_name": "Brown-Driver-Briggs",
                        "content": {"definitions": [{"definition": "preferred definition"}]},
                    },
                ],
                status=200,
            )
            definition, lex_name = helpers._lookup_sefaria_lexicon("שלום")
        assert definition == "preferred definition"
        assert lex_name == "Brown-Driver-Briggs"

    def test_strips_html_tags_from_definition(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[
                    {
                        "lexicon_name": "Jastrow",
                        "content": {"definitions": [{"definition": "<b>bold</b> meaning"}]},
                    }
                ],
                status=200,
            )
            definition, _ = helpers._lookup_sefaria_lexicon("אהבה")
        assert definition == "bold meaning"
        assert "<b>" not in definition

    def test_falls_back_to_content_definition_field(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[
                    {
                        "lexicon_name": "sefaria",
                        "content": {"definition": "fallback content definition"},
                    }
                ],
                status=200,
            )
            definition, _ = helpers._lookup_sefaria_lexicon("חסד")
        assert definition == "fallback content definition"

    def test_upstream_failure_status_returns_empty_and_caches(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                status=404,
            )
            definition, lex_name = helpers._lookup_sefaria_lexicon("ירושלים")
        assert (definition, lex_name) == ("", "")
        # cached as empty
        assert helpers.TRANSLATION_CACHE.get("sefaria-lex::ירושלים") == ""

    def test_non_list_payload_returns_empty(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json={"unexpected": "dict-not-list"},
                status=200,
            )
            assert helpers._lookup_sefaria_lexicon("אמת") == ("", "")

    def test_no_candidates_returns_empty(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[{"lexicon_name": "jastrow", "content": {}}],
                status=200,
            )
            assert helpers._lookup_sefaria_lexicon("יראה") == ("", "")

    def test_network_exception_returns_empty(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                body=requests.exceptions.ConnectionError("boom"),
            )
            assert helpers._lookup_sefaria_lexicon("מנהג") == ("", "")

    def test_result_is_cached_on_second_call(self, mock_outbound_http):
        helpers.TRANSLATION_CACHE.clear()
        helpers.TRANSLATION_SOURCE_CACHE.clear()
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://www\.sefaria\.org/api/words/.*"),
                json=[
                    {
                        "lexicon_name": "bdb",
                        "content": {"definitions": [{"definition": "cached definition"}]},
                    }
                ],
                status=200,
            )
            first = helpers._lookup_sefaria_lexicon("ברכה")
        # second call should hit cache, no network mock needed at all
        second = helpers._lookup_sefaria_lexicon("ברכה")
        assert second == first == ("cached definition", "bdb")


# ── English dictionary lookup ─────────────────────────────────────────────────


class TestLookupEnglishWordMeaning:
    def test_returns_empty_for_blank_word(self, mock_outbound_http):
        assert helpers._lookup_english_word_meaning("") == ("", "")
        assert helpers._lookup_english_word_meaning("   ") == ("", "")

    def test_success_returns_definition(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                json=[
                    {
                        "word": "peace",
                        "meanings": [
                            {"definitions": [{"definition": "freedom from disturbance"}]}
                        ],
                    }
                ],
                status=200,
            )
            definition, source = helpers._lookup_english_word_meaning("peace")
        assert definition == "freedom from disturbance"
        assert source == "dictionaryapi.dev"

    def test_upstream_failure_status_returns_empty(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                status=404,
            )
            assert helpers._lookup_english_word_meaning("xyzzy") == ("", "")

    def test_non_list_payload_returns_empty(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                json={"title": "No Definitions Found"},
                status=200,
            )
            assert helpers._lookup_english_word_meaning("xyzzy") == ("", "")

    def test_empty_meanings_returns_empty(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                json=[{"word": "x", "meanings": []}],
                status=200,
            )
            assert helpers._lookup_english_word_meaning("x") == ("", "")

    def test_network_exception_returns_empty(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                body=requests.exceptions.ConnectionError("boom"),
            )
            assert helpers._lookup_english_word_meaning("peace") == ("", "")

    def test_lowercases_word_before_lookup(self, mock_outbound_http):
        captured = {}

        def _callback(request):
            captured["url"] = request.url
            return (200, {}, '[{"word": "shalom", "meanings": []}]')

        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add_callback(
                responses_lib.GET,
                re.compile(r"https://api\.dictionaryapi\.dev/.*"),
                callback=_callback,
            )
            helpers._lookup_english_word_meaning("SHALOM")
        assert "shalom" in captured["url"]


# ── Hebrew word variant helpers ───────────────────────────────────────────────


class TestHebrewWordVariantCandidates:
    def test_returns_empty_list_for_non_hebrew(self):
        assert helpers._hebrew_word_variant_candidates("hello") == []

    def test_returns_empty_list_for_blank(self):
        assert helpers._hebrew_word_variant_candidates("") == []

    def test_single_word_no_prefix_returns_word_itself(self):
        # "שלום" len 4, first letter ש is a prefix letter -> also adds stripped variant
        result = helpers._hebrew_word_variant_candidates("שלום")
        assert "שלום" in result

    def test_strips_vav_prefix_when_length_at_least_four(self):
        # "ושמרתם" starts with ו and length >= 4
        result = helpers._hebrew_word_variant_candidates("ושמרתם")
        assert "ושמרתם" in result
        assert "שמרתם" in result

    def test_short_word_not_stripped(self):
        # "ול" len 2 < 4, no prefix-stripped variant added
        result = helpers._hebrew_word_variant_candidates("ול")
        assert result == ["ול"]

    def test_multi_word_phrase_adds_first_token(self):
        result = helpers._hebrew_word_variant_candidates("שבת שלום")
        assert "שבת שלום" in result
        assert "שבת" in result

    def test_caps_at_four_variants(self):
        result = helpers._hebrew_word_variant_candidates("ושמרתם הברכה")
        assert len(result) <= 4

    def test_strips_diacritics_before_processing(self):
        result = helpers._hebrew_word_variant_candidates("בְּרֵאשִׁית")
        assert "בראשית" in result


class TestParseMeaningCandidates:
    def test_returns_empty_list_for_blank(self):
        assert helpers._parse_meaning_candidates("") == []
        assert helpers._parse_meaning_candidates(None) == []

    def test_single_value_no_separator_returns_single_item_list(self):
        assert helpers._parse_meaning_candidates("Truth") == ["Truth"]

    def test_splits_on_semicolon(self):
        result = helpers._parse_meaning_candidates("create; fashion; make")
        assert result == ["create", "fashion", "make"]

    def test_splits_on_slash(self):
        result = helpers._parse_meaning_candidates("go/walk/proceed")
        assert result == ["go", "walk", "proceed"]

    def test_splits_on_pipe(self):
        result = helpers._parse_meaning_candidates("say|speak|declare")
        assert result == ["say", "speak", "declare"]

    def test_strips_trailing_periods_and_spaces(self):
        result = helpers._parse_meaning_candidates("create. ; fashion. ")
        assert result == ["create", "fashion"]

    def test_caps_at_four_candidates(self):
        result = helpers._parse_meaning_candidates("a;b;c;d;e;f")
        assert len(result) <= 4
        assert result == ["a", "b", "c", "d"]

    def test_collapses_internal_whitespace(self):
        result = helpers._parse_meaning_candidates("create   well")
        assert result == ["create well"]


# ── Source attribution helpers ────────────────────────────────────────────────


class TestJoinWithAnd:
    def test_empty_list_returns_empty_string(self):
        assert helpers._join_with_and([]) == ""

    def test_single_item(self):
        assert helpers._join_with_and(["Sefaria"]) == "Sefaria"

    def test_two_items(self):
        assert helpers._join_with_and(["Sefaria", "Customs"]) == "Sefaria and Customs"

    def test_three_or_more_items_uses_oxford_comma(self):
        result = helpers._join_with_and(["Sefaria", "Customs", "Web"])
        assert result == "Sefaria, Customs, and Web"


class TestBuildSourceAttributionNote:
    def test_no_sources_returns_internal_knowledge_disclaimer(self):
        result = helpers._build_source_attribution_note()
        assert result == helpers.INTERNAL_AI_KNOWLEDGE_DISCLAIMER

    def test_internal_knowledge_flag_overrides_other_sources(self):
        result = helpers._build_source_attribution_note(
            has_sefaria=True, has_internal_knowledge=True
        )
        assert result == helpers.INTERNAL_AI_KNOWLEDGE_DISCLAIMER

    def test_single_source_sefaria(self):
        result = helpers._build_source_attribution_note(has_sefaria=True)
        assert "Sefaria" in result
        assert helpers.RABBI_FINAL_RULING_FOOTER in result

    def test_multiple_sources_joined_with_and(self):
        result = helpers._build_source_attribution_note(
            has_sefaria=True, has_customs=True, has_general_web=True
        )
        assert "Sefaria, Community Customs, and General Web Context" in result

    def test_whitelisted_external_source_label(self):
        result = helpers._build_source_attribution_note(has_whitelisted_external=True)
        assert "Halachipedia / HebrewBooks / YHB" in result


class TestCompactAiSources:
    def test_non_list_input_returns_empty_list(self):
        assert helpers._compact_ai_sources(None) == []
        assert helpers._compact_ai_sources("not-a-list") == []

    def test_empty_list_returns_empty_list(self):
        assert helpers._compact_ai_sources([]) == []

    def test_basic_source_is_compacted(self):
        sources = [
            {
                "ref": "Genesis 1:1",
                "title": "Genesis",
                "lines": [{"en": "In the beginning", "he": "בְּרֵאשִׁית"}],
            }
        ]
        result = helpers._compact_ai_sources(sources)
        assert len(result) == 1
        assert result[0]["ref"] == "Genesis 1:1"
        assert result[0]["title"] == "Genesis"
        assert result[0]["lines"] == [{"en": "In the beginning", "he": "בְּרֵאשִׁית"}]

    def test_skips_non_dict_entries(self):
        sources = [{"ref": "A", "title": "A", "lines": []}, "not-a-dict", None]
        result = helpers._compact_ai_sources(sources)
        # "not-a-dict"/None skipped; first has no valid lines + empty lines list, so also skipped
        assert all(isinstance(item, dict) for item in result)

    def test_title_falls_back_to_ref_when_missing(self):
        sources = [{"ref": "Genesis 1:1", "lines": [{"en": "text", "he": ""}]}]
        result = helpers._compact_ai_sources(sources)
        assert result[0]["title"] == "Genesis 1:1"

    def test_skips_text_not_found_lines(self):
        sources = [
            {
                "ref": "Foo",
                "title": "Foo",
                "lines": [{"en": "Text not found in source", "he": ""}],
            }
        ]
        result = helpers._compact_ai_sources(sources)
        # No valid content -> source dropped entirely
        assert result == []

    def test_skips_error_prefixed_lines(self):
        sources = [
            {"ref": "Foo", "title": "Foo", "lines": [{"en": "Error: timeout", "he": ""}]}
        ]
        result = helpers._compact_ai_sources(sources)
        assert result == []

    def test_truncates_long_text_with_ellipsis(self):
        long_en = "x" * 400
        sources = [{"ref": "Foo", "title": "Foo", "lines": [{"en": long_en, "he": ""}]}]
        result = helpers._compact_ai_sources(sources, max_chars=280)
        assert len(result[0]["lines"][0]["en"]) == 283  # 280 + "..."
        assert result[0]["lines"][0]["en"].endswith("...")

    def test_truncation_never_leaves_a_dangling_html_tag(self):
        """Regression: HTML must be stripped BEFORE truncating, not after.

        Sefaria text often embeds footnote/commentary markup. Truncating the
        raw string by character count first risked slicing a tag in half
        (e.g. cutting "<i data-commentary-link=...>" mid-attribute), leaving
        an unclosed fragment like "<i data-com" with no closing ">" — which
        the frontend's tag-stripper (requires a literal ">") can't remove,
        so it rendered as visible garbage text in the source box.
        """
        prefix = "A" * 270
        raw_en = (
            prefix + ' text <i data-commentary-link="Rashi on Genesis">'
            "with footnote</i> more text after"
        )
        sources = [{"ref": "Genesis 1:1", "lines": [{"en": raw_en, "he": ""}]}]
        result = helpers._compact_ai_sources(sources, max_chars=280)
        en_out = result[0]["lines"][0]["en"]
        assert "<" not in en_out
        assert ">" not in en_out

    def test_html_stripped_from_short_text_too(self):
        sources = [{"ref": "Foo", "lines": [{"en": "plain <b>bold</b> text", "he": "<i>טקסט</i>"}]}]
        result = helpers._compact_ai_sources(sources)
        assert result[0]["lines"][0]["en"] == "plain bold text"
        assert result[0]["lines"][0]["he"] == "טקסט"

    def test_caps_at_max_sources(self):
        sources = [
            {"ref": f"S{i}", "title": f"S{i}", "lines": [{"en": "text", "he": ""}]}
            for i in range(20)
        ]
        result = helpers._compact_ai_sources(sources, max_sources=5)
        assert len(result) == 5

    def test_caps_lines_at_max_lines(self):
        many_lines = [{"en": f"line {i}", "he": ""} for i in range(10)]
        sources = [{"ref": "Foo", "title": "Foo", "lines": many_lines}]
        result = helpers._compact_ai_sources(sources, max_lines=3)
        assert len(result[0]["lines"]) == 3

    def test_includes_optional_domain_and_url_when_present(self):
        sources = [
            {
                "ref": "Foo",
                "title": "Foo",
                "lines": [{"en": "text", "he": ""}],
                "domain": "halachipedia.com",
                "source_provider": "Halachipedia",
                "url": "https://halachipedia.com/foo",
            }
        ]
        result = helpers._compact_ai_sources(sources)
        assert result[0]["domain"] == "halachipedia.com"
        assert result[0]["source_provider"] == "Halachipedia"
        assert result[0]["url"] == "https://halachipedia.com/foo"

    def test_omits_optional_fields_when_absent(self):
        sources = [{"ref": "Foo", "title": "Foo", "lines": [{"en": "text", "he": ""}]}]
        result = helpers._compact_ai_sources(sources)
        assert "domain" not in result[0]
        assert "source_provider" not in result[0]
        assert "url" not in result[0]

    def test_non_list_lines_field_treated_as_empty(self):
        sources = [{"ref": "Foo", "title": "Foo", "lines": "not-a-list"}]
        result = helpers._compact_ai_sources(sources)
        assert result == []

    def test_skips_non_dict_line_rows(self):
        sources = [{"ref": "Foo", "title": "Foo", "lines": ["not-a-dict", {"en": "ok", "he": ""}]}]
        result = helpers._compact_ai_sources(sources)
        assert len(result[0]["lines"]) == 1
        assert result[0]["lines"][0]["en"] == "ok"


# ── extract_ai_cited — only the uncovered edge not exercised by test_ask.py ──


class TestExtractAiCitedEdgeCases:
    """extract_ai_cited's happy path is covered via route tests in test_ask.py;
    this covers the remaining defensive branches directly."""

    def test_non_dict_payload_returns_empty_list(self):
        assert helpers.extract_ai_cited(None) == []
        assert helpers.extract_ai_cited("not-a-dict") == []
        assert helpers.extract_ai_cited([1, 2, 3]) == []

    def test_missing_sources_key_returns_empty_list(self):
        assert helpers.extract_ai_cited({}) == []

    def test_filters_out_blank_entries(self):
        result = helpers.extract_ai_cited({"sources": ["Genesis 1:1", "", "  ", None]})
        assert result == ["Genesis 1:1"]

    def test_coerces_non_string_entries_to_string(self):
        result = helpers.extract_ai_cited({"sources": [123, "Exodus 1:1"]})
        assert result == ["123", "Exodus 1:1"]


# ── Community name canonicalizer ──────────────────────────────────────────────


class TestCanonicalizeCommunityName:
    def test_none_or_empty_returns_none(self):
        assert helpers._canonicalize_community_name(None) is None
        assert helpers._canonicalize_community_name("") is None

    def test_exact_canonical_match_returned_as_is(self):
        assert helpers._canonicalize_community_name("Ashkenaz") == "Ashkenaz"

    def test_lowercase_alias_resolves_to_canonical(self):
        assert helpers._canonicalize_community_name("ashkenazi") == "Ashkenaz"
        assert helpers._canonicalize_community_name("sephardic") == "Sefardic"

    def test_alias_with_surrounding_whitespace(self):
        assert helpers._canonicalize_community_name("  yemenite  ") == "Yemenite"

    def test_alias_with_different_punctuation_normalizes(self):
        # "mountain jewish" alias vs "mountain-jewish-kavkazi" canonical-key match
        assert helpers._canonicalize_community_name("Mountain Jewish") == "Kavkazi"

    def test_alnum_normalized_alias_match(self):
        # "turkish ottoman sefardic" normalizes to match alias key
        result = helpers._canonicalize_community_name("Turkish Ottoman-Sefardic")
        assert result == "Turkish-Ottoman"

    def test_alnum_normalized_canonical_match(self):
        # falls through to canonical-key normalized match
        result = helpers._canonicalize_community_name("greekromaniote")
        assert result == "Greek-Romaniote"

    def test_unknown_name_returns_none(self):
        assert helpers._canonicalize_community_name("Atlantean") is None

    def test_case_insensitive_canonical_name(self):
        assert helpers._canonicalize_community_name("moroccan") == "Moroccan"
