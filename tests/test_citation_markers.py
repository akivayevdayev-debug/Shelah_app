"""Numbered source markers: the model's [n] tags are renumbered to match the
cleaned source list, and a ref spelled "Hilchot Shabbat" resolves to Sefaria's
"Mishneh Torah, Sabbath"."""

import pytest

from backend import claude
from backend.citation_markers import (
    MAX_CITED_SOURCES,
    finalize_sources,
    place_markers,
    remap_markers,
    source_key,
    strip_markers,
)
from backend.ref_aliases import canonical_mishneh_torah_ref


def _keep(value):
    return value.strip()


class TestSourceKey:
    def test_ignores_the_note_and_case(self):
        assert source_key("Genesis 1:1 — creation") == source_key("genesis 1:1")

    def test_en_dash_separates_the_note_too(self):
        assert source_key("Berakhot 2a – the time of Shema") == "berakhot 2a"

    def test_a_hyphen_inside_the_ref_is_not_a_separator(self):
        assert source_key("Shulchan Aruch, Orach Chayim 328-329") == "shulchan aruch, orach chayim 328-329"


class TestFinalizeSources:
    def test_keeps_a_clean_list_in_order(self):
        sources, index_map = finalize_sources(["A 1 — a", "B 2 — b"], _keep)
        assert sources == ["A 1 — a", "B 2 — b"]
        assert index_map == {1: 1, 2: 2}

    def test_drops_empties_and_renumbers_the_rest(self):
        sources, index_map = finalize_sources(["A 1", "", "  ", "B 2"], _keep)
        assert sources == ["A 1", "B 2"]
        assert index_map == {1: 1, 2: None, 3: None, 4: 2}

    def test_a_repeated_source_is_kept_once_and_points_at_the_first(self):
        sources, index_map = finalize_sources(["A 1 — one", "B 2", "a 1 — again"], _keep)
        assert sources == ["A 1 — one", "B 2"]
        assert index_map == {1: 1, 2: 2, 3: 1}

    def test_the_list_is_capped_and_overflow_has_no_target(self):
        raw = [f"Book {n}" for n in range(1, MAX_CITED_SOURCES + 3)]
        sources, index_map = finalize_sources(raw, _keep)
        assert len(sources) == MAX_CITED_SOURCES
        assert index_map[MAX_CITED_SOURCES + 1] is None
        assert index_map[MAX_CITED_SOURCES] == MAX_CITED_SOURCES

    def test_anything_but_a_list_is_empty(self):
        assert finalize_sources(None, _keep) == ([], {})
        assert finalize_sources("A 1", _keep) == ([], {})

    def test_sanitize_runs_per_entry(self):
        sources, _ = finalize_sources(["  A 1  "], lambda v: v.strip().upper())
        assert sources == ["A 1"]


class TestRemapMarkers:
    MAP = {1: 1, 2: None, 3: 2, 4: 1}

    def test_renumbers(self):
        assert remap_markers("Forbidden.[3]", self.MAP) == "Forbidden.[2]"

    def test_removes_a_marker_whose_source_was_dropped(self):
        assert remap_markers("Forbidden.[2] Next.", self.MAP) == "Forbidden. Next."

    def test_a_marker_past_the_list_is_removed(self):
        assert remap_markers("Forbidden.[9]", self.MAP) == "Forbidden."

    def test_one_chip_per_source_even_when_two_numbers_merge(self):
        assert remap_markers("Forbidden.[1][4]", self.MAP) == "Forbidden.[1]"

    def test_a_list_marker_becomes_adjacent_markers(self):
        assert remap_markers("Forbidden.[1, 3]", self.MAP) == "Forbidden.[1][2]"
        assert remap_markers("Forbidden.[1,3]", self.MAP) == "Forbidden.[1][2]"

    def test_a_range_marker_expands(self):
        identity = {1: 1, 2: 2, 3: 3}
        assert remap_markers("Forbidden.[1-3]", identity) == "Forbidden.[1][2][3]"
        assert remap_markers("Forbidden.[1–3]", identity) == "Forbidden.[1][2][3]"

    def test_survivors_are_sorted(self):
        assert remap_markers("Forbidden.[3][1]", self.MAP) == "Forbidden.[1][2]"

    def test_removing_a_marker_takes_the_space_before_it(self):
        assert remap_markers("Forbidden [2].", self.MAP) == "Forbidden."

    def test_non_marker_brackets_are_left_alone(self):
        text = "See [the Rema] and [Shabbat 31b] and [2a]."
        assert remap_markers(text, self.MAP) == text

    def test_empty_text(self):
        assert remap_markers("", self.MAP) == ""
        assert remap_markers(None, self.MAP) == ""


