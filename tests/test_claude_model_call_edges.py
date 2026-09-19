"""
Edge-path tests for backend/claude.py: the pure formatting/parsing helpers and
the failure branches of the model call sites (Gemini configuration, the async
Gemini primary, the async Anthropic fallback, the bookmark summariser).

SDK clients are faked at the narrowest boundary (the client object the call
site uses), as in tests/test_cost_meter_call_sites.py; record_llm_call is
stubbed so nothing touches Supabase or the network.
"""

from __future__ import annotations

import json
import types

import pytest

import backend.claude as claude
from backend.health_check import health


@pytest.fixture(autouse=True)
def _warm_genai_sdk():
    claude._ensure_genai_loaded()


@pytest.fixture(autouse=True)
def _no_cost_writes(monkeypatch):
    async def _record(**kwargs):
        return None

    monkeypatch.setattr(claude, "record_llm_call", _record)


# ─── _int_env ───────────────────────────────────────────────────────────────

class TestIntEnv:
    def test_reads_an_integer(self, monkeypatch):
        monkeypatch.setenv("SHELAH_TEST_INT", "42")

        assert claude._int_env("SHELAH_TEST_INT", 7) == 42

    def test_missing_variable_uses_the_default(self, monkeypatch):
        monkeypatch.delenv("SHELAH_TEST_INT", raising=False)

        assert claude._int_env("SHELAH_TEST_INT", 7) == 7

    @pytest.mark.parametrize("raw", ["abc", "1.5", ""])
    def test_a_non_integer_value_uses_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("SHELAH_TEST_INT", raw)

        assert claude._int_env("SHELAH_TEST_INT", 7) == 7


# ─── Context formatting ─────────────────────────────────────────────────────

class TestFormatCustomsTruncation:
    def test_a_long_ruling_is_cut_at_the_limit_with_an_ellipsis(self):
        output = claude.format_customs([{"community": "Sephardi", "ruling": "word " * 100}], max_chars=20)

        assert output == "\n[Sephardi] word word word word...\n"

    def test_a_ruling_at_the_limit_is_untouched(self):
        output = claude.format_customs([{"ruling": "x" * 20}], max_chars=20)

        assert output == "\n[Community] " + "x" * 20 + "\n"


class TestFormatUserMemories:
    def test_blank_summaries_are_skipped_and_long_ones_truncated(self):
        rows = [{"summary": "   "}, {"summary": "abc def ghi jkl"}, {"summary": None}]

        assert claude.format_user_memories(rows, max_items=5, max_chars=9) == "- abc def g..."

    def test_only_the_first_max_items_rows_are_considered(self):
        rows = [{"summary": ""}, {"summary": "kept"}, {"summary": "beyond the limit"}]

        assert claude.format_user_memories(rows, max_items=2) == "- kept"

    def test_no_rows_give_an_empty_string(self):
        assert claude.format_user_memories(None) == ""


class TestFormatOneContextItem:
    def test_title_and_summary(self):
        assert claude._format_one_context_item(
            {"title": "Shabbat", "summary": "A day of rest"}, "Web", set(),
        ) == "[Web] Shabbat: A day of rest"

    def test_title_only(self):
        assert claude._format_one_context_item({"title": "Shabbat"}, "Web", set()) == "[Web] Shabbat"

    def test_summary_only(self):
        assert claude._format_one_context_item({"summary": "A day of rest"}, "Web", set()) == "[Web] A day of rest"

    def test_the_items_own_provider_overrides_the_default_label(self):
        item = {"title": "T", "summary": "S", "source_provider": "Halachipedia"}

        assert claude._format_one_context_item(item, "Web", set()) == "[Halachipedia] T: S"

    def test_a_blank_provider_falls_back_to_the_default_label(self):
        item = {"title": "T", "source_provider": "   "}

        assert claude._format_one_context_item(item, "Web", set()) == "[Web] T"

    def test_summaries_are_capped_at_a_thousand_characters(self):
        line = claude._format_one_context_item({"title": "T", "summary": "s" * 1500}, "Web", set())

        assert line == "[Web] T: " + "s" * 1000

    @pytest.mark.parametrize("item", ["text", None, 5, {}, {"title": " ", "summary": ""}])
    def test_unusable_items_are_skipped(self, item):
        assert claude._format_one_context_item(item, "Web", set()) is None

    def test_a_repeat_is_skipped_case_insensitively(self):
        seen: set = set()

        assert claude._format_one_context_item({"title": "Shabbat", "summary": "Rest"}, "Web", seen)
        assert claude._format_one_context_item({"title": "SHABBAT", "summary": "REST"}, "Web", seen) is None


