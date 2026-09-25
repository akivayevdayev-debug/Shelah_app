"""
backend/claude.py model-call hardening:

- The sync /ask synthesis (_call_primary_model_sync) finishes within
  AI_TOTAL_BUDGET_SECONDS even when both providers hang: the Gemini leg is a
  blocking SDK call no asyncio.wait_for can cancel, so every HTTP timeout and
  retry on both legs is clamped to a per-synthesis deadline instead.
- MODEL_REQUEST_TIMEOUT_SECONDS stays under the total budget.
- AsyncAnthropic clients are cached per event loop, so the first call on each
  fresh asyncio.run() loop no longer dies on the previous loop's pooled
  keep-alive connection.
- The Gemini health probe never puts the API key in a URL (it leaked into
  the probe-error log line).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests
import responses as responses_lib

import backend.claude as claude
import backend.health_check as health_check


# ─── Deadline-bounded synthesis ──────────────────────────────────────────────


def _gemini_503():
    from google.genai import errors as genai_errors

    return genai_errors.APIError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})


class _HangingGeminiModels:
    """generate_content blocks for exactly the HTTP timeout it was handed
    (what a hung connection does), then fails with a retryable 503."""

    def __init__(self):
        self.timeouts = []

    def generate_content(self, *, model, contents, config):
        timeout = config.http_options.timeout / 1000
        self.timeouts.append(timeout)
        time.sleep(timeout)
        raise _gemini_503()


class _HangingAnthropicMessages:
    def __init__(self):
        self.timeouts = []

    async def create(self, **kwargs):
        self.timeouts.append(kwargs["timeout"])
        await asyncio.sleep(kwargs["timeout"])
        raise TimeoutError("anthropic read timed out")


@pytest.fixture
def hanging_providers(monkeypatch):
    claude._ensure_genai_loaded()
    gemini_models = _HangingGeminiModels()
    anthropic_messages = _HangingAnthropicMessages()
    monkeypatch.setattr(claude, "_configure_gemini_client", lambda: None)
    monkeypatch.setattr(claude, "_cached_gemini_client", SimpleNamespace(models=gemini_models))
    monkeypatch.setattr(claude, "_get_async_client", lambda: SimpleNamespace(messages=anthropic_messages))
    monkeypatch.setattr(claude._generate_gemini_content_with_retry.retry, "sleep", lambda seconds: None)
    monkeypatch.setattr(claude, "AI_TOTAL_BUDGET_SECONDS", 3)
    monkeypatch.setattr(claude, "MODEL_REQUEST_TIMEOUT_SECONDS", 1.2)
    # Scaled down from the real 10s Gemini minimum / 4s max backoff so the
    # test runs in ~3s; the ratios are what matter.
    monkeypatch.setattr(claude, "_GEMINI_MIN_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(claude, "_GEMINI_RETRY_WAIT_MAX_SECONDS", 0.2)
    return gemini_models, anthropic_messages


def test_sync_synthesis_is_bounded_by_the_total_budget(hanging_providers):
    gemini_models, anthropic_messages = hanging_providers

    started = time.monotonic()
    result = claude._call_primary_model_sync("prompt text")
    elapsed = time.monotonic() - started

    # Before: 3 Gemini attempts x 50s + backoff, then Claude 50s x 3 -- minutes.
    assert elapsed < 3.4
    assert result["error"].startswith("gemini_error:")
    assert result["error"].endswith("ai_budget_exhausted")
    # The 503 was retried while backoff + a minimum attempt still fit (1.8s
    # left after the first), but not a third time with only ~0.6s left...
    assert gemini_models.timeouts == [pytest.approx(1.2, abs=0.05)] * 2
    # ...and the Claude leg wasn't started on the scraps.
    assert anthropic_messages.timeouts == []


def test_claude_leg_gets_the_remaining_budget_as_its_timeout(monkeypatch):
    messages = _HangingAnthropicMessages()
    monkeypatch.setattr(claude, "_get_async_client", lambda: SimpleNamespace(messages=messages))
    monkeypatch.setattr(claude, "MODEL_REQUEST_TIMEOUT_SECONDS", 5)

    async def _run():
        claude._model_deadline.set(time.monotonic() + 1.5)
        return await claude._call_anthropic_httpx_model("prompt", gemini_error="x")

    started = time.monotonic()
    result = asyncio.run(_run())

    assert time.monotonic() - started < 1.8
    assert messages.timeouts == [pytest.approx(1.5, abs=0.05)]
    assert result["error"].startswith("gemini_error: x; anthropic_sdk_error:")


def test_claude_leg_is_skipped_when_no_budget_is_left(monkeypatch):
    messages = _HangingAnthropicMessages()
    monkeypatch.setattr(claude, "_get_async_client", lambda: SimpleNamespace(messages=messages))

    async def _run():
        claude._model_deadline.set(time.monotonic() - 1)
        return await claude._call_anthropic_httpx_model("prompt", gemini_error="gemini_error: x")

    result = asyncio.run(_run())

    assert result["error"] == "gemini_error: gemini_error: x; ai_budget_exhausted"
    assert messages.timeouts == []
    # Running out of budget isn't a provider failure.
    assert health_check.health._circuits["claude"].failures == 0


def test_without_a_deadline_calls_use_the_per_request_ceiling():
    assert claude._model_deadline.get() is None
    assert claude._remaining_model_budget() is None
    assert claude._model_call_timeout() == float(claude.MODEL_REQUEST_TIMEOUT_SECONDS)


@pytest.mark.parametrize("seconds_left", [-1, 0, 9.5])
def test_gemini_attempt_refuses_to_start_under_its_minimum_deadline(seconds_left):
    """Gemini rejects a request deadline under 10s with 400 INVALID_ARGUMENT,
    so such an attempt is never sent (and never trips the circuit)."""
    class _Models:
        def generate_content(self, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("sent a request Gemini would reject")

    token = claude._model_deadline.set(time.monotonic() + seconds_left)
    try:
        with pytest.raises(TimeoutError, match="ai_budget_exhausted"):
            claude._generate_gemini_content_with_retry(SimpleNamespace(models=_Models()), "m", "p")
    finally:
        claude._model_deadline.reset(token)


def test_gemini_attempt_timeout_is_clamped_to_the_remaining_budget():
    seen = []

    class _Models:
        def generate_content(self, *, model, contents, config):
            seen.append(config.http_options.timeout)
            return "ok"

    claude._ensure_genai_loaded()
    token = claude._model_deadline.set(time.monotonic() + 12)
    try:
        assert claude._generate_gemini_content_with_retry(SimpleNamespace(models=_Models()), "m", "p") == "ok"
    finally:
        claude._model_deadline.reset(token)
    assert 11_000 < seen[0] <= 12_000


def test_a_retry_needs_room_for_backoff_plus_a_minimum_attempt():
    from google.genai import errors as genai_errors

    exc = genai_errors.APIError(429, {"error": {"code": 429, "message": "q", "status": "RESOURCE_EXHAUSTED"}})
    for seconds_left, expected in ((15, True), (13, False)):
        token = claude._model_deadline.set(time.monotonic() + seconds_left)
        try:
            assert claude._is_retryable_gemini_error(exc) is expected
        finally:
            claude._model_deadline.reset(token)


def test_primary_model_keeps_an_outer_deadline(monkeypatch):
    seen = []

    def _fake_gemini(prompt, dynamic_system_context="", max_tokens=3072):
        seen.append(claude._model_deadline.get())
        return {"answer": "ok", "confidence": 0.75, "is_fallback": False, "model": "m"}

    monkeypatch.setattr(claude, "_call_gemini_model", _fake_gemini)

    async def _run():
        claude._model_deadline.set(123.0)
        await claude._call_primary_model("prompt")

    asyncio.run(_run())
    assert seen == [123.0]


def test_per_request_timeout_stays_under_the_total_budget():
    assert claude.MODEL_REQUEST_TIMEOUT_SECONDS < claude.AI_TOTAL_BUDGET_SECONDS


# ─── AsyncAnthropic client per event loop ────────────────────────────────────


def test_async_client_is_cached_per_loop(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(claude, "_async_clients_by_loop", {})

    async def _twice():
        return claude._get_async_client(), claude._get_async_client()

    a1, a2 = asyncio.run(_twice())
    b1, _ = asyncio.run(_twice())

    assert a1 is a2
    assert b1 is not a1
    # Only the live loop's entry survives; closed loops are pruned on a miss.
    assert len(claude._async_clients_by_loop) == 1


def test_async_client_rebuilt_when_the_key_changes(monkeypatch):
    monkeypatch.setattr(claude, "_async_clients_by_loop", {})

    async def _run():
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-one")
        first = claude._get_async_client()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-two")
        second = claude._get_async_client()
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        return first, second, claude._get_async_client()

    first, second, missing = asyncio.run(_run())
    assert first is not second
    assert missing is None
    assert claude._async_clients_by_loop == {}


class _KeepAliveMessagesHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep connections open between requests

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.dumps({
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": json.dumps({"ruling": "ok"})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def keepalive_anthropic(monkeypatch, mock_outbound_httpx):
    # conftest's autouse respx mock answers every unmatched httpx request
    # itself; this test needs the real socket to the local server.
    mock_outbound_httpx.route(host="127.0.0.1").pass_through()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _KeepAliveMessagesHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    claude._ensure_anthropic_loaded()
    base_url = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(claude, "_async_clients_by_loop", {})
    # No SDK retries, so a dead pooled connection would surface as a failure
    # instead of being papered over by a retry.
    monkeypatch.setattr(claude, "_build_async_client", lambda api_key: claude.anthropic.AsyncAnthropic(
        api_key=api_key, base_url=base_url, max_retries=0, timeout=5))

    async def _no_cost(**kwargs):
        return None

    monkeypatch.setattr(claude, "record_llm_call", _no_cost)
    yield
    server.shutdown()
    server.server_close()


def test_fallback_works_on_every_fresh_asyncio_run_loop(keepalive_anthropic):
    """Repro of the bug: with one shared client, call 2 (on a new loop) failed
    with APIConnectionError on the first loop's pooled connection."""
    results = [
        asyncio.run(claude._call_anthropic_httpx_model("prompt", gemini_error="x"))
        for _ in range(3)
    ]
    assert [r.get("error") for r in results] == [None, None, None]