class TestStripMarkers:
    def test_removes_every_marker(self):
        assert strip_markers("A.[1] B [2][3] C.") == "A. B C."

    def test_leaves_other_text(self):
        assert strip_markers("Shabbat 31b [the Rema]") == "Shabbat 31b [the Rema]"


class TestPlaceMarkers:
    """A marker sits right after the excerpt that rests on its source, shown
    once for a run of consecutive sentences on the same source -- never gathered
    at the paragraph's end and never repeated after every sentence."""

    def test_a_run_of_sentences_on_one_source_is_marked_once_at_its_last_sentence(self):
        assert place_markers("A.[1] B.[1] C.[1]") == "A. B. C.[1]"

    def test_each_source_stays_where_its_excerpt_ends(self):
        assert place_markers("A.[1] B.[1] C.[1][2] D.[2]") == "A. B. C.[1] D.[2]"

    def test_a_marker_is_not_pulled_down_to_the_end_of_the_paragraph(self):
        assert place_markers("Kindling is forbidden.[1] Cooking is too.[2] Plain.") == (
            "Kindling is forbidden.[1] Cooking is too.[2] Plain.")

    def test_a_source_shared_with_the_next_sentence_is_shown_there_not_twice(self):
        assert place_markers("Kindling is forbidden.[1] Cooking too.[1][2] And more.[1]") == (
            "Kindling is forbidden. Cooking too.[2] And more.[1]")

    def test_each_paragraph_and_list_item_is_its_own_segment(self):
        text = "One.[1] Again.[1]\n\nTwo.[2]\n- item.[3] item.[3]\n1. step.[3][1]"
        assert place_markers(text) == "One. Again.[1]\n\nTwo.[2]\n- item. item.[3]\n1. step.[1][3]"

    def test_a_source_may_recur_in_a_later_paragraph(self):
        assert place_markers("A.[1]\n\nB.[1]") == "A.[1]\n\nB.[1]"

    def test_numbers_ascend_and_lists_and_ranges_expand(self):
        assert place_markers("A.[3] B.[1, 2] C.[2]") == "A.[3] B.[1] C.[2]"
        assert place_markers("A.[1-3]") == "A.[1][2][3]"
        assert place_markers("A.[3][1]") == "A.[1][3]"

    def test_a_clause_marked_mid_sentence_keeps_its_own_source(self):
        assert place_markers("Some say X[1] while others say Y.[2]") == (
            "Some say X[1] while others say Y.[2]")

    def test_an_abbreviation_does_not_end_the_sentence(self):
        assert place_markers("Dr. Smith said so.[1] Next.[1]") == "Dr. Smith said so. Next.[1]"

    @pytest.mark.parametrize("text", [
        "Already.[1][2]", "No markers here.", "", "A.[1]\n\nB.[1][2]",
        "A code line:[1]\n```\nx[1] y[1]\n```",
    ])
    def test_a_tidy_segment_is_unchanged(self, text):
        assert place_markers(text) == text

    def test_idempotent(self):
        once = place_markers("A.[2] B.[1]  [2]\n\nC.[3] D.[3]")
        assert once == "A. B.[1][2]\n\nC. D.[3]"
        assert place_markers(once) == once

    def test_text_that_is_not_a_marker_is_left_alone(self):
        assert place_markers("See [2a] and [the Rema].[1] Yes.[1]") == (
            "See [2a] and [the Rema]. Yes.[1]")

    def test_a_code_fence_is_left_alone(self):
        assert place_markers("```\nx[1] y[1]\n```\nz[1] w[1]") == "```\nx[1] y[1]\n```\nz w[1]"

    def test_none_is_empty(self):
        assert place_markers(None) == ""


