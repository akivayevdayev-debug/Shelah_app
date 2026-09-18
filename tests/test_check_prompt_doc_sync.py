"""Tests for scripts/check_prompt_doc_sync.py's parsing/classification heuristic.

Not exhaustive -- this locks in the specific parser bugs already hit once
while building the script (alphanumeric prompt suffixes, the trailing
appendix table, umbrella-section false positives, negation, emoji priority)
so they don't silently regress. See the script's own module docstring for
what it does and does not promise.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

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


class TestFindMismatchesSkipPaths:
    """Every reason find_mismatches() declines to compare a prompt with a section."""

    _DONE_PROMPT = {
        "header": "## Prompt 70 — §20: thing — for Opus 5",
        "body": "## Prompt 70 — §20: thing — for Opus 5\n\n✅ done here.",
    }

    def test_prompt_without_a_section_reference_is_skipped(self):
        prompts = {"71": {"header": "## Prompt 71 — no refs here: x", "body": "✅ done"}}
        plan_sections = {"20": ("🔴 open", "🔴 open")}
        assert cpds.find_mismatches(plan_sections, prompts) == []

    def test_prompt_whose_own_status_is_unclassifiable_is_skipped(self):
        prompts = {"70": {**self._DONE_PROMPT, "body": "Narrative with no status markers."}}
        plan_sections = {"20": ("🔴 open", "🔴 open")}
        assert cpds.find_mismatches(plan_sections, prompts) == []

    def test_reference_to_a_section_missing_from_the_plan_is_skipped(self):
        assert cpds.find_mismatches({}, {"70": self._DONE_PROMPT}) == []

    def test_section_that_never_classifies_is_skipped(self):
        plan_sections = {"20": ("Plain prose.", "More plain prose.")}
        assert cpds.find_mismatches(plan_sections, {"70": self._DONE_PROMPT}) == []

    def test_section_status_falls_back_to_full_text_when_own_text_is_unclassifiable(self):
        plan_sections = {"20": ("Plain prose.", "## 20. Thing — 🔴 still open")}
        findings = cpds.find_mismatches(plan_sections, {"70": self._DONE_PROMPT})
        assert [(f["prompt_status"], f["section_status"]) for f in findings] == [("done", "open")]

    def test_repeated_reference_in_one_header_is_reported_once(self):
        prompt = {
            "header": "## Prompt 70 — §20 and §20: thing — for Opus 5",
            "body": "## Prompt 70 — §20 and §20: thing\n\n✅ done here.",
        }
        plan_sections = {"20": ("🔴 open", "🔴 open")}
        assert len(cpds.find_mismatches(plan_sections, {"70": prompt})) == 1

    def test_findings_come_out_in_numeric_prompt_order_with_letter_suffixes(self):
        plan_sections = {"20": ("🔴 open", "🔴 open")}

        def prompt(num):
            header = f"## Prompt {num} — §20: thing"
            return {"header": header, "body": f"{header}\n\n✅ done."}

        prompts = {num: prompt(num) for num in ("10", "9b", "9a", "100")}
        order = [f["prompt"] for f in cpds.find_mismatches(plan_sections, prompts)]
        assert order == ["9a", "9b", "10", "100"]


class TestResolveRepoPath:
    """SonarCloud pythonsecurity:S8707: --plan / --prompts are CLI-supplied and
    were read without any containment check, so ``../`` or an absolute path
    could point the script at any file the caller can read."""

    def test_relative_path_inside_repo_resolves(self, tmp_path, monkeypatch):
        (tmp_path / "plan.md").write_text("x")
        monkeypatch.chdir(tmp_path)
        assert cpds.resolve_repo_path("plan.md", tmp_path) == (tmp_path / "plan.md").resolve()

    def test_nested_and_dotdot_that_stays_inside_is_allowed(self, tmp_path, monkeypatch):
        (tmp_path / "docs").mkdir()
        (tmp_path / "plan.md").write_text("x")
        monkeypatch.chdir(tmp_path)
        assert cpds.resolve_repo_path("docs/../plan.md", tmp_path) == (tmp_path / "plan.md").resolve()

    def test_dotdot_traversal_out_of_repo_is_rejected(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "secret.txt").write_text("secret")
        monkeypatch.chdir(repo)
        with pytest.raises(ValueError, match="outside the repository root"):
            cpds.resolve_repo_path("../secret.txt", repo)

    def test_deep_dotdot_traversal_is_rejected(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.chdir(repo)
        with pytest.raises(ValueError):
            cpds.resolve_repo_path("../" * 10 + "etc/passwd", repo)

    def test_absolute_path_outside_repo_is_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("x")
        with pytest.raises(ValueError, match="outside the repository root"):
            cpds.resolve_repo_path(str(outside), repo)

    def test_absolute_path_inside_repo_is_allowed(self, tmp_path):
        inside = tmp_path / "plan.md"
        inside.write_text("x")
        assert cpds.resolve_repo_path(str(inside), tmp_path) == inside.resolve()

    def test_sibling_directory_sharing_the_repo_name_prefix_is_rejected(self, tmp_path):
        # A naive str.startswith() containment check would accept "repo-evil".
        repo = tmp_path / "repo"
        evil = tmp_path / "repo-evil"
        repo.mkdir()
        evil.mkdir()
        (evil / "plan.md").write_text("x")
        with pytest.raises(ValueError):
            cpds.resolve_repo_path(str(evil / "plan.md"), repo)

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlink support")
    def test_symlink_pointing_out_of_repo_is_rejected(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        target = tmp_path / "outside.md"
        target.write_text("x")
        link = repo / "plan.md"
        link.symlink_to(target)
        with pytest.raises(ValueError):
            cpds.resolve_repo_path(str(link), repo)

    def test_default_root_is_the_real_repository(self):
        assert cpds.resolve_repo_path(str(SCRIPT_PATH)) == SCRIPT_PATH.resolve()


class TestMainCli:
    PLAN = "## 5. Some section\n\nAll done. \u2705\n"
    PROMPTS = "## Prompt 1 -- \xa75: thing\n\n\U0001f534 still open\n"

    def _repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cpds, "REPO_ROOT", tmp_path)
        monkeypatch.chdir(tmp_path)
        (tmp_path / "plan.md").write_text(self.PLAN, encoding="utf-8")
        (tmp_path / "claude_code_prompts.md").write_text(self.PROMPTS, encoding="utf-8")

    def test_reports_a_mismatch_for_in_repo_files(self, tmp_path, monkeypatch, capsys):
        self._repo(tmp_path, monkeypatch)
        assert cpds.main([]) == 0
        out = capsys.readouterr().out
        assert "1 candidate mismatch" in out
        assert "Prompt 1 claims 'open' but plan.md \xa75 reads 'done'" in out

    def test_strict_exits_1_on_mismatch(self, tmp_path, monkeypatch):
        self._repo(tmp_path, monkeypatch)
        assert cpds.main(["--strict"]) == 1

    def test_no_mismatch_message(self, tmp_path, monkeypatch, capsys):
        self._repo(tmp_path, monkeypatch)
        (tmp_path / "claude_code_prompts.md").write_text(
            "## Prompt 1 -- \xa75: thing\n\n\u2705 done\n", encoding="utf-8")
        assert cpds.main(["--strict"]) == 0
        assert "no status mismatches found" in capsys.readouterr().out

    def test_missing_files_are_skipped_not_an_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cpds, "REPO_ROOT", tmp_path)
        monkeypatch.chdir(tmp_path)
        assert cpds.main([]) == 0
        assert "skipping (missing" in capsys.readouterr().out

    def test_dotdot_traversal_argument_is_rejected_with_a_clear_error(
            self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        (tmp_path / "secret.md").write_text("# secret", encoding="utf-8")
        monkeypatch.setattr(cpds, "REPO_ROOT", repo)
        monkeypatch.chdir(repo)
        with pytest.raises(SystemExit) as excinfo:
            cpds.main(["--plan", "../secret.md"])
        assert excinfo.value.code == 2
        assert "outside the repository root" in capsys.readouterr().err

    def test_absolute_path_escape_argument_is_rejected(self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("# outside", encoding="utf-8")
        monkeypatch.setattr(cpds, "REPO_ROOT", repo)
        monkeypatch.chdir(repo)
        with pytest.raises(SystemExit) as excinfo:
            cpds.main(["--prompts", str(outside)])
        assert excinfo.value.code == 2
        assert "outside the repository root" in capsys.readouterr().err

    def test_rejected_path_is_never_read(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside.md"
        outside.write_text("# outside", encoding="utf-8")
        monkeypatch.setattr(cpds, "REPO_ROOT", repo)
        monkeypatch.chdir(repo)
        reads = []
        real_read_text = Path.read_text
        monkeypatch.setattr(Path, "read_text",
                            lambda self, *a, **k: reads.append(self) or real_read_text(self, *a, **k))
        with pytest.raises(SystemExit):
            cpds.main(["--plan", str(outside)])
        assert reads == []
