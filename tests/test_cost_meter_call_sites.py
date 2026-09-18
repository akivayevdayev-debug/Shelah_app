"""
Confirms backend.cost_meter.record_llm_call is invoked (mocked, no network)
from every model call site in backend/claude.py — plan.md §8.E.1's "cost
metering on every model call" requirement:

  - _call_gemini_model (sync primary) via _call_primary_model
  - _call_gemini_httpx_model (async primary, used by ask_ai_async)
  - _call_anthropic_httpx_model (async fallback, shared by both the sync
    and async pipelines)
  - summarize_with_gemini (sync, semantic-bookmark summaries)

record_llm_call itself is monkeypatched to a recording stub, so these tests
never touch Supabase or the network — SDK objects are faked at the
narrowest boundary (the client returned by _get_async_client() /
_cached_gemini_client), matching the pattern already used in
test_loop_bridge.py of monkeypatching one layer below the function under
test.
"""

from __future__ import annotations

import json

import pytest

import backend.claude as claude_module
import backend.logging_setup as logging_setup


@pytest.fixture(autouse=True)
def _reset_request_id():
    logging_setup._request_id_var.set("")
    yield
    logging_setup._request_id_var.set("")


@pytest.fixture(autouse=True)
def _warm_genai_sdk():
    """Force the real _ensure_genai_loaded() side effect before each test.

    _configure_gemini_client() is what normally triggers this (populating
    the module-level genai_types global), but several tests below
    monkeypatch _configure_gemini_client itself with a no-op lambda to
    fake a pre-configured client. Run in isolation, that bypasses the
    loader entirely and genai_types stays None, which
    _call_gemini_httpx_model treats as a hard "gemini_sdk_missing" error
    before ever reaching record_llm_call — passing only when an earlier
    test in a full-suite run happened to warm the module-level cache
    first (plan.md §38.2). Calling the loader directly here removes that
    ordering dependency; it's idempotent (short-circuits on
    _genai_loaded), so it's harmless for tests that don't need it.
    """
    claude_module._ensure_genai_loaded()


@pytest.fixture
def captured_cost_calls(monkeypatch):
    calls: list[dict] = []

    async def _fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(claude_module, "record_llm_call", _fake_record)
    return calls


# ─── Sync Gemini primary call site (_call_gemini_model -> _call_primary_model) ─

