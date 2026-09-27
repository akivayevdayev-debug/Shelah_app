"""Typed siddur lines (backend/siddur_lines.py): headings, instructions,
conditional text and prayer read off the printed-siddur markup."""

import pytest

from backend import siddur_lines as sl


class TestClassify:
    def test_big_alone_is_a_heading_as_plain_text(self):
        assert sl.classify("<big><b>סדר הבדלה</b></big>") == {"t": "heading", "he": "סדר הבדלה"}

    def test_heading_inside_a_bold_wrapper(self):
        # Torat Emet's spelling of the same heading.
        assert sl.classify("<b><big><b>סדר הבדלה</b></big></b>")["t"] == "heading"

    def test_unpointed_small_is_an_instruction_with_its_citation(self):
        line = sl.classify("<small>יקח הכוס בידו <small>(בא\"ח)</small></small>")
        assert line == {"t": "instruction", "he": "יקח הכוס בידו <small>(בא\"ח)</small>"}

    def test_pointed_small_is_conditional(self):
        line = sl.classify("<small><b>יִתְגַּדַּל</b> וְיִתְקַדַּשׁ שְׁמֵהּ רַבָּא.</small>")
        assert line["t"] == "conditional" and "label" not in line
        assert line["he"].startswith("<b>יִתְגַּדַּל</b>")

    def test_a_leading_unpointed_small_becomes_the_label(self):
        line = sl.classify("<small><small>בשבועות:</small> חַג הַשָּׁבוּעוֹת הַזֶּה,</small>")
        assert line == {"t": "conditional", "label": "בשבועות:", "he": "חַג הַשָּׁבוּעוֹת הַזֶּה,"}

    def test_a_long_unpointed_opener_stays_in_the_body(self):
        rubric = "אין אומרים " + "תיקון חצות " * 10
        line = sl.classify(f"<small><small>{rubric}</small> בָּרוּךְ אַתָּה</small>")
        assert "label" not in line and rubric.strip() in line["he"]

    def test_inline_notes_on_both_ends_are_prayer_not_a_wrapped_segment(self):
        # A regex over the string would read this as one <small>...</small>.
        line = sl.classify("<small>ליל ד'</small> תּוֹרָה צִוָּה <small>ג' פעמים.</small>")
        assert line["t"] == "prayer"
        assert line["he"] == "<small>ליל ד'</small> תּוֹרָה צִוָּה <small>ג' פעמים.</small>"

    def test_space_inside_an_inline_tag_is_kept(self):
        line = sl.classify("<small>ועונים הקהל: </small>בָּרוּךְ יְהֹוָה")
        assert line["he"] == "<small>ועונים הקהל: </small>בָּרוּךְ יְהֹוָה"

    @pytest.mark.parametrize("fragment", ["", "   ", "<b> </b>", "<br>"])
    def test_empty_segments_are_skipped(self, fragment):
        assert sl.classify(fragment) is None


class TestSanitizing:
    def test_only_safe_tags_survive_and_text_is_escaped(self):
        html = sl.classify('<span onclick="x()">a &lt;script&gt; <em>b</em> <strong>c</strong> <big>d</big></span>')["he"]
        assert html == "a &lt;script&gt; <i>b</i> <b>c</b> <b>d</b>"

    def test_script_tag_is_unwrapped_to_escaped_text(self):
        assert sl.render_fragment("<script>alert(1)</script>ok") == "alert(1)ok"

    def test_edge_line_breaks_and_nbsp_are_trimmed(self):
        assert sl.render_fragment("<br>  שָׁלוֹם  עֲלֵיכֶם <br>") == "שָׁלוֹם עֲלֵיכֶם"

    def test_stray_close_tags_and_unclosed_tags_are_tolerated(self):
        assert sl.render_fragment("a</b> <i>b") == "a <i>b</i>"

    def test_entities_are_decoded_then_reescaped_once(self):
        assert sl.render_fragment("My L·rd &amp; G·d") == "My L·rd &amp; G·d"

    def test_plain_text_of_a_string(self):
        assert sl.plain_text("<b>א</b> <small>ב</small>") == "א ב"


