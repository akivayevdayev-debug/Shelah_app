"""Tests for scripts/check_prompt_doc_sync.py's parsing/classification heuristic.

Not exhaustive -- this locks in the specific parser bugs already hit once
while building the script (alphanumeric prompt suffixes, the trailing
appendix table, umbrella-section false positives, negation, emoji priority)
so they don't silently regress. See the script's own module docstring for
what it does and does not promise.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_prompt_doc_sync.py"
_spec = importlib.util.spec_from_file_location("check_prompt_doc_sync", SCRIPT_PATH)
cpds = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cpds
_spec.loader.exec_module(cpds)


class TestClassify:
    def test_done_emoji(self):
        assert cpds.classify("Some intro.\n\n✅ **Done 2026-08-23.** All good.") == "done"

    def test_open_emoji(self):
        assert cpds.classify("🔴 still needs work") == "open"

    def test_partial_emoji_wins_over_done_keyword(self):
        assert cpds.classify("🟡 Phase 0 done, Phase 1 not started") == "partial"

    def test_done_and_open_keywords_together_are_partial(self):
        assert cpds.classify("Step 1 is done. Step 2 remains open.") == "partial"

    def test_negation_blocks_done_phrase(self):
        assert cpds.classify("This is not resolved yet.") != "done"

    def test_hyphenated_negation_window(self):
        # "not" sits two words before "closed" via a hyphenated compound --
        # must still be caught by the ~20-char lookback, not just adjacency.
        assert cpds.classify("This item is not-fully-closed pending review.") != "done"

    def test_shipped_a_bug_is_not_done(self):
        assert cpds.classify("The last release already shipped a bug in this path.") != "done"

    def test_last_paragraph_with_signal_wins_over_earlier_stale_one(self):
        text = (
            "STEP 1 needs a human decision before anything else.\n\n"
            "**Done 2026-08-23.** Resolved by the operator directly."
        )
        assert cpds.classify(text) == "done"

    def test_emoji_paragraph_preferred_over_later_keyword_only_paragraph(self):
        # A correctly-marked emoji status earlier in the block should not be
        # overridden by a later paragraph's stray keyword about something else.
        text = (
            "🟡 This section is still open for its own remaining item.\n\n"
            "Meanwhile a sibling section closed six of its eight items."
        )
        assert cpds.classify(text) == "partial"

    def test_no_signal_is_unknown(self):
        assert cpds.classify("Just some narrative prose with no status markers.") == "unknown"

    def test_fenced_code_is_ignored(self):
        text = "```\nfixed a bug, done, closed\n```\n\nActual status: 🔴 still open."
        assert cpds.classify(text) == "open"


class TestParsePlanSections:
    def test_own_text_excludes_subsections(self):
        text = (
            "## 19. Umbrella finding — 🟡 partial overall\n"
            "\n"
            "Intro paragraph, no further signal here.\n"
            "\n"
            "### 19.1 A subsection\n"
            "\n"
            "✅ this subsection is done.\n"
        )
        sections = cpds.parse_plan_sections(text)
        own_text, full_text = sections["19"]
        assert "19.1" not in own_text
        assert "✅" not in own_text
        assert "✅" in full_text
        assert cpds.classify(own_text) == "partial"
        assert cpds.classify(full_text) == "done"

    def test_full_text_stops_at_next_same_level_heading(self):
        text = (
            "## 19. First section\n\nsome text\n\n"
            "### 19.1 child\n\nchild text\n\n"
            "## 20. Second section\n\nother text\n"
        )
        sections = cpds.parse_plan_sections(text)
        _, full_19 = sections["19"]
        assert "Second section" not in full_19


class TestParsePrompts:
    def test_alphanumeric_suffix_headers_are_separated(self):
        text = (
            "## Prompt 33 — §20: numeric prompt\n\nbody 33\n\n"
            "## Prompt 33a — §20a: lettered prompt\n\nbody 33a\n\n"
            "## Prompt 34 — §21: next numeric prompt\n\nbody 34\n"
        )
        prompts = cpds.parse_prompts(text)
        assert set(prompts) == {"33", "33a", "34"}
        assert "body 33a" not in prompts["33"]["body"]
        assert "body 33a" in prompts["33a"]["body"]
        assert "body 34" not in prompts["33a"]["body"]

    def test_last_prompt_does_not_swallow_appendix(self):
        text = (
            "## Prompt 65 — §52: last prompt\n\n✅ done\n\n"
            "Revised for the 2026-07-28 status snapshot, here is a table:\n\n"
            "| Prompt | Status |\n|---|---|\n| 12 | 🔴 open |\n"
        )
        prompts = cpds.parse_prompts(text)
        assert "🔴 open" not in prompts["65"]["body"]


class TestFindMismatches:
    def test_matching_statuses_produce_no_finding(self):
        plan_sections = {"20": ("## 20. Thing — ✅ done", "## 20. Thing — ✅ done")}
        prompts = {
            "70": {
                "header": "## Prompt 70 — §20: thing done — for Opus 5",
                "body": "## Prompt 70 — §20: thing done — for Opus 5\n\n✅ done here too.",
            }
        }
        assert cpds.find_mismatches(plan_sections, prompts) == []

    def test_diverging_statuses_produce_a_finding(self):
        plan_sections = {"20": ("## 20. Thing — 🔴 still open", "## 20. Thing — 🔴 still open")}
        prompts = {
            "70": {
                "header": "## Prompt 70 — §20: thing done — for Opus 5",
                "body": "## Prompt 70 — §20: thing done — for Opus 5\n\n✅ done here.",
            }
        }
        findings = cpds.find_mismatches(plan_sections, prompts)
        assert len(findings) == 1
        assert findings[0]["prompt"] == "70"
        assert findings[0]["section"] == "20"
        assert findings[0]["prompt_status"] == "done"
        assert findings[0]["section_status"] == "open"

    def test_backstory_section_refs_after_colon_are_ignored(self):
        # Only the segment between the dash and the first colon names the
        # section this row *tracks* -- refs mentioned afterward (backstory
        # about a different prompt) must not be compared against this row.
        plan_sections = {
            "32": ("## 32. Tracked section — ✅ done", "## 32. Tracked section — ✅ done"),
            "13": ("## 13. Unrelated backstory — 🔴 still open", "## 13. Unrelated backstory — 🔴 still open"),
        }
        prompts = {
            "44": {
                "header": "## Prompt 44 — §32: findings from implementing Prompts 26/27 (§13 backstory) — for Opus 5",
                "body": "## Prompt 44 — §32: findings from implementing Prompts 26/27 (§13 backstory) — for Opus 5\n\n✅ done here.",
            }
        }
        findings = cpds.find_mismatches(plan_sections, prompts)
        assert findings == []