class TestSyncGeminiPrimaryCallSite:
    async def test_success_records_gemini_cost(self, captured_cost_calls, monkeypatch):
        logging_setup.bind_request_id("req-sync-gemini-primary")

        def _fake_gemini_model(prompt, dynamic_system_context="", max_tokens=3072):
            return {
                "answer": "ok",
                "structured": {"ruling": "ok"},
                "confidence": 0.75,
                "is_fallback": False,
                "model": "gemini-test-model",
                "_usage_tokens": {"input": 123, "output": 45},
            }
        monkeypatch.setattr(claude_module, "_call_gemini_model", _fake_gemini_model)

        result = await claude_module._call_primary_model("prompt text")

        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "gemini"
        assert call["model"] == "gemini-test-model"
        assert call["input_tokens"] == 123
        assert call["output_tokens"] == 45
        assert call["route"] == "/ask"
        assert call["request_id"] == "req-sync-gemini-primary"
        # Internal-only bookkeeping key must never leak to callers/API responses.
        assert "_usage_tokens" not in result

    async def test_success_extracts_real_usage_metadata_attributes(
        self, captured_cost_calls, monkeypatch
    ):
        """Exercises the REAL _call_gemini_model (not monkeypatched away),
        faking only the SDK client one layer down — pins the actual
        getattr(usage, "prompt_token_count"/"candidates_token_count", 0)
        attribute names used to build _usage_tokens, which the
        monkeypatched-_call_gemini_model tests above cannot catch."""
        response = _FakeSyncGeminiResponse(
            json.dumps({"ruling": "ok"}), prompt_tokens=77, candidate_tokens=33)
        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude_module, "_cached_gemini_client", _FakeSyncGeminiClient(response))

        await claude_module._call_primary_model("prompt text")

        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "gemini"
        assert call["input_tokens"] == 77
        assert call["output_tokens"] == 33

    async def test_empty_response_still_records_billed_tokens(
        self, captured_cost_calls, monkeypatch
    ):
        """A Gemini call that succeeds but extracts to empty text still
        consumed tokens (resp exists) — must still be recorded, and must
        still fall through to the Claude fallback."""
        empty_response = _FakeSyncGeminiResponse("", prompt_tokens=50, candidate_tokens=0)
        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude_module, "_cached_gemini_client", _FakeSyncGeminiClient(empty_response))

        fallback_called = {}

        async def _fake_claude_model(prompt, dynamic_system_context="", gemini_error=""):
            fallback_called["gemini_error"] = gemini_error
            return {"answer": "fallback", "confidence": 0.5, "is_fallback": True}
        monkeypatch.setattr(claude_module, "_call_claude_model", _fake_claude_model)

        result = await claude_module._call_primary_model("prompt text")

        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "gemini"
        assert call["input_tokens"] == 50
        assert call["output_tokens"] == 0
        assert "empty_response" in fallback_called["gemini_error"]
        assert result["answer"] == "fallback"

    async def test_error_path_records_no_cost_and_falls_back_to_claude(
        self, captured_cost_calls, monkeypatch
    ):
        def _fake_gemini_model(prompt, dynamic_system_context="", max_tokens=3072):
            return {
                "answer": "unavailable",
                "confidence": 0,
                "error": "gemini_error: boom",
                "is_fallback": False,
                "model": "gemini-test-model",
            }
        monkeypatch.setattr(claude_module, "_call_gemini_model", _fake_gemini_model)

        fallback_called = {}

        async def _fake_claude_model(prompt, dynamic_system_context="", gemini_error=""):
            fallback_called["gemini_error"] = gemini_error
            return {"answer": "fallback", "confidence": 0.5, "is_fallback": True}
        monkeypatch.setattr(claude_module, "_call_claude_model", _fake_claude_model)

        result = await claude_module._call_primary_model("prompt text")

        # No Gemini row (no tokens were billed on a hard-error path) — the
        # Claude fallback call site is responsible for its own cost row.
        assert captured_cost_calls == []
        assert fallback_called["gemini_error"] == "gemini_error: boom"
        assert result["answer"] == "fallback"


# ─── Async Anthropic fallback call site (_call_anthropic_httpx_model) ──────────

class _FakeUsage:
    def __init__(self, input_tokens, output_tokens):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeContentBlock:
    def __init__(self, text):
        self.text = text


class _FakeAnthropicMessage:
    def __init__(self, text, input_tokens, output_tokens):
        self.content = [_FakeContentBlock(text)]
        self.usage = _FakeUsage(input_tokens, output_tokens)


class _FakeAnthropicMessages:
    def __init__(self, message):
        self._message = message

    async def create(self, **kwargs):
        return self._message


class _FakeAsyncAnthropicClient:
    def __init__(self, message):
        self.messages = _FakeAnthropicMessages(message)


class TestAnthropicFallbackCallSite:
    async def test_success_records_anthropic_cost(self, captured_cost_calls, monkeypatch):
        logging_setup.bind_request_id("req-anthropic-fallback")
        message = _FakeAnthropicMessage(
            json.dumps({"ruling": "ok"}), input_tokens=200, output_tokens=80)
        monkeypatch.setattr(
            claude_module, "_get_async_client", lambda: _FakeAsyncAnthropicClient(message))

        await claude_module._call_anthropic_httpx_model("prompt text")

        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "anthropic"
        assert call["input_tokens"] == 200
        assert call["output_tokens"] == 80
        assert call["route"] == "/ask"
        assert call["request_id"] == "req-anthropic-fallback"

    async def test_missing_client_records_no_cost(self, captured_cost_calls, monkeypatch):
        monkeypatch.setattr(claude_module, "_get_async_client", lambda: None)

        result = await claude_module._call_anthropic_httpx_model("prompt text")

        assert captured_cost_calls == []
        assert result.get("error")


# ─── Async Gemini primary call site (_call_gemini_httpx_model) ────────────────

