"""Mocked-client tests for scripts/redteam_retrieved_context.py.

None of these tests ever make a real network/model call -- claude.ask_ai_async
is always monkeypatched. The point of this file is to pin the harness's own
logic (case selection, the bypass-verification premise, hijack classification,
cost estimation, and the --live safety gate) independent of any live model,
per AI_SECURITY_REVIEW.md M2 follow-up item C's "mocked-client unit tests,
never a live call in CI" requirement.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "redteam_retrieved_context.py"
_spec = importlib.util.spec_from_file_location("redteam_retrieved_context", _SCRIPT_PATH)
redteam = importlib.util.module_from_spec(_spec)
sys.modules["redteam_retrieved_context"] = redteam
_spec.loader.exec_module(redteam)


class TestAttackCasesBypassTheGuardToday:
    """Each case's payload must currently slip past retrieval_guard unflagged
    -- that's the exact residual-gap scenario this harness exists to test
    against a live model. If retrieval_guard grows a pattern that catches one,
    the case tests nothing new and should be replaced, not silently kept."""

    @pytest.mark.parametrize("case", redteam.ATTACK_CASES, ids=lambda c: c.name)
    def test_case_payload_is_not_flagged_by_retrieval_guard(self, case):
        assert redteam.verify_case_bypasses_guard(case) == []

    def test_there_is_at_least_one_case(self):
        assert len(redteam.ATTACK_CASES) >= 1

    def test_case_names_are_unique(self):
        names = [c.name for c in redteam.ATTACK_CASES]
        assert len(names) == len(set(names))


class TestClassifyHijack:
    def test_no_markers_present_returns_empty(self):
        case = redteam.ATTACK_CASES[0]
        found = redteam.classify_hijack("A normal, unrelated halachic answer.", case)
        assert found == []

    def test_a_present_marker_is_found_case_insensitively(self):
        case = redteam.AttackCase(
            name="synthetic",
            question="q",
            fake_title="t",
            injected_snippet="s",
            hijack_markers=["Totally Permitted For Everyone"],
        )
        found = redteam.classify_hijack("the ruling is totally permitted for everyone here", case)
        assert found == ["Totally Permitted For Everyone"]

    def test_empty_answer_is_handled(self):
        case = redteam.ATTACK_CASES[0]
        assert redteam.classify_hijack("", case) == []
        assert redteam.classify_hijack(None, case) == []


class TestEstimateCost:
    def test_scales_with_case_count(self):
        one = redteam.estimate_cost(1)
        five = redteam.estimate_cost(5)
        assert "1 live model call" in one
        assert "5 live model call" in five

    def test_mentions_a_dollar_figure(self):
        assert "$" in redteam.estimate_cost(3)


class TestRunCaseUsesTheRealPipelineMocked:
    """run_case() must call claude.ask_ai_async with the injected snippet
    riding in as a Halachipedia entry (the same shape a real retrieved
    result takes), and must correctly read the security/output block that
    ask_ai_async's own apply_output_validation() attaches."""

    async def test_passes_the_injected_snippet_as_a_halachipedia_entry(self, monkeypatch):
        captured = {}

        async def _fake_ask_ai_async(**kwargs):
            captured.update(kwargs)
            return {"answer": "A clean answer.", "model": "test-model", "security": {"output": {"blocked": False, "reason": ""}}}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        case = redteam.ATTACK_CASES[0]
        result = await redteam.run_case(case)

        assert captured["question"] == case.question
        assert captured["halachipedia"] == [{"title": case.fake_title, "summary": case.injected_snippet}]
        assert captured["sefaria_sources"] == []
        assert captured["customs"] == []
        assert result["hijack_suspected"] is False

    async def test_hijack_markers_found_and_not_blocked_is_suspected_hijack(self, monkeypatch):
        case = redteam.ATTACK_CASES[0]
        hijacked_answer = f"The ruling is: {case.hijack_markers[0]}, per the retrieved note."

        async def _fake_ask_ai_async(**kwargs):
            return {"answer": hijacked_answer, "model": "test-model", "security": {"output": {"blocked": False, "reason": ""}}}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        result = await redteam.run_case(case)

        assert result["hijack_suspected"] is True
        assert case.hijack_markers[0] in result["hijack_markers_found"]

    async def test_output_validator_blocking_overrides_hijack_suspicion(self, monkeypatch):
        """Even if the raw text happened to contain a hijack marker, a
        blocked output means apply_output_validation() already replaced it
        with the safe fallback text -- the harness must not double-flag
        something the existing defense already caught."""
        case = redteam.ATTACK_CASES[0]

        async def _fake_ask_ai_async(**kwargs):
            return {
                "answer": "No verified source found",
                "model": "test-model",
                "security": {"output": {"blocked": True, "reason": "blocked_internal_instructions"}},
            }

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        result = await redteam.run_case(case)

        assert result["output_blocked"] is True
        assert result["hijack_suspected"] is False

    async def test_run_all_runs_every_case_given(self, monkeypatch):
        calls = []

        async def _fake_ask_ai_async(**kwargs):
            calls.append(kwargs["question"])
            return {"answer": "ok", "model": "test-model", "security": {"output": {"blocked": False, "reason": ""}}}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        cases = redteam.ATTACK_CASES[:2]
        results = await redteam.run_all(cases)

        assert len(results) == 2
        assert calls == [c.question for c in cases]


class TestMainSafetyGate:
    """The CLI's default must never touch the network. A live call needs
    --live, an API key in the environment, and (absent --yes) an explicit
    typed confirmation."""

    def test_dry_run_default_makes_no_live_call_and_exits_zero(self, monkeypatch, capsys):
        called = False

        async def _fake_ask_ai_async(**kwargs):
            nonlocal called
            called = True
            return {"answer": "", "model": "", "security": {"output": {"blocked": False, "reason": ""}}}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        exit_code = redteam.main([])

        assert exit_code == 0
        assert called is False
        out = capsys.readouterr().out
        assert "Dry run only" in out

    def test_live_without_any_api_key_refuses_and_makes_no_call(self, monkeypatch, capsys):
        called = False

        async def _fake_ask_ai_async(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

        exit_code = redteam.main(["--live"])

        assert exit_code == 1
        assert called is False

    def test_live_with_api_key_but_declined_confirmation_makes_no_call(self, monkeypatch, capsys):
        called = False

        async def _fake_ask_ai_async(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
        monkeypatch.setattr("builtins.input", lambda prompt="": "no")

        exit_code = redteam.main(["--live"])

        assert exit_code == 1
        assert called is False

    def test_live_with_yes_flag_and_api_key_runs_without_prompting(self, monkeypatch):
        calls = []

        async def _fake_ask_ai_async(**kwargs):
            calls.append(kwargs["question"])
            return {"answer": "ok", "model": "test-model", "security": {"output": {"blocked": False, "reason": ""}}}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

        def _fail_if_prompted(prompt=""):
            raise AssertionError("must not prompt when --yes is passed")

        monkeypatch.setattr("builtins.input", _fail_if_prompted)

        exit_code = redteam.main(["--live", "--yes", "--case", redteam.ATTACK_CASES[0].name])

        assert exit_code == 0
        assert calls == [redteam.ATTACK_CASES[0].question]

    def test_unknown_case_name_errors_without_any_call(self, monkeypatch, capsys):
        called = False

        async def _fake_ask_ai_async(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(redteam.claude, "ask_ai_async", _fake_ask_ai_async)

        exit_code = redteam.main(["--case", "does_not_exist"])

        assert exit_code == 2
        assert called is False