# ─── JSON extraction / markdown / bookmark fallback ─────────────────────────

class TestExtractFirstJsonObjectScanning:
    def test_skips_a_brace_pair_that_is_not_json_and_finds_the_next_object(self):
        assert claude._extract_first_json_object('note {not json} then {"ruling": "ok"}') == {"ruling": "ok"}

    def test_no_valid_object_anywhere_gives_none(self):
        assert claude._extract_first_json_object("{oops} and {also: bad}") is None


class TestRenderFullMarkdownProhibitedStatus:
    HEADERS = dict(
        direct_header="## Answer", status_label="**Status: prohibited**", deeper_header="## Deeper",
        steps_label="Steps", summary_header="## Summary", sources_label="Sources",
    )

    def test_a_prohibited_ruling_gets_the_status_label(self):
        lines = claude._render_full_markdown_lines(
            "Not permitted.", {"is_prohibited": True}, [], "", [], **self.HEADERS)

        assert lines[:4] == ["## Answer", "", "Not permitted.", ""]
        assert "**Status: prohibited**" in lines

    def test_a_permitted_ruling_has_no_status_label(self):
        lines = claude._render_full_markdown_lines(
            "Permitted.", {"is_prohibited": False}, [], "", [], **self.HEADERS)

        assert "**Status: prohibited**" not in lines


class TestFallbackBookmarkSummary:
    def test_segment_and_notes_are_combined(self):
        assert claude._fallback_bookmark_summary("The segment.", "my note") == (
            "The segment. Practical takeaway: my note."
        )

    def test_segment_only_points_to_a_teacher(self):
        assert claude._fallback_bookmark_summary("The segment.", "") == (
            "The segment. Practical takeaway: review this section alongside a trusted posek or teacher."
        )

    def test_notes_only_asks_to_verify_against_sources(self):
        assert claude._fallback_bookmark_summary("", "my note") == (
            "my note Practical takeaway: verify this note against primary sources before relying on it."
        )

    def test_nothing_gives_an_empty_summary(self):
        assert claude._fallback_bookmark_summary(None, "  ") == ""

    def test_a_long_segment_is_cut_at_exactly_520_characters(self):
        summary = claude._fallback_bookmark_summary("s" * 600, "")

        assert summary.startswith("s" * 520 + "... Practical takeaway:")

    def test_long_notes_are_cut_at_exactly_220_characters(self):
        summary = claude._fallback_bookmark_summary("The segment.", "n" * 300)

        assert summary == "The segment. Practical takeaway: " + "n" * 220 + "...."

    def test_whitespace_runs_are_collapsed(self):
        assert claude._fallback_bookmark_summary("a \n\t b", " c    d ") == "a b Practical takeaway: c d."


# ─── _configure_gemini_client failure branches ──────────────────────────────

class TestConfigureGeminiClientFailures:
    @pytest.fixture(autouse=True)
    def _fresh_client_cache(self, monkeypatch):
        monkeypatch.setattr(claude, "_cached_gemini_client", "stale-client")
        monkeypatch.setattr(claude, "_cached_gemini_api_key", "stale-key")

    def test_a_missing_sdk_clears_the_cached_client(self, monkeypatch):
        monkeypatch.setattr(claude, "_ensure_genai_loaded", lambda: None)
        monkeypatch.setattr(claude, "genai", None)

        assert claude._configure_gemini_client() == "gemini_sdk_missing"
        assert claude._cached_gemini_client is None
        assert claude._cached_gemini_api_key is None

    def test_a_client_constructor_failure_is_reported_and_clears_the_cache(self, monkeypatch):
        def broken_client(**kwargs):
            raise ValueError("bad options")

        monkeypatch.setenv("GEMINI_API_KEY", "a-new-key")
        monkeypatch.setattr(claude, "genai", types.SimpleNamespace(Client=broken_client))

        assert claude._configure_gemini_client() == "gemini_config_error: bad options"
        assert claude._cached_gemini_client is None
        assert claude._cached_gemini_api_key is None


