"""
Behavioural coverage for the remaining uncovered lines of app.py:

  * _coerce_ai_answer_shape's "nothing usable" early return,
  * the pyluach holiday fallback's per-date failure isolation,
  * the Hebrew prayer-shortcut branch,
  * _collect_primary_sources_sync's blank-ref skipping and per-source failure
    isolation,
  * _security_blocked_ask_payload's default answer and non-dict "structured"
    handling,
  * _dispatch_ask_ai_synthesis_call's agentic (AI_AGENTIC_TOOLS) branch,
  * _extract_raw_ai_answer / _resolve_ask_web_warning_flag /
    _compose_validated_ask_answer edge cases,
  * ask_question's outermost "critical error" handler, and
  * app.py's module-level startup code (customs-validation toggle, ephemeral
    secret-key fallback, blueprint-registration failure, __main__ block),
    exercised by executing an isolated copy of the source file under a
    different module name (or as __main__ via runpy) with every global side
    effect stubbed, so the already-imported ``app`` module is never touched.
"""

from __future__ import annotations

import importlib
import importlib.util
import itertools
import logging
import runpy
import sys
import threading
import types
from pathlib import Path
from unittest import mock

import dotenv
import flask
import pytest

import app as flask_app_module
from backend import customs as backend_customs
from backend import logging_setup

app = flask_app_module

ASK_ERROR_BODY = {"error": "An internal error occurred while processing your request."}


# ─── _coerce_ai_answer_shape: nothing usable to structure ───────────────────

class TestCoerceAiAnswerShapeNothingUsable:
    @pytest.mark.parametrize("answer", ["", "   \n "])
    def test_blank_answer_without_structure_is_returned_untouched(self, answer):
        result = {"answer": answer}

        out = app._coerce_ai_answer_shape(result, "Can I?", "balanced")

        assert out is result
        # Nothing was rendered or attached: no "structured" key, answer as-is.
        assert out == {"answer": answer}

    def test_non_dict_structured_with_blank_answer_is_left_alone(self):
        result = {"answer": "", "structured": "not a dict"}

        out = app._coerce_ai_answer_shape(result, "Can I?", "balanced")

        assert out is result
        assert out == {"answer": "", "structured": "not a dict"}

    def test_a_usable_answer_by_contrast_is_rewritten(self):
        out = app._coerce_ai_answer_shape({"answer": "Just prose."}, "Can I?", "balanced")

        assert out["structured"]["ruling"] == "Just prose."


# ─── _build_pyluach_holiday_events: one bad date must not abort the year ────

class TestPyluachHolidayFallbackFailureIsolation:
    def test_a_date_that_raises_is_skipped_and_the_rest_of_the_year_survives(self, monkeypatch):
        baseline = app._build_pyluach_holiday_events(2026)
        assert "2026-09-21" in {e["start"] for e in baseline}  # Yom Kippur

        real_gregorian_date = app.pyluach_dates.GregorianDate

        def flaky_gregorian_date(year, month, day):
            if (year, month, day) == (2026, 9, 21):
                raise RuntimeError("pyluach exploded for this one date")
            return real_gregorian_date(year, month, day)

        monkeypatch.setattr(app.pyluach_dates, "GregorianDate", flaky_gregorian_date)

        events = app._build_pyluach_holiday_events(2026)

        starts = [e["start"] for e in events]
        assert "2026-09-21" not in starts
        assert len(events) == len(baseline) - 1
        # The loop kept advancing past the failure: later holidays are present
        # and the output is still chronological.
        assert "2026-09-26" in starts  # Sukkot, the first holiday after Yom Kippur
        assert starts == sorted(starts)
        assert [e for e in baseline if e["start"] != "2026-09-21"] == events

    def test_every_date_failing_yields_an_empty_list_instead_of_raising(self, monkeypatch):
        def always_fails(year, month, day):
            raise RuntimeError("pyluach is down")

        monkeypatch.setattr(app.pyluach_dates, "GregorianDate", always_fails)

        assert app._build_pyluach_holiday_events(2026) == []


# ─── _ask_question_prayer_payload: Hebrew answer text ───────────────────────