class _FakeGeminiUsage:
    def __init__(self, prompt_tokens, candidate_tokens):
        self.prompt_token_count = prompt_tokens
        self.candidates_token_count = candidate_tokens


class _FakeGeminiResponse:
    def __init__(self, text, prompt_tokens, candidate_tokens):
        self.text = text
        self.usage_metadata = _FakeGeminiUsage(prompt_tokens, candidate_tokens)


class _FakeAioModels:
    def __init__(self, response):
        self._response = response

    async def generate_content(self, **kwargs):
        return self._response


class _FakeAsyncGeminiClient:
    def __init__(self, response):
        class _Aio:
            def __init__(self, models):
                self.models = models
        self.aio = _Aio(_FakeAioModels(response))


class TestAsyncGeminiPrimaryCallSite:
    async def test_success_records_gemini_cost(self, captured_cost_calls, monkeypatch):
        logging_setup.bind_request_id("req-async-gemini-primary")
        response = _FakeGeminiResponse(
            json.dumps({"ruling": "ok"}), prompt_tokens=300, candidate_tokens=120)
        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude_module, "_cached_gemini_client", _FakeAsyncGeminiClient(response))

        await claude_module._call_gemini_httpx_model("prompt text")

        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "gemini"
        assert call["input_tokens"] == 300
        assert call["output_tokens"] == 120
        assert call["route"] == "/ask"
        assert call["request_id"] == "req-async-gemini-primary"

    async def test_config_error_records_no_cost(self, captured_cost_calls, monkeypatch):
        monkeypatch.setattr(
            claude_module, "_configure_gemini_client", lambda: "gemini_api_key_missing")

        result = await claude_module._call_gemini_httpx_model("prompt text")

        assert captured_cost_calls == []
        assert result.get("error") == "gemini_api_key_missing"


# ─── summarize_with_gemini (sync, semantic-bookmark summaries) ────────────────

class _FakeSyncGeminiResponse:
    def __init__(self, text, prompt_tokens, candidate_tokens):
        self.text = text
        self.usage_metadata = _FakeGeminiUsage(prompt_tokens, candidate_tokens)


class _FakeSyncGeminiModels:
    def __init__(self, response):
        self._response = response

    def generate_content(self, **kwargs):
        return self._response


class _FakeSyncGeminiClient:
    def __init__(self, response):
        self.models = _FakeSyncGeminiModels(response)


class TestSummarizeWithGeminiCallSite:
    def test_success_records_gemini_cost(self, captured_cost_calls, monkeypatch):
        logging_setup.bind_request_id("req-summarize-with-gemini")
        response = _FakeSyncGeminiResponse(
            "A concise chevruta summary.", prompt_tokens=80, candidate_tokens=40)
        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude_module, "_cached_gemini_client", _FakeSyncGeminiClient(response))

        result = claude_module.summarize_with_gemini("segment text", notes="my notes")

        assert result["summary"] == "A concise chevruta summary."
        assert len(captured_cost_calls) == 1
        call = captured_cost_calls[0]
        assert call["provider"] == "gemini"
        assert call["input_tokens"] == 80
        assert call["output_tokens"] == 40
        assert call["route"] == "/api/bookmarks/semantic"
        assert call["request_id"] == "req-summarize-with-gemini"

    def test_cost_tracking_failure_does_not_break_the_summary(self, monkeypatch):
        response = _FakeSyncGeminiResponse(
            "A concise chevruta summary.", prompt_tokens=80, candidate_tokens=40)
        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(
            claude_module, "_cached_gemini_client", _FakeSyncGeminiClient(response))

        def _boom(**kwargs):
            raise RuntimeError("cost tracking exploded")
        monkeypatch.setattr(claude_module, "record_llm_call", _boom)

        result = claude_module.summarize_with_gemini("segment text")

        assert result["summary"] == "A concise chevruta summary."
        assert result["error"] == ""

    def test_unconfigured_client_records_no_cost(self, captured_cost_calls, monkeypatch):
        monkeypatch.setattr(
            claude_module, "_configure_gemini_client", lambda: "gemini_api_key_missing")

        result = claude_module.summarize_with_gemini("segment text")

        assert captured_cost_calls == []
        assert result["error"] == "gemini_api_key_missing"