class TestSyncGeminiModelConfigError:
    def test_a_configuration_error_is_returned_as_an_unavailable_result(self, monkeypatch):
        monkeypatch.setattr(claude, "_configure_gemini_client", lambda: "gemini_api_key_missing")

        result = claude._call_gemini_model("prompt")

        assert result["error"] == "gemini_api_key_missing"
        assert result["answer"] == claude._ERR_AI_PROVIDER_UNAVAILABLE
        assert result["is_fallback"] is False

    def test_a_missing_client_after_a_clean_configure_is_reported(self, monkeypatch):
        monkeypatch.setattr(claude, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(claude, "_cached_gemini_client", None)

        result = claude._call_gemini_model("prompt")

        assert result["error"] == "gemini_client_missing"
        assert result["answer"] == claude._ERR_AI_PROVIDER_UNAVAILABLE
        assert result["is_fallback"] is False


# ─── Async Gemini primary: failure branches ─────────────────────────────────

class _FakeGeminiModels:
    def __init__(self, *, text=None, error=None):
        self._text, self._error = text, error
        self.calls: list[dict] = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(text=self._text, usage_metadata=None)


def _gemini_client(models):
    return types.SimpleNamespace(aio=types.SimpleNamespace(models=models))


class TestAsyncGeminiPrimary:
    @pytest.fixture(autouse=True)
    def _configured(self, monkeypatch):
        monkeypatch.setattr(claude, "_configure_gemini_client", lambda: None)

    async def test_a_blank_model_env_falls_back_to_the_default_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "   ")
        monkeypatch.setattr(claude, "_cached_gemini_client", None)

        result = await claude._call_gemini_httpx_model("prompt")

        assert result["model"] == claude._DEFAULT_GEMINI_MODEL
        assert result["error"] == "gemini_client_missing"

    async def test_a_configuration_error_is_surfaced(self, monkeypatch):
        monkeypatch.setattr(claude, "_configure_gemini_client", lambda: "gemini_api_key_missing")

        result = await claude._call_gemini_httpx_model("prompt")

        assert result["error"] == "gemini_api_key_missing"

    async def test_a_missing_types_module_is_reported_as_a_missing_sdk(self, monkeypatch):
        monkeypatch.setattr(claude, "_cached_gemini_client", _gemini_client(_FakeGeminiModels(text="{}")))
        monkeypatch.setattr(claude, "genai_types", None)

        result = await claude._call_gemini_httpx_model("prompt")

        assert result["error"] == "gemini_sdk_missing"
        assert result["is_fallback"] is False

    async def test_an_empty_response_is_a_recorded_failure(self, monkeypatch):
        monkeypatch.setattr(claude, "_cached_gemini_client", _gemini_client(_FakeGeminiModels(text="  ")))

        result = await claude._call_gemini_httpx_model("prompt")

        assert result["error"] == "gemini_sdk_error: empty_response"
        assert result["answer"] == claude._ERR_AI_PROVIDER_UNAVAILABLE
        assert health._circuits["gemini"].failures == 1

    async def test_an_sdk_exception_is_a_recorded_failure(self, monkeypatch):
        models = _FakeGeminiModels(error=RuntimeError("quota exceeded"))
        monkeypatch.setattr(claude, "_cached_gemini_client", _gemini_client(models))

        result = await claude._call_gemini_httpx_model("prompt")

        assert result["error"] == "gemini_sdk_error: quota exceeded"
        assert health._circuits["gemini"].failures == 1

    async def test_dynamic_context_and_the_simple_flag_shape_the_request(self, monkeypatch):
        models = _FakeGeminiModels(text=json.dumps({"ruling": "ok"}))
        monkeypatch.setattr(claude, "_cached_gemini_client", _gemini_client(models))

        result = await claude._call_gemini_httpx_model("prompt", "EXTRA CONTEXT", is_simple=True)

        [call] = models.calls
        assert call["contents"] == "prompt"
        assert call["config"].system_instruction == f"{claude.SIMPLE_SYSTEM_PROMPT}\n\nEXTRA CONTEXT".strip()
        assert call["config"].max_output_tokens == 512
        assert result["is_fallback"] is False
        assert health._circuits["gemini"].failures == 0


# ─── Async Anthropic fallback: failure branches ─────────────────────────────

class _FakeAnthropicMessages:
    def __init__(self, *, texts=("{}",), error=None):
        self._texts, self._error = texts, error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text=text) for text in self._texts], usage=None,
        )


def _anthropic_client(messages):
    return types.SimpleNamespace(messages=messages)