class TestPrayerPayloadHebrew:
    @pytest.fixture
    def stats(self, monkeypatch):
        fresh = {"answers_total": 0, "fallback_answers": 0, "strict_blocks": 0, "segment_reports": 0}
        monkeypatch.setattr(app, "DEVTOOLS_STATS", fresh)
        return fresh

    def test_hebrew_language_gets_the_hebrew_guide_text(self, stats):
        question = "מתי Shacharit?"

        payload = app._ask_question_prayer_payload(question, "balanced", "he", "All")

        assert payload["answer"].startswith(f"מדריך תפילה\n\n{question}\n\n")
        assert "הרב שלך" in payload["answer"]
        assert "Prayer Service Guide" not in payload["answer"]
        assert payload["meta"]["language"] == "he"
        assert payload["meta"]["mode"] == "balanced"
        assert payload["meta"]["community_lens"] == "All"
        assert payload["meta"]["source_count"] == 1
        assert payload["confidence"] == 0.85
        assert payload["sources"][0]["ref"] == "Sefaria Liturgy"
        assert stats["answers_total"] == 1

    def test_english_language_keeps_the_english_guide_text(self, stats):
        payload = app._ask_question_prayer_payload("When is Mincha?", "balanced", "en", "All")

        assert payload["answer"].startswith("Prayer Service Guide\n\nWhen is Mincha?\n\n")
        assert "מדריך תפילה" not in payload["answer"]
        assert stats["answers_total"] == 1

    def test_non_prayer_question_returns_none_and_counts_nothing(self, stats):
        assert app._ask_question_prayer_payload("Is it kosher?", "balanced", "he", "All") is None
        assert stats["answers_total"] == 0


# ─── _collect_primary_sources_sync ──────────────────────────────────────────

class _FakeEngine:
    """Engine stub whose get_library_text records every ref it is asked for."""

    def __init__(self, responses):
        self._responses = responses
        self.requested = []
        self._lock = threading.Lock()

    def get_library_text(self, ref):
        with self._lock:
            self.requested.append(ref)
        outcome = self._responses.get(ref, {"ref": ref})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class TestCollectPrimarySourcesSync:
    def test_blank_refs_are_skipped_and_do_not_use_up_the_limit(self, monkeypatch):
        monkeypatch.setattr(
            app.sefaria, "find_refs_for_question",
            lambda question: ["", "   ", None, "Genesis 1:1", "Exodus 2:2", "Leviticus 3:3"],
        )
        monkeypatch.setenv("ASK_PRIMARY_SOURCE_LIMIT", "2")
        engine = _FakeEngine({})

        sources = app._collect_primary_sources_sync("q", engine)

        assert sorted(engine.requested) == ["Exodus 2:2", "Genesis 1:1"]
        assert sources == [{"ref": "Genesis 1:1"}, {"ref": "Exodus 2:2"}]

    def test_refs_are_stripped_before_being_fetched(self, monkeypatch):
        monkeypatch.setattr(app.sefaria, "find_refs_for_question", lambda question: ["  Genesis 1:1  "])
        engine = _FakeEngine({})

        assert app._collect_primary_sources_sync("q", engine) == [{"ref": "Genesis 1:1"}]
        assert engine.requested == ["Genesis 1:1"]

    def test_a_failing_source_is_dropped_without_losing_the_others(self, monkeypatch):
        monkeypatch.setattr(
            app.sefaria, "find_refs_for_question",
            lambda question: ["Genesis 1:1", "Bad 9:9", "Exodus 2:2"],
        )
        engine = _FakeEngine({"Bad 9:9": RuntimeError("sefaria lookup failed")})

        sources = app._collect_primary_sources_sync("q", engine)

        assert sources == [{"ref": "Genesis 1:1"}, {"ref": "Exodus 2:2"}]
        assert sorted(engine.requested) == ["Bad 9:9", "Exodus 2:2", "Genesis 1:1"]

    def test_non_dict_source_payloads_are_dropped(self, monkeypatch):
        monkeypatch.setattr(app.sefaria, "find_refs_for_question", lambda question: ["A 1:1", "B 2:2"])
        engine = _FakeEngine({"A 1:1": "not a dict"})

        assert app._collect_primary_sources_sync("q", engine) == [{"ref": "B 2:2"}]

    def test_only_blank_refs_mean_no_lookups_at_all(self, monkeypatch):
        monkeypatch.setattr(app.sefaria, "find_refs_for_question", lambda question: ["", None, "  "])
        engine = _FakeEngine({})

        assert app._collect_primary_sources_sync("q", engine) == []
        assert engine.requested == []


