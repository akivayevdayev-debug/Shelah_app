"""Which Sefaria refs a question gets (backend/sefaria.py).

The curated table used to hand every unmatched question Orach Chayim 1 and the
Rambam's Laws of Prayer, filed "Laws of Shabbat" refs that Sefaria resolves to
Human Dispositions, and let a one-word umbrella ("shabbat") crowd the specific
topic out of the capped list. These tests pin the repaired behaviour.
"""

import pytest

from backend import ref_aliases
from backend import sefaria


def refs(question, context=()):
    return sefaria.find_refs_for_question(question, context)


class TestNoDefaultSource:
    @pytest.mark.parametrize("question", [
        "Who wrote Hamlet?",
        "What is the capital of France?",
        "Is it permissible to use a smartphone",
        "And then?",
        "",
    ])
    def test_unmatched_questions_get_no_refs(self, question):
        assert refs(question) == []

    def test_never_falls_back_to_orach_chayim_one_or_laws_of_prayer(self):
        for question in ("Who wrote Hamlet?", "Tell me something", "xyzzy"):
            result = refs(question)
            assert "Shulchan_Arukh,_Orach_Chayim.1" not in result
            assert not any("Laws_of_Prayer" in ref for ref in result)


class TestSpecificBeatsUmbrella:
    def test_a_specific_topic_replaces_the_umbrella_overview(self):
        assert refs("Can I cook on Shabbat?") == ["Shulchan_Arukh,_Orach_Chayim.318"]

    def test_the_specific_topic_is_not_crowded_out_of_the_cap(self):
        # "shabbat" alone has three refs; "electricity" used to be cut off
        # the 7-ref list behind them.
        result = refs("Can I use electricity on Shabbat?")
        assert result[0] == "Shulchan_Arukh,_Orach_Chayim.334"
        assert "Shulchan_Arukh,_Orach_Chayim.242" not in result

    def test_a_bare_umbrella_gets_the_overview(self):
        result = refs("What are the laws of Shabbat?")
        assert result[0] == "Shulchan_Arukh,_Orach_Chayim.242"
        assert len(result) == len(set(result)) <= 3

    def test_kosher_alone_is_an_overview_but_meat_is_specific(self):
        assert "Mishneh_Torah,_Forbidden_Foods.1" in refs("Is chicken kosher?")
        assert refs("Is this meat kosher?") == ["Shulchan_Arukh,_Yoreh_De'ah.87"]

    def test_a_phrase_stands_in_for_its_own_words(self):
        assert refs("Do I need an eruv tavshilin?") == ["Shulchan_Arukh,_Orach_Chayim.527"]
        assert refs("How long should I wait after eating meat?") == [
            "Shulchan_Arukh,_Yoreh_De'ah.89"]

    def test_topics_take_turns_so_a_long_list_cannot_starve_another(self):
        result = refs("Can I fast on Yom Kippur if I take medicine?")
        assert "Shulchan_Arukh,_Yoreh_De'ah.336" in result
        assert result[0].endswith("Orach_Chayim.611")


class TestEverydayWordsNeedContext:
    @pytest.mark.parametrize("question", [
        "How fast can you run?",
        "I am interested in reading about Hamlet",
        "Is a standing desk better for my back?",
        "I have a business trip tomorrow",
        "How do I get a visa?",
        "What medicine should I take for a cold?",
    ])
    def test_ambiguous_words_alone_match_nothing(self, question):
        assert refs(question) == []

    def test_the_same_words_match_in_a_torah_question(self):
        assert "Shulchan_Arukh,_Orach_Chayim.549" in refs("Is it permitted to fast on a Jewish fast day?")
        assert "Shulchan_Arukh,_Yoreh_De'ah.160" in refs("Is interest permitted according to halacha?")

    def test_bris_is_not_brisket_and_shul_is_not_shulchan(self):
        assert not any("De'ah.260" in ref for ref in refs("Is brisket kosher?"))
        assert refs("What does the Shulchan Arukh say about honesty?") == []
        assert refs("How do I get to shul?") == [
            "Shulchan_Arukh,_Orach_Chayim.151", "Shulchan_Arukh,_Orach_Chayim.150"]

    def test_shabbat_only_topics_need_shabbat(self):
        assert refs("Can I use electricity?") == []
        assert refs("Can I cook food on Yom Tov?") == ["Shulchan_Arukh,_Orach_Chayim.495"]
        assert refs("Is it permitted to cook on Saturday?") == ["Shulchan_Arukh,_Orach_Chayim.318"]


class TestHebrew:
    def test_a_hebrew_prefix_is_allowed(self):
        assert refs("מותר לבשל בשבת") == ["Shulchan_Arukh,_Orach_Chayim.318"]

    def test_hebrew_words_inside_other_words_do_not_match(self):
        # "החלה" ("began") is not the "challah" of "ה" + "חלה"
        assert refs("החלה לרדת גשם") == []

    def test_meat_and_milk_in_hebrew(self):
        assert refs("מה דין בשר וחלב") == [
            "Shulchan_Arukh,_Yoreh_De'ah.87",
            "Shulchan_Arukh,_Yoreh_De'ah.89",
            "Shulchan_Arukh,_Yoreh_De'ah.88",
        ]


class TestCuratedTable:
    def all_refs(self):
        return {ref for refs_ in sefaria.TOPIC_REFS.values() for ref in refs_}

    def test_no_ref_uses_the_spelling_sefaria_resolves_to_human_dispositions(self):
        # "Rambam,_Mishneh_Torah,_Laws_of_X" is not a Sefaria ref; the name
        # lookup landed every one of them on "Mishneh Torah, Human Dispositions".
        assert not any("Laws_of_" in ref or ref.startswith("Rambam,") for ref in self.all_refs())

    def test_mishneh_torah_refs_use_sefaria_section_titles(self):
        sections = set(ref_aliases._SECTIONS)
        for ref in self.all_refs():
            if ref.startswith("Mishneh_Torah,_"):
                title = ref[len("Mishneh_Torah,_"):].rsplit(".", 1)[0].replace("_", " ")
                assert title in sections, ref

    def test_refs_that_resolved_to_the_wrong_book_are_gone(self):
        refs_ = self.all_refs()
        assert "Rama,_Orach_Chayim.242" not in refs_  # Yad Ramah on Sanhedrin
        assert "Yom_Kippur" not in refs_              # text not found

    def test_every_list_is_a_unique_non_empty_list_of_refs(self):
        for keyword, ref_list in sefaria.TOPIC_REFS.items():
            assert ref_list and len(ref_list) == len(set(ref_list)), keyword
            assert all(isinstance(ref, str) and ref.strip() for ref in ref_list), keyword

    def test_umbrella_ambiguous_and_shabbat_only_keywords_exist_in_the_table(self):
        known = set(sefaria.TOPIC_REFS)
        assert sefaria.UMBRELLA_KEYWORDS <= known
        assert sefaria._AMBIGUOUS_KEYWORDS <= known
        assert sefaria._SHABBAT_ONLY_KEYWORDS <= known
        assert sefaria._PARTIAL_MATCH_WORDS  # distinctive words of multi-word keywords