class TestNormalizedAnswers:
    def _normalize(self, **payload):
        return claude._normalize_structured_response(payload)

    def test_markers_follow_the_cleaned_source_list(self):
        out = self._normalize(
            ruling="Kindling is forbidden.[3] Cooking too.[1]",
            sources=["Shabbat 73b — the 39 labors", "", "Mishneh Torah, Sabbath 12 — kindling"],
            summary="Recap.[3]",
            practical_steps=["Do not light.[3]"],
        )
        assert out["sources"] == ["Shabbat 73b — the 39 labors", "Mishneh Torah, Sabbath 12 — kindling"]
        # Renumbered; each marker stays right after the sentence that rests on it.
        assert out["ruling"] == "Kindling is forbidden.[2] Cooking too.[1]"
        assert out["summary"] == "Recap.[2]"
        assert out["practical_steps"] == ["Do not light.[2]"]

    def test_a_source_repeated_after_every_sentence_is_cited_once(self):
        out = self._normalize(
            ruling="Cooking is forbidden.[1] Reheating is cooking.[1] Warming is too.[1][2]",
            sources=["Shabbat 73b — labors", "Mishneh Torah, Sabbath 3 — cooking"],
            summary="Recap.[1] More.[1]",
            practical_steps=["Do not cook.[1] Not even warm.[1]", "Ask a rabbi.[2]"],
        )
        assert out["ruling"] == "Cooking is forbidden. Reheating is cooking. Warming is too.[1][2]"
        assert out["summary"] == "Recap. More.[1]"
        assert out["practical_steps"] == ["Do not cook. Not even warm.[1]", "Ask a rabbi.[2]"]

    def test_two_sources_that_are_one_after_cleanup_are_cited_once(self):
        out = self._normalize(
            ruling="Cooking is forbidden.[1] Reheating too.[2]",
            sources=["Shabbat 73b — labors", "shabbat 73b"],
        )
        assert out["sources"] == ["Shabbat 73b — labors"]
        assert out["ruling"] == "Cooking is forbidden. Reheating too.[1]"

    def test_markers_with_no_sources_are_dropped(self):
        out = self._normalize(ruling="Permitted.[1]", sources=[])
        assert out["ruling"] == "Permitted."

    def test_rendered_markdown_keeps_markers_in_the_prose(self):
        out = self._normalize(ruling="Permitted.[1]", sources=["Berakhot 2a — shema"])
        markdown = claude.render_structured_markdown(out)
        assert "Permitted.[1]" in markdown
        assert "Berakhot 2a" in markdown


class TestPromptsAskForMarkers:
    def test_the_system_prompts_ask_for_markers_not_inline_source_names(self):
        for prompt in (claude.CORE_SYSTEM_PROMPT, claude.SIMPLE_SYSTEM_PROMPT):
            assert "[n]" in prompt or "[1]" in prompt
            assert "Hilchot Shabbat" not in prompt.replace('never "Hilchot Shabbat"', "")

    def test_the_request_prompt_asks_for_markers(self):
        assert "SOURCE MARKERS" in claude.build_prompt("can I light a fire", [], [])

    def test_the_prompts_ask_for_each_marker_right_after_the_sentence_it_backs(self):
        for prompt in (
            claude.CORE_SYSTEM_PROMPT,
            claude.SIMPLE_SYSTEM_PROMPT,
            claude.build_prompt("can I light a fire", [], []),
        ):
            assert "right after the sentence" in prompt
            assert "same source" in prompt and "once" in prompt

    def test_a_follow_up_sees_earlier_answers_without_markers(self):
        body, cited = claude._condense_assistant_answer(
            "## Direct Answer\n\nForbidden.[1][2]\n\n**Sources**\n\n- Shabbat 73b — labors\n"
        )
        assert "[1]" not in body and "Forbidden." in body
        assert cited == ["Shabbat 73b"]


@pytest.mark.parametrize("spelling, expected", [
    ("Mishneh Torah, Hilchot Shabbat 2", "Mishneh Torah, Sabbath 2"),
    ("Rambam, Hilkhot Shabbos 2:1", "Mishneh Torah, Sabbath 2:1"),
    ("Laws of Shabbat 2", "Mishneh Torah, Sabbath 2"),
    ("Rambam Hilchot Tefillah 11:1", "Mishneh Torah, Prayer and the Priestly Blessing 11:1"),
    ("Maimonides, Laws of Mourning 4", "Mishneh Torah, Mourning 4"),
    ("Mishneh Torah, Hilchot Maachalot Assurot 9:1", "Mishneh Torah, Forbidden Foods 9:1"),
    ("Mishneh Torah, Sabbath 2:1", "Mishneh Torah, Sabbath 2:1"),
    ("Mishneh Torah, Sabbath", "Mishneh Torah, Sabbath"),
])
def test_mishneh_torah_spellings_resolve_to_sefaria_titles(spelling, expected):
    assert canonical_mishneh_torah_ref(spelling) == expected


@pytest.mark.parametrize("ref", [
    "Shabbat 31b",            # a Talmud page, not the Rambam's Shabbat
    "Hilchot Unknown 3",      # a section nobody knows is not guessed
    "Shulchan Aruch, Orach Chayim 328",
    "Genesis 1:1",
    "",
    None,
])
def test_other_refs_are_left_alone(ref):
    assert canonical_mishneh_torah_ref(ref) is None


def test_removing_a_marker_never_joins_lines():
    assert remap_markers("First.\n[2] Second.", {1: 1}) == "First.\n Second."
