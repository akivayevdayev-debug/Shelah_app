"""
Direct unit tests for asgi.py's pure helper functions -- test anchors for
the plan.md §32.1 complexity refactor of _flatten_sources_for_ai (and its
extracted helpers _select_source_line_text / _flatten_one_source_for_ai)
and of _run_ask_async_ai_synthesis (and its five extracted helpers,
_dispatch_ask_async_ai_synthesis_call / _validate_ask_async_ai_result /
_extract_ask_async_raw_ai_answer / _resolve_ask_async_web_warning_flag /
_compose_validated_ask_async_answer). Not exercised directly by
tests/test_ask.py's end-to-end /ask coverage, so these pin each function's
own branching (language fallback, skip rules, malformed-input tolerance,
error/timeout handling) in isolation.
"""

import asyncio

import pytest

import asgi
from asgi import (
    _flatten_sources_for_ai,
    _flatten_one_source_for_ai,
    _select_source_line_text,
    _dispatch_ask_async_ai_synthesis_call,
    _validate_ask_async_ai_result,
    _extract_ask_async_raw_ai_answer,
    _resolve_ask_async_web_warning_flag,
    _compose_validated_ask_async_answer,
)
from backend import claude as claude_module
from backend import ask_pipeline as ask_pipeline_module


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


# ── _validate_ask_async_ai_result ───────────────────────────────────────────

def test_validate_ask_async_ai_result_no_error_returns_empty_string():
    assert _validate_ask_async_ai_result({"answer": "ok"}) == ""


def test_validate_ask_async_ai_result_raises_on_real_error():
    with pytest.raises(RuntimeError, match="boom"):
        _validate_ask_async_ai_result({"error": "boom"})


def test_validate_ask_async_ai_result_security_blocked_does_not_raise():
    assert _validate_ask_async_ai_result(
        {"error": "security_blocked_domain"}) == "security_blocked_domain"


# ── _extract_ask_async_raw_ai_answer ────────────────────────────────────────

def test_extract_ask_async_raw_ai_answer_uses_plain_answer_when_not_structured():
    structured, raw = _extract_ask_async_raw_ai_answer(
        {"answer": "  plain text answer  "}, answer_language="en")
    assert structured is None
    assert raw == "plain text answer"


def test_extract_ask_async_raw_ai_answer_renders_structured_payload(monkeypatch):
    monkeypatch.setattr(
        claude_module, "render_structured_markdown",
        lambda structured, answer_language, is_simple: f"rendered:{answer_language}:{is_simple}")
    structured, raw = _extract_ask_async_raw_ai_answer(
        {"structured": {"summary": "x"}, "is_simple": True}, answer_language="he")
    assert structured == {"summary": "x"}
    assert raw == "rendered:he:True"


def test_extract_ask_async_raw_ai_answer_ignores_non_dict_structured():
    structured, raw = _extract_ask_async_raw_ai_answer(
        {"structured": "not a dict", "answer": "fallback text"}, answer_language="en")
    assert structured is None
    assert raw == "fallback text"


def test_extract_ask_async_raw_ai_answer_raises_when_empty():
    with pytest.raises(RuntimeError, match="empty"):
        _extract_ask_async_raw_ai_answer({"answer": "   "}, answer_language="en")


# ── _resolve_ask_async_web_warning_flag ─────────────────────────────────────

def test_resolve_ask_async_web_warning_flag_prefers_used_web_search_key():
    assert _resolve_ask_async_web_warning_flag(
        {"used_web_search": True},
        ctx={"use_tertiary_web_context": False, "wiki_context_for_ai": []}) is True
    assert _resolve_ask_async_web_warning_flag(
        {"used_web_search": False},
        ctx={"use_tertiary_web_context": True, "wiki_context_for_ai": ["x"]}) is False