# ─── _security_blocked_ask_payload ──────────────────────────────────────────

class TestSecurityBlockedAskPayload:
    @pytest.fixture
    def history(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "_store_ask_history", lambda *a, **k: calls.append((a, k)))
        monkeypatch.setattr(
            app, "DEVTOOLS_STATS",
            {"answers_total": 0, "fallback_answers": 0, "strict_blocks": 0, "segment_reports": 0},
        )
        return calls

    def _payload(self, result, **overrides):
        kwargs = {
            "mode": "balanced", "canonical_lens": "All", "knowledge_rows": [], "user_memory_summaries": [],
            "user_id": "user-1", "question_was_sanitized": False,
            "question": "some blocked question", "answer_language": "en",
        }
        kwargs.update(overrides)
        return app._security_blocked_ask_payload(result, **kwargs)

    @pytest.mark.parametrize("result", [{"answer": ""}, {"answer": "   "}, {}])
    def test_blank_answer_is_replaced_by_the_default_block_message(self, history, result):
        payload = self._payload(result)

        expected = "Request blocked by security policy. Please submit a direct halakhic question."
        assert payload["answer"] == expected
        # The same default text is what gets retained for dispute reconstruction.
        (args, kwargs), = history
        assert args == ("user-1", "some blocked question", expected)
        assert kwargs["sources"] == []
        assert kwargs["ai_cited_sources"] == []
        assert kwargs["safety_class"] == "ok"

    def test_provided_answer_is_kept_and_stripped(self, history):
        payload = self._payload({"answer": "  Blocked: off-topic.  "})

        assert payload["answer"] == "Blocked: off-topic."
        assert history[0][0][2] == "Blocked: off-topic."

    def test_non_dict_structured_payload_is_ignored_and_defaults_apply(self, history):
        payload = self._payload({"answer": "no", "structured": "junk", "security": {"reason": "domain"}})

        meta = payload["meta"]
        assert meta["safety_class"] == "ok"
        assert meta["is_prohibited"] is False
        assert meta["rabbinic_disclaimer"] == app.claude.RABBI_FINAL_RULING_FOOTER
        assert meta["security"] == {"reason": "domain"}
        assert history[0][1]["safety_class"] == "ok"

    def test_dict_structured_payload_by_contrast_drives_the_safety_meta(self, history):
        structured = {"safety_class": "medical", "is_prohibited": True, "rabbinic_disclaimer": "See a doctor."}

        payload = self._payload({"answer": "no", "structured": structured})

        meta = payload["meta"]
        assert meta["safety_class"] == "medical"
        assert meta["is_prohibited"] is True
        assert meta["rabbinic_disclaimer"] == "See a doctor."
        assert history[0][1]["safety_class"] == "medical"

    def test_block_is_counted_as_an_answered_fallback(self, history):
        self._payload({"answer": "no"})

        assert app.DEVTOOLS_STATS["answers_total"] == 1
        assert app.DEVTOOLS_STATS["fallback_answers"] == 1


# ─── _dispatch_ask_ai_synthesis_call ────────────────────────────────────────

