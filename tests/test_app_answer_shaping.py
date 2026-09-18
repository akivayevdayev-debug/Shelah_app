"""Behavior tests for the answer-shaping helpers in app.py.

These pure functions sit between the model and the UI: they recover a ruling
the model leaked as raw JSON, strip markdown noise, downgrade a spurious
"prohibited" flag, and backfill summary/steps when the asker wants detail.
The expectations below are written from the intended behavior (what a reader
should end up seeing), not from the implementation's incidental structure.
"""

from __future__ import annotations

import pytest

import app


class TestNumberedSteps:
    def test_numbered_clauses_become_capitalised_steps_without_punctuation(self):
        text = "First (1) wait until nightfall; (2) recite havdalah. ; (3) light a candle;"
        assert app._extract_numbered_ruling_steps(text, 5) == [
            "Wait until nightfall", "Recite havdalah", "Light a candle"]

    def test_limit_and_duplicates(self):
        text = "(1) do a; (2) do a; (3) do b; (4) do c"
        assert app._extract_numbered_ruling_steps(text, 3) == ["Do a", "Do b"]

    def test_no_numbering_yields_nothing(self):
        assert app._extract_numbered_ruling_steps("Just prose, no list.", 5) == []


class TestKeywordSteps:
    def test_only_fragments_with_guidance_keywords_are_kept(self):
        fragments = ["It is complicated.", "Consult your rabbi.", "The sky is blue.", "Verify the custom."]
        assert app._extract_keyword_ruling_steps(fragments, 5) == ["Consult your rabbi.", "Verify the custom."]

    def test_hebrew_keywords_count(self):
        assert app._extract_keyword_ruling_steps(["התייעץ עם רב."], 5) == ["התייעץ עם רב."]

    def test_stops_at_max_steps_and_dedupes(self):
        fragments = ["Ask a rabbi.", "Ask a rabbi.", "Check the label.", "Review the source."]
        assert app._extract_keyword_ruling_steps(fragments, 2) == ["Ask a rabbi.", "Check the label."]

    def test_fragments_are_truncated_to_180_characters(self):
        (step,) = app._extract_keyword_ruling_steps(["consult " + "x" * 400], 5)
        assert len(step) == 180


class TestActionSteps:
    def test_empty_ruling_has_no_steps(self):
        assert app._extract_action_steps_from_ruling("") == []
        assert app._extract_action_steps_from_ruling(None) == []

    def test_numbered_clauses_win_over_keyword_fragments(self):
        assert app._extract_action_steps_from_ruling("(1) rest; (2) pray; consult a rabbi.") == ["Rest", "Pray"]

    def test_keyword_strategy_used_when_there_is_no_numbering(self):
        text = "The matter is subtle. Consult your rabbi. Nothing else applies."
        assert app._extract_action_steps_from_ruling(text) == ["Consult your rabbi."]

    def test_falls_back_to_leading_fragments_when_no_keywords_match(self):
        text = "Alpha is so. Beta is so. Gamma is so."
        assert app._extract_action_steps_from_ruling(text, max_steps=2) == ["Alpha is so.", "Beta is so."]


