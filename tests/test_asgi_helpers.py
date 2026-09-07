"""
Direct unit tests for asgi.py's pure helper functions -- test anchors for
the plan.md §32.1 complexity refactor of _flatten_sources_for_ai (and its
extracted helpers _select_source_line_text / _flatten_one_source_for_ai).
Not exercised directly by tests/test_ask.py's end-to-end /ask coverage, so
these pin the function's own branching (language fallback, skip rules,
malformed-input tolerance) in isolation.
"""

from asgi import _flatten_sources_for_ai, _flatten_one_source_for_ai, _select_source_line_text


# ── _select_source_line_text ──────────────────────────────────────────────────

def test_select_source_line_text_english_prefers_en():
    assert _select_source_line_text({"en": "hello", "he": "shalom"}, use_hebrew=False) == "hello"


def test_select_source_line_text_english_falls_back_to_he():
    assert _select_source_line_text({"he": "shalom"}, use_hebrew=False) == "shalom"


def test_select_source_line_text_english_falls_back_to_empty_string():
    assert _select_source_line_text({}, use_hebrew=False) == ""


def test_select_source_line_text_hebrew_prefers_he():
    assert _select_source_line_text({"en": "hello", "he": "shalom"}, use_hebrew=True) == "shalom"


def test_select_source_line_text_hebrew_falls_back_to_en():
    assert _select_source_line_text({"en": "hello"}, use_hebrew=True) == "hello"


# ── _flatten_one_source_for_ai ─────────────────────────────────────────────────

def test_flatten_one_source_joins_multiple_lines():
    src = {"ref": "Shabbat 21a", "lines": [{"en": "line one"}, {"en": "line two"}]}
    assert _flatten_one_source_for_ai(src, use_hebrew=False) == {
        "ref": "Shabbat 21a", "text": "line one line two"}


def test_flatten_one_source_skips_non_dict_lines():
    src = {"ref": "x", "lines": [{"en": "kept"}, "not a dict", None]}
    assert _flatten_one_source_for_ai(src, use_hebrew=False) == {"ref": "x", "text": "kept"}


def test_flatten_one_source_returns_none_when_no_ref_and_no_text():
    assert _flatten_one_source_for_ai({"ref": "", "lines": []}, use_hebrew=False) is None


def test_flatten_one_source_keeps_ref_only_entry():
    assert _flatten_one_source_for_ai({"ref": "Shabbat 21a", "lines": []}, use_hebrew=False) == {
        "ref": "Shabbat 21a", "text": ""}


def test_flatten_one_source_handles_non_list_lines():
    assert _flatten_one_source_for_ai({"ref": "x", "lines": "not a list"}, use_hebrew=False) == {
        "ref": "x", "text": ""}


# ── _flatten_sources_for_ai ────────────────────────────────────────────────────

def test_flatten_sources_for_ai_basic_english():
    sources = [{"ref": "Shabbat 21a", "lines": [{"en": "Kindle lights", "he": "מדליקין"}]}]
    assert _flatten_sources_for_ai(sources, answer_language="en") == [
        {"ref": "Shabbat 21a", "text": "Kindle lights"}]


def test_flatten_sources_for_ai_hebrew_language():
    sources = [{"ref": "Shabbat 21a", "lines": [{"en": "Kindle lights", "he": "מדליקין"}]}]
    assert _flatten_sources_for_ai(sources, answer_language="he") == [
        {"ref": "Shabbat 21a", "text": "מדליקין"}]


def test_flatten_sources_for_ai_skips_non_dict_sources():
    sources = ["not a dict", {"ref": "x", "lines": [{"en": "kept"}]}]
    assert _flatten_sources_for_ai(sources) == [{"ref": "x", "text": "kept"}]


def test_flatten_sources_for_ai_drops_entries_with_no_ref_and_no_text():
    sources = [{"ref": "", "lines": []}, {"ref": "x", "lines": [{"en": "kept"}]}]
    assert _flatten_sources_for_ai(sources) == [{"ref": "x", "text": "kept"}]


def test_flatten_sources_for_ai_empty_input_returns_empty_list():
    assert _flatten_sources_for_ai([]) == []


def test_flatten_sources_for_ai_defaults_to_english():
    sources = [{"ref": "x", "lines": [{"en": "english text", "he": "hebrew text"}]}]
    assert _flatten_sources_for_ai(sources) == [{"ref": "x", "text": "english text"}]