class TestWhenTags:
    @pytest.mark.parametrize("text, tags", [
        ("בראש חודש ובחול המועד אומרים:", ["rosh-chodesh", "chol-hamoed-pesach", "chol-hamoed-sukkot"]),
        ('בחוה"מ פסח:', ["chol-hamoed-pesach", "pesach"]),
        ("חוה''מ סוכות", ["chol-hamoed-sukkot", "sukkot"]),
        ("בחנוכה ופורים אומרים:", ["chanukah", "purim"]),
        ("בתענית ציבור השליח ציבור אומר בחזרה", ["fast"]),
        ("בשבת אומרים", ["shabbat"]),
        ("בשבת שובה אומרים כאן אבינו מלכינו", ["shabbat-shuva"]),
        ("בעשרת ימי תשובה אומרים:", ["aseret-yemei-teshuva"]),
        ("בקיץ:", ["barchenu"]),
        ("בחורף:", ["barech-alenu"]),
        ("בראש השנה:", ["rosh-hashana"]),
    ])
    def test_switch_rubrics(self, text, tags):
        assert sl.when_tags(text) == tags

    @pytest.mark.parametrize("text", [
        "בתשעה באב ויום הכיפורים אין אומרים ברכה זו",  # a negation, and not Purim
        "למחרת יום הכיפורים אומרים",
        "במוצאי שבת ויום טוב אומרים",
        "בערב שבת אומרים מזמור",
        "ביום־טוב שאינו שבת מדלגים",
        "בחורף הזה אומרים",  # not the bare season rubric
        "",
        "בשבת " + "ומוסיפים " * 10,  # a rubric paragraph, not a switch
    ])
    def test_non_switches_get_no_tags(self, text):
        assert sl.when_tags(text) == []

    def test_every_tag_is_a_known_key(self):
        text = "בראש חודש ובחול המועד בחנוכה ופורים בתענית"
        assert set(sl.when_tags(text)) <= set(sl.WHEN_KEYS)


class TestBuildLines:
    def test_pairs_english_by_index_and_numbers_segments(self):
        lines = sl.build_lines(
            ["<big>עמידה</big>", "", "<small>בקיץ:</small>", "<b>בָּרְכֵנוּ</b> יְהֹוָה"],
            ["Amida", "", "[In Summer]", "Bless us &amp; our land"],
        )
        assert lines == [
            {"t": "heading", "he": "עמידה", "en": "Amida", "n": 1},
            {"t": "instruction", "he": "בקיץ:", "en": "[In Summer]", "when": ["barchenu"], "n": 3},
            {"t": "prayer", "he": "<b>בָּרְכֵנוּ</b> יְהֹוָה", "en": "Bless us &amp; our land", "n": 4},
        ]

    def test_english_beside_empty_hebrew_is_kept_as_prayer(self):
        assert sl.build_lines(["", None], ["Only English"]) == [{"t": "prayer", "he": "", "en": "Only English", "n": 1}]

    def test_labelled_conditional_gets_when_from_its_label(self):
        [line] = sl.build_lines(["<small><small>בראש חדש:</small> רֹאשׁ חֹדֶשׁ הַזֶּה,</small>"])
        assert line["when"] == ["rosh-chodesh"] and line["label"] == "בראש חדש:"

    def test_prayer_lines_never_get_when(self):
        [line] = sl.build_lines(["<small>בשבת:</small> אֱלֹהֵינוּ"])
        assert line["t"] == "prayer" and "when" not in line

    @pytest.mark.parametrize("he, en, aligned", [
        (["a", "b"], ["x", "y"], True),
        (["a", "b"], ["x", "", ""], True),
        (["a"], ["x", "y"], False),
        (["a"], None, True),
        ([], [], True),
    ])
    def test_english_alignment(self, he, en, aligned):
        assert sl.english_is_aligned(he, en) is aligned
