"""
Golden-master characterization tests for the text/formatting layer being
extracted from app.py into backend/utils/text_engine.py (Phase 1 of the
backend refactor in plan.md).

These pin the exact current output of the typography/normalization functions
so the move is provably behavior-preserving. Values were captured by
exercising the pre-move app.py functions directly. Do not "fix" any assertion
here without a deliberate, separately-reviewed behavior change — this file's
job is to prove the move changed nothing, not to improve formatting.
"""

from __future__ import annotations

from backend.utils.text_engine import (
    HEBREW_DIACRITICS_RE,
    WEB_LAST_RESORT_WARNING,
    WEB_LAST_RESORT_WARNING_PLAIN,
    _strip_model_web_warning_prefix,
    _normalize_ai_answer,
    _bold_halakhic_verdicts,
    _collapse_markdown_spacing,
    _format_ui_answer,
    _strip_source_attribution_prefix,
    _normalize_answer_line,
)

RABBI_FOOTER = "Please consult with your local Rabbi for a final ruling."


# ── HEBREW_DIACRITICS_RE ──────────────────────────────────────────────────────

def test_hebrew_diacritics_re_strips_niqqud_and_trope():
    text = "שָׁלוֹם עֲלֵיכֶם"
    assert HEBREW_DIACRITICS_RE.sub("", text) == "שלום עליכם"


# ── CLOCK_TIME_LATEX_RE (via _normalize_ai_answer) ────────────────────────────

def test_normalize_ai_answer_strips_clock_time_latex_artifact():
    result = _normalize_ai_answer("Candle lighting is at $8:30\\text{PM}$ tonight.")
    assert result == f"{RABBI_FOOTER}\n\nCandle lighting is at 8:30 PM tonight."


def test_normalize_ai_answer_strips_clock_time_latex_double_backslash():
    result = _normalize_ai_answer("Time is $11:05\\\\text{ AM }$ now.")
    assert result == f"{RABBI_FOOTER}\n\nTime is 11:05 AM now."


# ── HALAKHIC_VERDICT_RE / _bold_halakhic_verdicts ─────────────────────────────

def test_bold_halakhic_verdicts_bolds_and_canonicalizes_verdict_line():
    result = _bold_halakhic_verdicts(
        "This activity is prohibited on Shabbat, but permitted on Chol HaMoed."
    )
    assert result == "This activity is **Prohibited** on Shabbat, but **Permitted** on Chol HaMoed."


def test_bold_halakhic_verdicts_handles_asur_mutar_variants():
    result = _bold_halakhic_verdicts("mutar and assur are both discussed")
    assert result == "**Mutar** and **Assur** are both discussed"


# ── _strip_model_web_warning_prefix ───────────────────────────────────────────

def test_strip_model_web_warning_prefix_removes_bold_warning():
    warned = WEB_LAST_RESORT_WARNING + "\n\nSome general web info follows."
    assert _strip_model_web_warning_prefix(warned) == "Some general web info follows."


def test_strip_model_web_warning_prefix_removes_plain_warning():
    warned = WEB_LAST_RESORT_WARNING_PLAIN + "\n\nSome general web info follows."
    assert _strip_model_web_warning_prefix(warned) == "Some general web info follows."


# ── _collapse_markdown_spacing (multi-blank-line markdown) ───────────────────

def test_collapse_markdown_spacing_collapses_multiple_blank_lines():
    lines = ["## Ruling", "", "", "", "Text here.", "", "", "### Reason", "More text."]
    assert _collapse_markdown_spacing(lines) == [
        "## Ruling", "", "Text here.", "", "### Reason", "More text.",
    ]


def test_collapse_markdown_spacing_all_blank_lines_yields_empty():
    assert _collapse_markdown_spacing(["", "", ""]) == []
    assert _collapse_markdown_spacing([]) == []


# ── _format_ui_answer (full pipeline) ─────────────────────────────────────────

def test_format_ui_answer_full_pipeline():
    raw = "**Ruling**: This is prohibited.\n\nReason: because of X.\n\n\n\n- point one\n- point two"
    result = _format_ui_answer(raw)
    assert result == (
        "### Ruling\nThis is **Prohibited**.\n\n"
        "### Reason\nbecause of X.\n\n"
        "- point one\n- point two"
    )


def test_format_ui_answer_no_verified_source_found_passthrough():
    assert _format_ui_answer("No verified source found") == "No verified source found"


# ── _strip_source_attribution_prefix ──────────────────────────────────────────

def test_strip_source_attribution_prefix_two_paragraph_shape():
    note = (
        "Note: This information was pulled from Sefaria and Community Customs. "
        f"{RABBI_FOOTER}\n\n"
        "Actual body text here."
    )
    assert _strip_source_attribution_prefix(note) == "Actual body text here."