class TestDispatchAskAiSynthesisCall:
    CTX = {
        "flat_sources_for_claude": ["src"],
        "customs_info": ["custom"],
        "user_memory_summaries": ["memory"],
        "wiki_context_for_claude": ["wiki"],
        "halachipedia_list": ["hp"],
    }

    def _expected_kwargs(self):
        return {
            "question": "May I?",
            "sefaria_sources": ["src"],
            "customs": ["custom"],
            "user_memories": ["memory"],
            "wiki": ["wiki"],
            "halachipedia": ["hp"],
            "mode": "practical",
            "community_lens": "Persian",
            "answer_language": "he",
            "tool_context": {"tool": "context"},
        }

    def test_agentic_flag_runs_the_agentic_loop_on_a_worker_thread(self, monkeypatch):
        seen = {}

        async def fake_run_agentic_ask(**kwargs):
            seen["kwargs"] = kwargs
            seen["thread"] = threading.current_thread()
            return {"answer": "agentic", "used_web_search": True}

        plain_calls = []
        monkeypatch.setattr(app.claude, "AI_AGENTIC_TOOLS", True)
        monkeypatch.setattr(app.ask_pipeline, "run_agentic_ask", fake_run_agentic_ask)
        monkeypatch.setattr(app.claude, "ask_claude", lambda **kw: plain_calls.append(kw))
        monkeypatch.setattr(app, "_build_ask_tool_context", lambda engine: {"tool": "context"})

        result = app._dispatch_ask_ai_synthesis_call(
            "May I?", "practical", "Persian", "he", self.CTX, object())

        assert result == {"answer": "agentic", "used_web_search": True}
        assert seen["kwargs"] == self._expected_kwargs()
        assert seen["thread"] is not threading.current_thread()
        assert plain_calls == []

    def test_agentic_loop_errors_propagate_to_the_caller(self, monkeypatch):
        async def failing_run_agentic_ask(**kwargs):
            raise RuntimeError("agent loop failed")

        monkeypatch.setattr(app.claude, "AI_AGENTIC_TOOLS", True)
        monkeypatch.setattr(app.ask_pipeline, "run_agentic_ask", failing_run_agentic_ask)
        monkeypatch.setattr(app, "_build_ask_tool_context", lambda engine: {})

        with pytest.raises(RuntimeError, match="agent loop failed"):
            app._dispatch_ask_ai_synthesis_call("May I?", "practical", "All", "en", self.CTX, object())

    def test_flag_off_calls_ask_claude_with_the_same_kwargs(self, monkeypatch):
        seen = {}

        def fake_ask_claude(**kwargs):
            seen["kwargs"] = kwargs
            return {"answer": "plain"}

        async def agentic_must_not_run(**kwargs):
            raise AssertionError("agentic loop must stay off by default")

        monkeypatch.setattr(app.claude, "AI_AGENTIC_TOOLS", False)
        monkeypatch.setattr(app.claude, "ask_claude", fake_ask_claude)
        monkeypatch.setattr(app.ask_pipeline, "run_agentic_ask", agentic_must_not_run)
        monkeypatch.setattr(app, "_build_ask_tool_context", lambda engine: {"tool": "context"})

        result = app._dispatch_ask_ai_synthesis_call(
            "May I?", "practical", "Persian", "he", self.CTX, object())

        assert result == {"answer": "plain"}
        assert seen["kwargs"] == self._expected_kwargs()


# ─── _extract_raw_ai_answer ─────────────────────────────────────────────────

class TestExtractRawAiAnswer:
    def test_non_dict_structured_falls_back_to_the_plain_answer_text(self):
        structured, raw = app._extract_raw_ai_answer({"structured": "junk", "answer": "  hello  "}, "en")

        assert structured is None
        assert raw == "hello"

    def test_empty_structured_dict_also_falls_back_to_the_plain_answer_text(self):
        structured, raw = app._extract_raw_ai_answer({"structured": {}, "answer": "prose"}, "en")

        assert structured == {}
        assert raw == "prose"

    def test_missing_structured_uses_the_answer_text(self):
        assert app._extract_raw_ai_answer({"answer": "plain"}, "he") == (None, "plain")

    @pytest.mark.parametrize("result", [
        {}, {"answer": ""}, {"answer": "   "}, {"answer": None}, {"structured": "junk"},
        {"structured": ["a"], "answer": " "},
    ])
    def test_no_usable_text_raises(self, result):
        with pytest.raises(RuntimeError, match="AI response was empty"):
            app._extract_raw_ai_answer(result, "en")

    def test_structured_dict_is_rendered_instead_of_the_answer_text(self):
        structured, raw = app._extract_raw_ai_answer(
            {"structured": {"ruling": "It is permitted."}, "answer": "IGNORED"}, "en")

        assert structured == {"ruling": "It is permitted."}
        assert raw == "It is permitted."


# ─── _resolve_ask_web_warning_flag ──────────────────────────────────────────