# ─── Gemini health probe: key never in the URL ───────────────────────────────


def test_gemini_probe_sends_the_key_in_a_header(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-GEMINI-KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with responses_lib.RequestsMock() as rsps:
        rsps.add(responses_lib.GET, "https://generativelanguage.googleapis.com/v1beta/models", status=200)
        assert health_check._probe_gemini() is True
        request = rsps.calls[0].request
    assert "SECRET-GEMINI-KEY" not in request.url
    assert request.headers["x-goog-api-key"] == "SECRET-GEMINI-KEY"


def test_gemini_probe_failure_log_does_not_contain_the_key(monkeypatch, caplog, mock_outbound_http):
    mock_outbound_http.add_passthru("http://127.0.0.1:9")
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-GEMINI-KEY")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    real_get = requests.get

    def _unreachable(url, **kwargs):
        # Same URL and headers, sent somewhere nothing listens -- a real
        # ConnectionError whose message embeds the request URL.
        return real_get(url.replace("https://generativelanguage.googleapis.com", "http://127.0.0.1:9"),
                        **{**kwargs, "timeout": 1})

    monkeypatch.setattr(health_check.requests, "get", _unreachable)
    with caplog.at_level(logging.WARNING, logger=health_check.logger.name):
        assert health_check.APIHealth()._probe("gemini") is False

    assert "probe error" in caplog.text
    assert "SECRET-GEMINI-KEY" not in caplog.text