def test_strip_source_attribution_prefix_single_line_shape():
    note = f"Note: This information was pulled from Sefaria. {RABBI_FOOTER} Actual body."
    assert _strip_source_attribution_prefix(note) == "Actual body."


# ── _should_drop_debug_line / _normalize_answer_line (UI_SECTION_KEYS) ───────

def test_normalize_answer_line_drops_conflict_flags_debug_line():
    assert _normalize_answer_line("- Conflict Flags: none detected") == []


def test_normalize_answer_line_promotes_ui_section_key_value_to_header():
    assert _normalize_answer_line("Ruling: It is permitted") == [
        "### Ruling", "It is **Permitted**",
    ]


def test_normalize_answer_line_promotes_bold_header():
    assert _normalize_answer_line("**Practical Steps**: do X then Y") == [
        "### Practical Steps", "do X then Y",
    ]


# ── _normalize_ai_answer: domain refusal passthrough ──────────────────────────

def test_normalize_ai_answer_preserves_domain_refusal_message_and_adds_footer():
    refusal = (
        "Sh'elah is a specialized tool for Halakhic and communal knowledge. "
        "I cannot assist with medical diagnoses, as it falls outside my specialized domain."
    )
    result = _normalize_ai_answer(refusal)
    assert result == f"{refusal}\n\n{RABBI_FOOTER}"


def test_normalize_ai_answer_domain_refusal_with_footer_already_present_unchanged():
    refusal_with_footer = (
        "Sh'elah is a specialized tool for Halakhic and communal knowledge. "
        "I cannot assist with medical diagnoses, as it falls outside my specialized domain. "
        f"{RABBI_FOOTER}"
    )
    assert _normalize_ai_answer(refusal_with_footer) == refusal_with_footer


# ── _normalize_ai_answer: empty / fallback handling ───────────────────────────

def test_normalize_ai_answer_empty_input_uses_fallback_text():
    assert _normalize_ai_answer("") == "No verified source found"
    assert _normalize_ai_answer(None) == "No verified source found"


def test_normalize_ai_answer_empty_input_no_fallback_returns_empty():
    assert _normalize_ai_answer("", allow_empty_fallback=False) == ""


def test_normalize_ai_answer_web_warning_and_attribution_combo():
    result = _normalize_ai_answer(
        "Body text.",
        include_web_warning=True,
        source_attribution_note=f"Note: from web. {RABBI_FOOTER}",
    )
    assert result == (
        f"{WEB_LAST_RESORT_WARNING}\n\n"
        f"Note: from web. {RABBI_FOOTER}\n\n"
        "Body text."
    )


# ── Defensive-integrity: no dangling markdown/HTML introduced by these fns ───
# These functions do not truncate text themselves and do not sanitize
# markdown/HTML that arrives already-truncated from an upstream source (e.g. a
# token-limited AI response cut mid-token). The characterization below pins
# the exact current pass-through behavior: the functions must not raise, and
# must not introduce ANY new unbalanced markdown/HTML beyond what was already
# present in the input — pre-existing dangling markup passes through verbatim
# rather than being silently "fixed" (fixing that is out of scope for a
# behavior-preserving move).

def test_bold_halakhic_verdicts_does_not_corrupt_string_cut_mid_bold():
    cut_bold = "The ruling is that this is **proh"
    result = _bold_halakhic_verdicts(cut_bold)
    assert result == cut_bold
    assert result.count("**") == cut_bold.count("**")


def test_format_ui_answer_does_not_corrupt_string_cut_mid_bold():
    cut_bold = "The ruling is that this is **proh"
    result = _format_ui_answer(cut_bold)
    assert result == "## Ruling\n\nThe ruling is that this is **proh"
    assert result.count("**") == cut_bold.count("**")


def test_normalize_ai_answer_does_not_corrupt_string_cut_mid_bold():
    cut_bold = "The ruling is that this is **proh"
    result = _normalize_ai_answer(cut_bold)
    assert result == f"{RABBI_FOOTER}\n\nThe ruling is that this is **proh"
    assert result.count("**") == cut_bold.count("**")


def test_format_ui_answer_does_not_corrupt_string_cut_mid_tag():
    cut_tag = "See the note <em>important cont"
    result = _format_ui_answer(cut_tag)
    assert result == "## Ruling\n\nSee the note <em>important cont"
    assert result.count("<") == cut_tag.count("<")
    assert result.count(">") == cut_tag.count(">")


def test_normalize_ai_answer_does_not_corrupt_string_cut_mid_tag():
    cut_tag = "See the note <em>important cont"
    result = _normalize_ai_answer(cut_tag)
    assert result == f"{RABBI_FOOTER}\n\nSee the note <em>important cont"
    assert result.count("<") == cut_tag.count("<")
    assert result.count(">") == cut_tag.count(">")