class TestResolveAskWebWarningFlag:
    def test_agentic_web_search_flag_wins_even_without_prefetched_wiki(self):
        ctx = {"use_tertiary_web_context": False, "wiki_context_for_claude": []}

        assert app._resolve_ask_web_warning_flag({"used_web_search": True}, ctx) is True

    def test_agentic_no_web_search_overrides_a_tertiary_web_context(self):
        ctx = {"use_tertiary_web_context": True, "wiki_context_for_claude": ["wiki page"]}

        assert app._resolve_ask_web_warning_flag({"used_web_search": False}, ctx) is False

    @pytest.mark.parametrize("raw, expected", [(1, True), (0, False), ("yes", True), (None, False)])
    def test_flag_value_is_coerced_to_bool(self, raw, expected):
        ctx = {"use_tertiary_web_context": True, "wiki_context_for_claude": ["wiki page"]}

        assert app._resolve_ask_web_warning_flag({"used_web_search": raw}, ctx) is expected

    @pytest.mark.parametrize("tertiary, wiki, expected", [
        (True, ["page"], True),
        (True, [], False),
        (False, ["page"], False),
    ])
    def test_without_the_agentic_key_the_prefetch_context_decides(self, tertiary, wiki, expected):
        ctx = {"use_tertiary_web_context": tertiary, "wiki_context_for_claude": wiki}

        assert app._resolve_ask_web_warning_flag({"answer": "x"}, ctx) is expected


# ─── _compose_validated_ask_answer ──────────────────────────────────────────

class TestComposeValidatedAskAnswer:
    @pytest.mark.parametrize("needs_web_warning", [False, True])
    @pytest.mark.parametrize("raw", ["", "   \n"])
    def test_blank_body_raises_even_when_a_web_warning_would_be_prepended(self, raw, needs_web_warning):
        with pytest.raises(RuntimeError, match="AI response normalized to empty content"):
            app._compose_validated_ask_answer(raw, needs_web_warning)

    def test_body_is_returned_unchanged_without_a_warning(self):
        assert app._compose_validated_ask_answer("  The answer.  ", False) == "The answer."

    def test_web_warning_is_prepended_when_requested(self):
        out = app._compose_validated_ask_answer("The answer.", True)

        assert out == f"{app.WEB_LAST_RESORT_WARNING}\n\nThe answer."


# ─── ask_question: outermost critical-error handler ─────────────────────────

class TestAskRouteCriticalError:
    QUESTION = "Is it permitted to test the critical error path?"

    @pytest.fixture
    def captured(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            app, "_capture_backend_error",
            lambda event_name, error, context=None: seen.append((event_name, error, context)),
        )
        monkeypatch.setattr(app, "ASK_RESPONSE_CACHE", {})
        return seen

    def _post(self, test_client, **extra):
        body = {"question": self.QUESTION, "language": "he", "mode": "sources", "community": "All"}
        body.update(extra)
        return test_client.post("/ask", json=body)

    def test_engine_failure_returns_a_generic_500_and_reports_the_error(self, test_client, monkeypatch, captured):
        boom = RuntimeError("engine exploded: secret detail")

        def failing_get_engine():
            raise boom

        monkeypatch.setattr(app, "get_engine", failing_get_engine)

        response = self._post(test_client)

        assert response.status_code == 500
        assert response.get_json() == ASK_ERROR_BODY
        assert "secret detail" not in response.get_data(as_text=True)
        assert len(captured) == 1
        event_name, error, context = captured[0]
        assert event_name == "ask_route_critical_error"
        assert error is boom
        assert context == {
            "question": self.QUESTION,
            "mode": "sources",
            "community_lens": "All",
            "input_length_bucket": logging_setup.question_length_bucket(self.QUESTION),
            "language": "he",
        }

    def test_context_gathering_failure_is_not_absorbed_by_the_ai_fallback(self, test_client, monkeypatch, captured):
        boom = ValueError("context collection failed")

        def failing_collect(*args, **kwargs):
            raise boom

        fallback_calls = []
        monkeypatch.setattr(app, "get_engine", lambda: object())
        monkeypatch.setattr(app, "_collect_ask_question_context", failing_collect)
        monkeypatch.setattr(app, "_run_ask_question_fallback", lambda *a, **k: fallback_calls.append(a))

        response = self._post(test_client)

        assert response.status_code == 500
        assert response.get_json() == ASK_ERROR_BODY
        assert [(name, err) for name, err, _ in captured] == [("ask_route_critical_error", boom)]
        assert fallback_calls == []
        assert app.ASK_RESPONSE_CACHE == {}

    def test_a_failing_fallback_after_an_ai_failure_still_ends_in_the_500_handler(
        self, test_client, monkeypatch, captured,
    ):
        ai_failure = RuntimeError("model down")
        fallback_failure = OSError("fallback down too")

        def failing_synthesis(*args, **kwargs):
            raise ai_failure

        def failing_fallback(*args, **kwargs):
            raise fallback_failure

        monkeypatch.setattr(app, "get_engine", lambda: object())
        monkeypatch.setattr(app, "_collect_ask_question_context", lambda *a, **k: {})
        monkeypatch.setattr(app, "_ask_question_strict_payload", lambda *a, **k: None)
        monkeypatch.setattr(app, "_run_ask_question_ai_synthesis", failing_synthesis)
        monkeypatch.setattr(app, "_run_ask_question_fallback", failing_fallback)

        response = self._post(test_client)

        assert response.status_code == 500
        assert response.get_json() == ASK_ERROR_BODY
        assert [(name, err) for name, err, _ in captured] == [("ask_route_critical_error", fallback_failure)]
        assert app.ASK_RESPONSE_CACHE == {}


