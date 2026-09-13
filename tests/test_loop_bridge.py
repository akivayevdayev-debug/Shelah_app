"""
Tests for backend/claude.py's sync/async loop-bridge (plan.md §5.1).

_call_primary_model_sync() is documented as only safe to call from a thread
with no running event loop (Flask WSGI workers). These tests pin that fast
path, and verify the escape-hatch bridge for a caller that violates the
assumption (invoked from a thread that owns a running loop).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import warnings

import pytest

import backend.claude as claude


def _patch_primary_model(monkeypatch, result=None, delay=0.0):
    """Replace claude._call_primary_model with a fast, deterministic stub so
    these tests exercise only the sync/async bridging logic, not the Gemini/
    Anthropic call chain (already covered elsewhere)."""
    result = result if result is not None else {
        "answer": "ok", "confidence": 0.9}

    async def _fake_primary_model(prompt, dynamic_system_context="", max_tokens=3072):
        if delay:
            await asyncio.sleep(delay)
        return result

    monkeypatch.setattr(claude, "_call_primary_model", _fake_primary_model)
    return result


async def _call_sync_wrapper_from_within_loop(prompt="question"):
    """Invoke the sync wrapper directly from inside a running coroutine —
    i.e. from the thread driving this coroutine's event loop, rather than via
    asyncio.to_thread(). This simulates a caller violating the sync-context
    assumption and should route through the loop-bridge escape hatch."""
    return claude._call_primary_model_sync(prompt)


class TestFastPath:
    def test_no_running_loop_takes_fast_path_with_identical_result(self, monkeypatch, caplog):
        expected = _patch_primary_model(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="backend.claude"):
            result = claude._call_primary_model_sync("question")

        assert result == expected
        assert "claude-loop-bridge" not in caplog.text


class TestBridgePath:
    def test_call_from_running_loop_uses_bridge_and_logs_warning(self, monkeypatch, caplog):
        expected = _patch_primary_model(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="backend.claude"):
            result = asyncio.run(_call_sync_wrapper_from_within_loop())

        assert result == expected
        assert "claude-loop-bridge" in caplog.text

    def test_repeated_bridge_calls_reuse_executor_without_thread_growth(self, monkeypatch):
        _patch_primary_model(monkeypatch)

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            for _ in range(5):
                asyncio.run(_call_sync_wrapper_from_within_loop())

        bridge_threads = [
            t for t in threading.enumerate()
            if t.name.startswith("claude-loop-bridge")
        ]
        assert 0 < len(bridge_threads) <= 2

    def test_timeout_propagates_cleanly(self, monkeypatch):
        _patch_primary_model(monkeypatch, delay=0.3)
        monkeypatch.setattr(claude, "AI_TOTAL_BUDGET_SECONDS", 0.05)

        with pytest.raises(TimeoutError):
            asyncio.run(_call_sync_wrapper_from_within_loop())

    def test_timeout_cancels_the_underlying_coroutine(self, monkeypatch):
        """Regression test for a confirmed Phase 5 concurrency-review
        finding: concurrent.futures.Future.result(timeout=...) alone only
        stops the *caller* from waiting — it does not cancel the submitted
        work, so an abandoned bridge call could keep occupying a worker in
        the 2-worker claude-loop-bridge pool for the model call's full
        (much larger) retry/backoff duration instead of the caller's
        budget. _call_primary_model_with_budget wraps the coroutine in its
        own asyncio.wait_for(), which cancels it from inside the worker
        thread's own loop — the same mechanism the ASGI path already uses.
        This proves the cancellation actually reaches the coroutine rather
        than letting it run to completion in the background."""
        cancelled = threading.Event()
        completed = threading.Event()

        async def _fake_primary_model(prompt, dynamic_system_context="", max_tokens=3072):
            try:
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            completed.set()
            return {"answer": "should not complete"}

        monkeypatch.setattr(claude, "_call_primary_model",
                             _fake_primary_model)
        monkeypatch.setattr(claude, "AI_TOTAL_BUDGET_SECONDS", 0.05)

        with pytest.raises(TimeoutError):
            asyncio.run(_call_sync_wrapper_from_within_loop())

        # Well under the 0.3s sleep — the worker thread must have already
        # observed and propagated the cancellation by this point.
        assert cancelled.wait(timeout=0.2), (
            "the abandoned coroutine was never cancelled — it would keep "
            "occupying a claude-loop-bridge worker for the full model-call "
            "delay instead of being bounded by AI_TOTAL_BUDGET_SECONDS"
        )
        assert not completed.is_set()
