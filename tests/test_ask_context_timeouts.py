"""
The /ask retrieval fan-out must not be held hostage by its slowest secondary
source: asyncio.gather waits for every member, and the httpx calls behind
Halachipedia / Wikipedia default to 10s each. Each stage now has its own
ceiling (asgi._ASK_CONTEXT_TIMEOUT_SECONDS) and a stage that misses it is
dropped while the answer proceeds. Also covers the sync route's ordering: the
secondary lookups must be submitted before the primary Sefaria sources are
resolved, so the two stages overlap.
"""

import asyncio
import time
from unittest import mock

import pytest

import app as flask_app_module
import asgi
from backend import ask_progress


async def _sleeps(seconds, value):
    await asyncio.sleep(seconds)
    return value


class TestWithinCeiling:
    async def test_returns_the_stage_result_when_it_finishes_in_time(self):
        assert await asgi._within("wiki", _sleeps(0, "payload"), None) == "payload"

    async def test_returns_the_default_when_the_stage_runs_out_of_time(self, monkeypatch, caplog):
        monkeypatch.setitem(asgi._ASK_CONTEXT_TIMEOUT_SECONDS, "wiki", 0.05)
        with caplog.at_level("WARNING"):
            result = await asgi._within("wiki", _sleeps(5, "late"), "fallback")
        assert result == "fallback"
        assert "exceeded" in caplog.text

    async def test_other_failures_still_propagate(self):
        async def boom():
            raise RuntimeError("upstream exploded")

        with pytest.raises(RuntimeError, match="upstream exploded"):
            await asgi._within("wiki", boom(), None)


class TestCollectAsyncContextDegrades:
    @pytest.fixture
    def stubbed(self, monkeypatch):
        async def primary(question):
            return ["Berakhot 1:1"], [{"ref": "Berakhot 1:1", "lines": [{"en": "Text", "he": "טקסט"}]}]

        async def tool_context():
            return {"route": "/ask", "async": True, "hebrew_date": "x"}

        monkeypatch.setattr(asgi, "_collect_primary_sources", primary)
        monkeypatch.setattr(asgi, "_build_tool_context", tool_context)
        monkeypatch.setattr(asgi, "_retrieve_community_knowledge", lambda *a, **k: [])
        monkeypatch.setattr(asgi, "_fetch_user_memory_summaries", lambda *a, **k: [])

        async def halachipedia(question):
            return {"title": "[Halachipedia] Shabbat", "summary": "s"}

        async def wiki(question):
            return {"title": "Shabbat", "summary": "w"}

        monkeypatch.setattr(asgi.search, "async_search_halachipedia", halachipedia)
        monkeypatch.setattr(asgi.search, "async_search_wikipedia", wiki)

    async def test_a_hung_halachipedia_does_not_hold_the_answer(self, stubbed, monkeypatch):
        async def hung(question):
            await asyncio.sleep(30)

        monkeypatch.setattr(asgi.search, "async_search_halachipedia", hung)
        monkeypatch.setitem(asgi._ASK_CONTEXT_TIMEOUT_SECONDS, "halachipedia", 0.05)

        started = time.monotonic()
        ctx = await asgi._collect_ask_async_context("q", "All", None, "en")

        assert time.monotonic() - started < 2
        assert ctx["halachipedia_list"] == []
        assert ctx["has_primary_sources"] is True
        assert ctx["tool_context"]["hebrew_date"] == "x"

    async def test_a_hung_primary_stage_yields_no_sources_instead_of_blocking(self, stubbed, monkeypatch):
        async def hung(question):
            await asyncio.sleep(30)

        monkeypatch.setattr(asgi, "_collect_primary_sources", hung)
        monkeypatch.setitem(asgi._ASK_CONTEXT_TIMEOUT_SECONDS, "primary", 0.05)

        ctx = await asgi._collect_ask_async_context("q", "All", None, "en")

        assert ctx["primary_sources"] == []
        assert ctx["has_primary_sources"] is False

    async def test_hung_tool_context_falls_back_to_the_default(self, stubbed, monkeypatch):
        async def hung():
            await asyncio.sleep(30)

        monkeypatch.setattr(asgi, "_build_tool_context", hung)
        monkeypatch.setitem(asgi._ASK_CONTEXT_TIMEOUT_SECONDS, "tool_context", 0.05)

        ctx = await asgi._collect_ask_async_context("q", "All", None, "en")

        assert ctx["tool_context"] == {"route": "/ask", "async": True}

    async def test_all_stages_healthy_keeps_every_result(self, stubbed):
        ctx = await asgi._collect_ask_async_context("q", "All", None, "en")

        assert [s["ref"] for s in ctx["primary_sources"]] == ["Berakhot 1:1"]
        assert ctx["halachipedia_list"][0]["title"] == "[Halachipedia] Shabbat"
        assert ctx["wiki_list"][0]["title"] == "Shabbat"

    def test_every_stage_has_a_ceiling(self):
        assert set(asgi._ASK_CONTEXT_TIMEOUT_SECONDS) == {
            "primary", "halachipedia", "wiki", "knowledge", "memory", "tool_context"}
        assert all(v > 0 for v in asgi._ASK_CONTEXT_TIMEOUT_SECONDS.values())


class TestSyncContextOverlapsStages:
    def test_secondary_lookups_are_submitted_before_the_primary_sources(self, monkeypatch):
        order = []

        def submit(pool, fn, *args, **kwargs):
            order.append(f"submit:{getattr(fn, '__name__', 'fn')}")
            fut = mock.Mock()
            fut.result.return_value = None
            return fut

        def primary(question, engine, context=()):
            order.append("primary")
            return []

        monkeypatch.setattr(flask_app_module, "submit_with_context", submit)
        monkeypatch.setattr(flask_app_module, "_collect_primary_sources_sync", primary)
        engine = mock.Mock(get_halachipedia_summary=lambda q: None, get_wiki=lambda q: None)

        flask_app_module._collect_ask_question_context("q", "All", None, "en", engine)

        assert order.index("primary") == len(order) - 1
        assert len([o for o in order if o.startswith("submit:")]) == 4


class TestProgressTracked:
    """The sync route reports each secondary lookup as a live-progress step
    without changing what it submits to the pool."""

    def test_keeps_the_wrapped_lookups_name(self):
        def get_wiki(question):
            return question

        assert flask_app_module._progress_tracked("commentary", get_wiki).__name__ == "get_wiki"

    def test_reports_the_step_around_the_call_and_passes_arguments_through(self):
        events = []
        token = ask_progress.bind(events.append)
        try:
            out = flask_app_module._progress_tracked("commentary", lambda q, n=1: (q, n))("x", n=2)
        finally:
            ask_progress.unbind(token)

        assert out == ("x", 2)
        assert [(e["stage"], e["state"]) for e in events] == [("commentary", "start"), ("commentary", "done")]

    def test_ends_the_step_when_the_lookup_raises(self):
        def boom():
            raise RuntimeError("upstream down")

        events = []
        token = ask_progress.bind(events.append)
        try:
            with pytest.raises(RuntimeError, match="upstream down"):
                flask_app_module._progress_tracked("customs", boom)()
        finally:
            ask_progress.unbind(token)

        assert [e["state"] for e in events] == ["start", "done"]

    def test_is_silent_when_no_request_is_listening(self):
        assert flask_app_module._progress_tracked("customs", lambda: "ok")() == "ok"