# ─── module-level startup code, via isolated executions of app.py ───────────

APP_SOURCE = Path(flask_app_module.__file__).resolve()
_COPY_COUNTER = itertools.count()
_REAL_IMPORT_MODULE = importlib.import_module


@pytest.fixture
def startup_stubs(monkeypatch):
    """Neutralise every process-global side effect of executing app.py again.

    * dotenv.load_dotenv(override=True) would clobber os.environ with the
      developer's real .env.
    * setup_logging() would reset the root logger level/handlers.
    * validate_all_customs_at_startup() walks the customs corpus.
    * Flask.run() would start a real dev server.

    Each stub records its calls so tests can assert on them.
    """
    calls = {"load_dotenv": [], "setup_logging": [], "validate": [], "run": []}

    def fake_load_dotenv(*args, **kwargs):
        calls["load_dotenv"].append((args, kwargs))
        return False

    def fake_setup_logging(*args, **kwargs):
        calls["setup_logging"].append((args, kwargs))

    def fake_validate():
        calls["validate"].append(True)

    def fake_run(self, *args, **kwargs):
        calls["run"].append({"app": self, "args": args, "kwargs": kwargs})

    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)
    monkeypatch.setattr(logging_setup, "setup_logging", fake_setup_logging)
    monkeypatch.setattr(backend_customs, "validate_all_customs_at_startup", fake_validate)
    monkeypatch.setattr(flask.Flask, "run", fake_run)
    # Deterministic runtime flavour unless a test overrides it.
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("FLASK_ENV", "testing")
    monkeypatch.delenv("VALIDATE_CUSTOMS_AT_STARTUP", raising=False)
    monkeypatch.setenv("FLASK_SECRET_KEY", "isolated-test-secret")
    return calls


def _new_copy_name() -> str:
    return f"_shelah_app_isolated_copy_{next(_COPY_COUNTER)}"


def _exec_copy(monkeypatch, name: str) -> types.ModuleType:
    """Execute a private copy of app.py as module ``name``.

    The module is registered in sys.modules (Flask resolves its root path from
    there) through monkeypatch so it is removed again at teardown, even when
    execution raises part-way through.
    """
    spec = importlib.util.spec_from_file_location(name, APP_SOURCE)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _load_copy(monkeypatch) -> types.ModuleType:
    return _exec_copy(monkeypatch, _new_copy_name())