def test_resolve_ask_async_web_warning_flag_falls_back_to_tertiary_web_context():
    assert _resolve_ask_async_web_warning_flag(
        {}, ctx={"use_tertiary_web_context": True, "wiki_context_for_ai": ["some wiki text"]}) is True
    assert _resolve_ask_async_web_warning_flag(
        {}, ctx={"use_tertiary_web_context": True, "wiki_context_for_ai": []}) is False
    assert _resolve_ask_async_web_warning_flag(
        {}, ctx={"use_tertiary_web_context": False, "wiki_context_for_ai": ["some wiki text"]}) is False


# ── _compose_validated_ask_async_answer ─────────────────────────────────────

def test_compose_validated_ask_async_answer_returns_composed_text():
    result = _compose_validated_ask_async_answer("Some answer.", needs_web_warning=False)
    assert "Some answer." in result


def test_compose_validated_ask_async_answer_raises_when_normalized_empty(monkeypatch):
    monkeypatch.setattr(asgi, "_compose_answer_with_prefixes", lambda raw, include_web_warning: "   ")
    with pytest.raises(RuntimeError, match="normalized to empty"):
        _compose_validated_ask_async_answer("anything", needs_web_warning=False)


# ── _dispatch_ask_async_ai_synthesis_call ───────────────────────────────────

async def test_dispatch_ask_async_ai_synthesis_call_uses_plain_path_by_default(monkeypatch):
    monkeypatch.setattr(claude_module, "AI_AGENTIC_TOOLS", False)
    monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 5)

    captured = {}

    async def _fake_ask_ai_async(**kwargs):
        captured.update(kwargs)
        return {"answer": "ok"}

    monkeypatch.setattr(claude_module, "ask_ai_async", _fake_ask_ai_async)

    ctx = {
        "tool_context": {"route": "/ask"},
        "flat_sources_for_ai": [],
        "customs_info": [],
        "user_memory_summaries": [],
        "wiki_context_for_ai": [],
        "halachipedia_list": [],
    }
    result = await _dispatch_ask_async_ai_synthesis_call(
        "What is Shabbat?", "standard", "sephardic", "en", ctx)

    assert result == {"answer": "ok"}
    assert captured["question"] == "What is Shabbat?"
    assert captured["tool_context"]["async"] is True


async def test_dispatch_ask_async_ai_synthesis_call_uses_agentic_path_when_enabled(monkeypatch):
    monkeypatch.setattr(claude_module, "AI_AGENTIC_TOOLS", True)
    monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 5)

    called = {}

    async def _fake_run_agentic_ask(**kwargs):
        called["invoked"] = True
        return {"answer": "agentic"}

    monkeypatch.setattr(ask_pipeline_module, "run_agentic_ask", _fake_run_agentic_ask)

    ctx = {
        "tool_context": "not-a-dict",
        "flat_sources_for_ai": [],
        "customs_info": [],
        "user_memory_summaries": [],
        "wiki_context_for_ai": [],
        "halachipedia_list": [],
    }
    result = await _dispatch_ask_async_ai_synthesis_call(
        "question", "standard", "ashkenazic", "en", ctx)

    assert result == {"answer": "agentic"}
    assert called.get("invoked") is True


async def test_dispatch_ask_async_ai_synthesis_call_times_out(monkeypatch):
    monkeypatch.setattr(claude_module, "AI_AGENTIC_TOOLS", False)
    monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 0.01)

    async def _slow_ask_ai_async(**kwargs):
        await asyncio.sleep(1)
        return {"answer": "too slow"}

    monkeypatch.setattr(claude_module, "ask_ai_async", _slow_ask_ai_async)

    ctx = {
        "tool_context": {},
        "flat_sources_for_ai": [],
        "customs_info": [],
        "user_memory_summaries": [],
        "wiki_context_for_ai": [],
        "halachipedia_list": [],
    }
    with pytest.raises(asyncio.TimeoutError):
        await _dispatch_ask_async_ai_synthesis_call("q", "standard", "sephardic", "en", ctx)