class TestAsyncAnthropicFallback:
    async def test_dynamic_context_is_appended_to_the_system_prompt(self, monkeypatch):
        messages = _FakeAnthropicMessages(texts=[json.dumps({"ruling": "ok"})])
        monkeypatch.setattr(claude, "_get_async_client", lambda: _anthropic_client(messages))

        result = await claude._call_anthropic_httpx_model("prompt", "EXTRA CONTEXT")

        [call] = messages.calls
        assert call["system"][0]["text"] == f"{claude.CORE_SYSTEM_PROMPT}\n\nEXTRA CONTEXT"
        assert call["messages"] == [{"role": "user", "content": "prompt"}]
        assert result["is_fallback"] is True
        assert health._circuits["claude"].failures == 0

    async def test_without_dynamic_context_the_core_prompt_is_used_as_is(self, monkeypatch):
        messages = _FakeAnthropicMessages(texts=[json.dumps({"ruling": "ok"})])
        monkeypatch.setattr(claude, "_get_async_client", lambda: _anthropic_client(messages))

        await claude._call_anthropic_httpx_model("prompt")

        assert messages.calls[0]["system"][0]["text"] == claude.CORE_SYSTEM_PROMPT

    async def test_blank_text_blocks_are_an_empty_response_and_a_recorded_failure(self, monkeypatch):
        messages = _FakeAnthropicMessages(texts=["", "   "])
        monkeypatch.setattr(claude, "_get_async_client", lambda: _anthropic_client(messages))

        result = await claude._call_anthropic_httpx_model("prompt")

        assert result["error"] == "anthropic_sdk_error: empty_response"
        assert result["answer"] == claude._ERR_AI_PROVIDER_UNAVAILABLE
        assert health._circuits["claude"].failures == 1

    async def test_an_sdk_exception_keeps_the_gemini_error_for_diagnosis(self, monkeypatch):
        messages = _FakeAnthropicMessages(error=RuntimeError("overloaded"))
        monkeypatch.setattr(claude, "_get_async_client", lambda: _anthropic_client(messages))

        result = await claude._call_anthropic_httpx_model("prompt", gemini_error="gemini_circuit_open")

        assert result["error"] == "gemini_error: gemini_circuit_open; anthropic_sdk_error: overloaded"
        assert health._circuits["claude"].failures == 1


# ─── summarize_with_gemini: empty and failing responses ─────────────────────

class TestSummarizeWithGeminiFallbacks:
    @staticmethod
    def _install_client(monkeypatch, *, text=None, error=None):
        calls: list[dict] = []

        class _Models:
            def generate_content(self, **kwargs):
                calls.append(kwargs)
                if error is not None:
                    raise error
                return types.SimpleNamespace(text=text, usage_metadata=None, candidates=[])

        monkeypatch.setattr(claude, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude, "_cached_gemini_client", types.SimpleNamespace(models=_Models()),
        )
        monkeypatch.setattr(claude, "_extract_gemini_response_text", lambda response: response.text or "")
        return calls

    def test_an_empty_summary_falls_back_to_the_deterministic_one(self, monkeypatch):
        self._install_client(monkeypatch, text="   ")

        result = claude.summarize_with_gemini("The segment.", "a note")

        assert result == {
            "summary": "The segment. Practical takeaway: a note.",
            "error": "gemini_empty_summary",
        }

    def test_an_sdk_exception_falls_back_to_the_deterministic_summary(self, monkeypatch):
        self._install_client(monkeypatch, error=RuntimeError("quota exceeded"))

        result = claude.summarize_with_gemini("The segment.", "")

        assert result["summary"].startswith("The segment. Practical takeaway:")
        assert "quota exceeded" in result["error"]

    def test_a_blank_model_env_falls_back_to_the_default_model(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", " ")
        calls = self._install_client(monkeypatch, text="A useful summary.")

        assert claude.summarize_with_gemini("The segment.", "") == {
            "summary": "A useful summary.", "error": "",
        }
        assert calls[0]["model"] == claude._DEFAULT_GEMINI_MODEL

    def test_a_configured_model_env_is_used(self, monkeypatch):
        monkeypatch.setenv("GEMINI_MODEL", "  custom-model ")
        calls = self._install_client(monkeypatch, text="A useful summary.")

        claude.summarize_with_gemini("The segment.", "")

        assert calls[0]["model"] == "custom-model"