class TestStartupCustomsValidationToggle:
    @pytest.mark.parametrize("validate_env, vercel, flask_env, expect_prod, expect_validate", [
        # Explicit opt-in wins, even in production.
        ("1", "1", "testing", True, True),
        ("true", None, "production", True, True),
        ("  YES  ", "1", "", True, True),
        ("on", None, "testing", False, True),
        # Explicit opt-out wins, even in development.
        ("0", None, "testing", False, False),
        ("False", None, "testing", False, False),
        ("no", None, "testing", False, False),
        ("OFF", None, "testing", False, False),
        # No override: validate in development, skip in production.
        ("", None, "testing", False, True),
        ("   ", None, "testing", False, True),
        ("", "1", "testing", True, False),
        ("", None, " Production ", True, False),
        ("", "0", "testing", False, True),
    ])
    def test_validation_runs_per_override_else_per_runtime(
        self, monkeypatch, startup_stubs, validate_env, vercel, flask_env, expect_prod, expect_validate,
    ):
        monkeypatch.setenv("VALIDATE_CUSTOMS_AT_STARTUP", validate_env)
        monkeypatch.setenv("FLASK_ENV", flask_env)
        if vercel is None:
            monkeypatch.delenv("VERCEL", raising=False)
        else:
            monkeypatch.setenv("VERCEL", vercel)

        module = _load_copy(monkeypatch)

        assert module.is_production_runtime is expect_prod
        assert module._should_validate_customs_at_startup is expect_validate
        assert startup_stubs["validate"] == ([True] if expect_validate else [])

    def test_execution_reads_dotenv_with_override_and_configures_logging_once(self, monkeypatch, startup_stubs):
        _load_copy(monkeypatch)

        assert startup_stubs["load_dotenv"] == [((), {"override": True})]
        assert len(startup_stubs["setup_logging"]) == 1


class TestStartupSecretKey:
    def test_missing_key_falls_back_to_a_random_ephemeral_key_and_logs_critical(
        self, monkeypatch, startup_stubs, caplog,
    ):
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        caplog.set_level(logging.CRITICAL)

        first = _load_copy(monkeypatch)
        second = _load_copy(monkeypatch)

        for module in (first, second):
            assert isinstance(module.app.secret_key, bytes)
            assert len(module.app.secret_key) == 32
            assert module._flask_secret is module.app.secret_key
        # Ephemeral means fresh entropy per process, not a constant fallback.
        assert first.app.secret_key != second.app.secret_key

        for module in (first, second):
            records = [r for r in caplog.records if r.name == module.__name__]
            assert len(records) == 1
            assert records[0].levelno == logging.CRITICAL
            message = records[0].getMessage()
            assert "FLASK_SECRET_KEY is not set" in message
            assert "invalidated on every process restart" in message

    def test_empty_key_is_treated_as_missing(self, monkeypatch, startup_stubs, caplog):
        monkeypatch.setenv("FLASK_SECRET_KEY", "")
        caplog.set_level(logging.CRITICAL)

        module = _load_copy(monkeypatch)

        assert isinstance(module.app.secret_key, bytes)
        assert [r for r in caplog.records if r.name == module.__name__]

    def test_configured_key_is_used_verbatim_and_nothing_is_logged(self, monkeypatch, startup_stubs, caplog):
        monkeypatch.setenv("FLASK_SECRET_KEY", "stable-key-from-env")
        caplog.set_level(logging.CRITICAL)

        module = _load_copy(monkeypatch)

        assert module.app.secret_key == "stable-key-from-env"
        assert [r for r in caplog.records if r.name == module.__name__] == []