class TestJsonishExtraction:
    @pytest.mark.parametrize("raw, expected", [
        ("", ""), (None, ""), ("a\\nb", "a b"), ("a\\tb", "a b"), ('say \\"hi\\"', 'say "hi"'),
        ("  many   spaces  ", "many spaces"),
    ])
    def test_decode_jsonish_text(self, raw, expected):
        assert app._decode_jsonish_text(raw) == expected

    def test_string_field_is_extracted_case_insensitively(self):
        text = '{"Ruling": "It is permitted.", "summary": "Short."}'
        assert app._extract_jsonish_string_field(text, "ruling") == "It is permitted."
        assert app._extract_jsonish_string_field(text, "summary") == "Short."

    def test_field_inside_an_escaped_json_string_is_found(self):
        text = '{\\"ruling\\": \\"Wait six hours.\\"}'
        assert app._extract_jsonish_string_field(text, "ruling") == "Wait six hours."

    @pytest.mark.parametrize("text, field", [("", "ruling"), ("no json here", "ruling"),
                                             ('{"ruling": ""}', "ruling"), ('{"a": "b"}', ""),
                                             ('{"other": "x"}', "ruling")])
    def test_missing_blank_or_unnamed_field_is_empty(self, text, field):
        assert app._extract_jsonish_string_field(text, field) == ""

    def test_field_name_is_regex_escaped(self):
        assert app._extract_jsonish_string_field('{"a.b": "hit"}', "a.b") == "hit"
        assert app._extract_jsonish_string_field('{"axb": "miss"}', "a.b") == ""

    def test_array_body_is_deduped_and_capped(self):
        assert app._parse_jsonish_array_body('"a", "b", "a", "c", "d"', 3) == ["a", "b", "c"]
        assert app._parse_jsonish_array_body('"", "  ", "x"', 5) == ["x"]

    def test_string_array_field_extraction(self):
        text = 'prefix {"practical_steps": ["Rest", "Pray", "Rest"], "x": 1}'
        assert app._extract_jsonish_string_array_field(text, "practical_steps") == ["Rest", "Pray"]
        assert app._extract_jsonish_string_array_field(text, "practical_steps", max_items=1) == ["Rest"]

    @pytest.mark.parametrize("text, field", [("", "practical_steps"), ('{"a": ["x"]}', ""),
                                             ('{"a": []}', "a"), ('{"b": ["x"]}', "a")])
    def test_array_field_absent_or_empty_yields_empty_list(self, text, field):
        assert app._extract_jsonish_string_array_field(text, field) == []


class TestLeakDetection:
    @pytest.mark.parametrize("text", ["```json\n{}", 'x "ruling": y', 'x "SUMMARY" y', "## Summary\nfoo",
                                      "## Practical Steps"])
    def test_leak_markers_are_recognised(self, text):
        assert app._looks_like_leaked_structured_payload(text)

    @pytest.mark.parametrize("text", ["", None, "It is permitted to light the candle."])
    def test_clean_text_is_not_flagged(self, text):
        assert not app._looks_like_leaked_structured_payload(text)


class TestStripStructuredNoise:
    def test_removes_code_fences_headings_and_bold_status_words(self):
        noisy = "```json\n## Ruling\nIt is **Prohibited** here.\n## Practical Steps\n```\n\n\n\nDone."
        assert app._strip_structured_noise(noisy) == "It is  here.\n\nDone."

    def test_blank_input_is_empty(self):
        assert app._strip_structured_noise("  ") == ""
        assert app._strip_structured_noise(None) == ""


class TestRecoverLeakedFields:
    RAW = '```json\n{"ruling": "Permitted after dark.", "summary": "Wait.", "practical_steps": ["Watch the sky", "Ask"]}\n```'

    def test_fields_are_pulled_out_of_a_leaked_json_answer(self):
        structured = {"ruling": "```json"}
        app._recover_leaked_structured_fields(structured, self.RAW)
        assert structured == {"ruling": "Permitted after dark.", "summary": "Wait.",
                              "practical_steps": ["Watch the sky", "Ask"]}

    def test_leak_inside_the_existing_ruling_is_also_recovered(self):
        structured = {"ruling": '{"ruling": "Inner ruling."}'}
        app._recover_leaked_structured_fields(structured, "plain raw answer")
        assert structured["ruling"] == "Inner ruling."

    def test_clean_structured_answers_are_left_alone(self):
        structured = {"ruling": "It is permitted.", "summary": "S"}
        app._recover_leaked_structured_fields(structured, "It is permitted.")
        assert structured == {"ruling": "It is permitted.", "summary": "S"}

    def test_a_leak_with_nothing_recoverable_changes_nothing(self):
        structured = {"ruling": "## Summary"}
        app._recover_leaked_structured_fields(structured, "## Summary")
        assert structured == {"ruling": "## Summary"}


class TestReevaluateProhibitionFlag:
    @pytest.mark.parametrize("ruling, still_prohibited", [
        ("Cooking on Yom Kippur is strictly forbidden.", True),
        ("This may not be done.", True),
        ("זה אסור בהחלט.", True),
        # flagged prohibited, but the text is permissive / only mentions melacha:
        ("It is permitted to walk; avoid melacha.", False),
        ("Resting is a mitzvah and an obligation.", False),
        ("Nothing here says anything definite.", False),
        # a direct prohibition alongside permission wording AND forbidden-work context is downgraded
        ("Forbidden work is not permitted, but walking is permitted.", False),
    ])
    def test_flag_follows_what_the_ruling_actually_asserts(self, ruling, still_prohibited):
        structured = {"is_prohibited": True}
        app._reevaluate_prohibition_flag(structured, ruling)
        assert structured["is_prohibited"] is still_prohibited

    def test_unflagged_or_empty_ruling_is_never_touched(self):
        unflagged = {"is_prohibited": False}
        app._reevaluate_prohibition_flag(unflagged, "This is forbidden.")
        assert unflagged["is_prohibited"] is False

        flagged = {"is_prohibited": True}
        app._reevaluate_prohibition_flag(flagged, "")
        assert flagged["is_prohibited"] is True


class TestDetailRequested:
    @pytest.mark.parametrize("question, mode, expected", [
        ("Can I light a candle?", "balanced", False),
        ("Please explain why", "balanced", True),
        ("How do I do this", None, True),
        ("הסבר לי", "", True),
        ("Can I light a candle?", " Sources ", True),
        ("Can I light a candle?", "STRICT", True),
        (None, None, False),
    ])
    def test_detail_is_requested_by_mode_or_by_wording(self, question, mode, expected):
        assert app._is_detail_requested(question, mode) is expected


class TestSummarizeRuling:
    def test_keeps_the_first_sentences_up_to_the_limit(self):
        assert app._summarize_ruling_text("One. Two! Three? Four.", max_sentences=2) == "One. Two!"

    def test_overlong_summary_is_cut_with_an_ellipsis(self):
        out = app._summarize_ruling_text("word " * 200, max_chars=20)
        assert out.endswith("...") and len(out) <= 23

    def test_blank_is_empty(self):
        assert app._summarize_ruling_text("   ") == ""


class TestResolveAndEnrich:
    def test_missing_structured_falls_back_to_parsing_the_raw_answer(self):
        result = {"answer": '{"ruling": "Parsed ruling.", "summary": "S", "practical_steps": ["a"]}'}
        resolved = app._resolve_clean_structured_answer(result, result["answer"])
        assert resolved["ruling"] == "Parsed ruling."

    def test_structured_with_a_ruling_is_kept_not_reparsed(self):
        result = {"structured": {"ruling": "Kept."}, "answer": "Other prose."}
        assert app._resolve_clean_structured_answer(result, result["answer"])["ruling"] == "Kept."

    def test_a_ruling_leaked_into_the_raw_answer_overrides_the_structured_one(self):
        raw = '{"ruling": "Recovered from the raw text."}'
        result = {"structured": {"ruling": "Stale."}, "answer": raw}
        assert app._resolve_clean_structured_answer(result, raw)["ruling"] == "Recovered from the raw text."

    def test_the_input_dict_is_copied_not_mutated(self):
        original = {"ruling": '{"ruling": "Recovered"}'}
        result = {"structured": original}
        app._resolve_clean_structured_answer(result, "")
        assert original == {"ruling": '{"ruling": "Recovered"}'}

    def test_detail_requests_backfill_summary_and_steps(self):
        structured = {"ruling": "First sentence. Second sentence. Consult your rabbi.",
                      "practical_steps": ["only one"]}
        out = app._enrich_structured_answer(structured, "Please explain", "balanced")
        assert out["summary"].startswith("First sentence. Second sentence.")
        assert out["practical_steps"] == ["Consult your rabbi."]

    def test_plain_questions_get_no_backfill_but_are_cleaned(self):
        structured = {"ruling": "```\nIt is fine.\n```", "summary": "", "practical_steps": "not a list"}
        out = app._enrich_structured_answer(structured, "Can I?", "balanced")
        assert out["ruling"] == "It is fine."
        assert not out.get("summary")
        assert out["practical_steps"] == "not a list"

    def test_existing_summary_is_cleaned_not_replaced_and_enough_steps_are_kept(self):
        structured = {"ruling": "R.", "summary": "## Summary\nKeep me", "practical_steps": ["a", "b"]}
        out = app._enrich_structured_answer(structured, "why?", "balanced")
        assert out["summary"] == "Keep me"
        assert out["practical_steps"] == ["a", "b"]

    def test_spurious_prohibition_flag_is_downgraded_during_enrichment(self):
        structured = {"ruling": "Resting is a mitzvah.", "is_prohibited": True}
        assert app._enrich_structured_answer(structured, "Can I?", "balanced")["is_prohibited"] is False