class TestStartupBlueprintRegistration:
    def test_a_clean_import_registers_every_blueprint_on_the_new_app(self, monkeypatch, startup_stubs):
        module = _load_copy(monkeypatch)

        assert len(module.app.blueprints) == 11
        assert module.app.blueprints.keys() == flask_app_module.app.blueprints.keys()
        # The registration list and its helper names are cleaned up afterwards.
        for leaked in ("_BLUEPRINTS", "_importlib", "_mod_path", "_bp_name", "_mod", "_bp"):
            assert not hasattr(module, leaked)

    @pytest.mark.parametrize("failing_path, blueprints_registered_before", [
        ("backend.routes_library", 0),
        ("backend.routes_calendar", 3),
        ("backend.routes_webhooks", 10),
    ])
    def test_a_failing_import_aborts_startup_with_the_offending_module_named(
        self, monkeypatch, startup_stubs, caplog, failing_path, blueprints_registered_before,
    ):
        original = ImportError(f"cannot import {failing_path}")
        blueprint_name = failing_path.rsplit(".", 1)[1]
        caplog.set_level(logging.CRITICAL)

        def fake_import_module(name, package=None):
            if name == failing_path:
                raise original
            return _REAL_IMPORT_MODULE(name, package)

        name = _new_copy_name()
        with mock.patch.object(importlib, "import_module", fake_import_module):
            with pytest.raises(RuntimeError) as excinfo:
                _exec_copy(monkeypatch, name)

        assert str(excinfo.value) == (
            f"Blueprint '{blueprint_name}' from '{failing_path}' failed to load: {original}"
        )
        assert excinfo.value.__cause__ is original
        # Blueprints ahead of the broken one were registered; none after it.
        assert len(sys.modules[name].app.blueprints) == blueprints_registered_before

        records = [r for r in caplog.records if r.name == name]
        assert len(records) == 1
        assert records[0].levelno == logging.CRITICAL
        assert records[0].getMessage() == (
            f"FATAL: blueprint registration failed for {failing_path}.{blueprint_name} — {original}"
        )
        assert records[0].exc_info[1] is original

    def test_a_module_missing_its_blueprint_object_also_aborts_startup(self, monkeypatch, startup_stubs, caplog):
        caplog.set_level(logging.CRITICAL)
        hollow_module = types.ModuleType("backend.routes_prayers")

        def fake_import_module(name, package=None):
            if name == "backend.routes_prayers":
                return hollow_module
            return _REAL_IMPORT_MODULE(name, package)

        name = _new_copy_name()
        with mock.patch.object(importlib, "import_module", fake_import_module):
            with pytest.raises(RuntimeError, match="Blueprint 'routes_prayers' from 'backend.routes_prayers'") as excinfo:
                _exec_copy(monkeypatch, name)

        assert isinstance(excinfo.value.__cause__, AttributeError)
        assert len(sys.modules[name].app.blueprints) == 1
        assert [r.levelno for r in caplog.records if r.name == name] == [logging.CRITICAL]


class TestMainBlock:
    def _run_as_main(self):
        return runpy.run_path(str(APP_SOURCE), run_name="__main__")

    def test_importing_the_module_does_not_start_the_dev_server(self, monkeypatch, startup_stubs):
        _load_copy(monkeypatch)

        assert startup_stubs["run"] == []

    @pytest.mark.parametrize("port_env, debug_env, expected_port, expected_debug", [
        (None, None, 5001, False),
        ("8123", "1", 8123, True),
        ("9000", " TRUE ", 9000, True),
        ("5002", "0", 5002, False),
        ("5003", "yes", 5003, False),
        ("5004", "", 5004, False),
    ])
    def test_running_as_a_script_starts_the_dev_server_from_env(
        self, monkeypatch, startup_stubs, port_env, debug_env, expected_port, expected_debug,
    ):
        if port_env is None:
            monkeypatch.delenv("PORT", raising=False)
        else:
            monkeypatch.setenv("PORT", port_env)
        if debug_env is None:
            monkeypatch.delenv("FLASK_DEBUG", raising=False)
        else:
            monkeypatch.setenv("FLASK_DEBUG", debug_env)
        monkeypatch.setenv("FLASK_SECRET_KEY", "main-block-secret")

        self._run_as_main()

        assert len(startup_stubs["run"]) == 1
        run_call = startup_stubs["run"][0]
        assert run_call["args"] == ()
        assert run_call["kwargs"] == {"debug": expected_debug, "host": "0.0.0.0", "port": expected_port}
        assert isinstance(run_call["kwargs"]["port"], int)
        # The server is started on the app object this very run built.
        assert isinstance(run_call["app"], flask.Flask)
        assert run_call["app"].secret_key == "main-block-secret"
        assert run_call["app"] is not flask_app_module.app

    def test_running_as_a_script_never_hijacks_the_already_imported_app_module(self, monkeypatch, startup_stubs):
        assert sys.modules["app"] is flask_app_module

        self._run_as_main()

        assert sys.modules["app"] is flask_app_module

    def test_a_non_numeric_port_fails_before_any_server_is_started(self, monkeypatch, startup_stubs):
        monkeypatch.setenv("PORT", "not-a-port")

        with pytest.raises(ValueError):
            self._run_as_main()

        assert startup_stubs["run"] == []