class TestCoerceAiAnswerShape:
    def test_non_dict_results_pass_through(self):
        assert app._coerce_ai_answer_shape("oops", "q", "balanced") == "oops"

    @pytest.mark.parametrize("error", ["security_blocked_input: x", "security_blocked_domain", "security_blocked_safety:y"])
    def test_security_blocked_results_are_returned_untouched(self, error):
        result = {"error": error, "answer": "blocked", "structured": {"ruling": "x"}}
        assert app._coerce_ai_answer_shape(dict(result), "q", "balanced") == result

    def test_detailed_structured_answer_is_rendered_into_the_english_sections(self):
        result = {"answer": "raw", "structured": {"ruling": "It is permitted.", "summary": "Short.",
                                                 "practical_steps": ["Rest"], "sources": ["Shulchan Arukh"]}}
        out = app._coerce_ai_answer_shape(result, "Can I?", "balanced")
        assert "## Direct Answer" in out["answer"]
        assert "It is permitted." in out["answer"]
        assert "**Practical Steps**" in out["answer"]
        assert "Shulchan Arukh" in out["answer"]
        assert out["structured"]["ruling"] == "It is permitted."

    def test_ruling_only_answers_render_as_just_the_ruling(self):
        result = {"answer": "raw", "structured": {"ruling": "It is permitted.", "practical_steps": [], "sources": []}}
        assert app._coerce_ai_answer_shape(result, "Can I?", "balanced")["answer"] == "It is permitted."

    def test_hebrew_answers_render_with_hebrew_headings(self):
        result = {"answer": "raw", "structured": {"ruling": "מותר.", "summary": "קצר.",
                                                 "practical_steps": ["נוח"], "sources": []}}
        out = app._coerce_ai_answer_shape(result, "שאלה", "balanced", answer_language="he")
        assert "## תשובה ישירה" in out["answer"]
        assert "**צעדים מעשיים**" in out["answer"]

    def test_a_leaked_json_answer_is_repaired_end_to_end(self):
        raw = '```json\n{"ruling": "Wait until three stars.", "summary": "Short.", "practical_steps": ["Look up"]}\n```'
        out = app._coerce_ai_answer_shape({"answer": raw}, "Can I?", "balanced")
        assert "```" not in out["answer"]
        assert "Wait until three stars." in out["answer"]
        assert out["structured"]["practical_steps"] == ["Look up"]

    def test_plain_text_answer_is_wrapped_as_the_ruling(self):
        out = app._coerce_ai_answer_shape({"answer": "Just prose."}, "Can I?", "balanced")
        assert out["structured"]["ruling"] == "Just prose."
        assert "Just prose." in out["answer"]


class TestDetectCommunity:
    def test_longest_alias_wins_then_canonical_names_then_none(self, monkeypatch):
        monkeypatch.setattr(app, "COMMUNITY_ALIASES", {"yemen": "Yemenite", "yemenite jews": "Yemenite Jews"})
        monkeypatch.setattr(app, "COMMUNITIES", {"Persian": {}})

        assert app._detect_community_in_text("customs of the Yemenite Jews") == "Yemenite Jews"
        assert app._detect_community_in_text("what about YEMEN") == "Yemenite"
        assert app._detect_community_in_text("Persian customs") == "Persian"
        assert app._detect_community_in_text("no community here") is None
        assert app._detect_community_in_text(None) is None


class TestPyluachHolidayFallback:
    @pytest.fixture(scope="class")
    def events(self):
        return app._build_pyluach_holiday_events(2026)

    def test_major_holidays_fall_on_their_known_2026_dates(self, events):
        by_date = {e["start"]: e["title"] for e in events}
        assert "Rosh Hashana" in by_date["2026-09-12"]
        assert "Yom Kippur" in by_date["2026-09-21"]
        assert "Pesach" in by_date["2026-04-02"]

    def test_events_are_all_day_major_and_chronological(self, events):
        assert len(events) > 20
        assert all(e["allDay"] and e["category"] == "major" for e in events)
        assert [e["start"] for e in events] == sorted(e["start"] for e in events)
        assert all(e["start"].startswith("2026-") for e in events)

    @pytest.mark.parametrize("year", ["not-a-year", None, 0, 99999])
    def test_an_invalid_year_yields_no_events_instead_of_raising(self, year):
        assert app._build_pyluach_holiday_events(year) == []
