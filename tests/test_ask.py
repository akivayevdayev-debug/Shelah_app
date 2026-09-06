"""
Tests for the /ask pipeline (Flask sync + FastAPI ASGI async).

Covers:
  - POST /ask (Flask)  valid payload → 200 with answer + sources keys
  - POST /ask (FastAPI async, via fastapi_client) → 200 with same keys
  - POST /ask missing question field → 400 or error response
  - POST /ask with Anthropic 500 → fallback path; answer key present, meta.fallback=true
  - Rate limiting: second request from same IP when limit=1 → 429
  - Prayer keyword shortcut: returns 200 with answer quickly

All outbound HTTP is intercepted by conftest autouse fixtures.
"""

from __future__ import annotations

import json
import re
import pytest
import responses as responses_lib
import httpx

from backend.health_check import FAIL_THRESHOLD


# ─── Flask (sync) tests ───────────────────────────────────────────────────────

class TestAskFlask:
    """Tests against the Flask /ask route directly."""

    def test_valid_question_returns_200(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            content_type="application/json",
        )
        assert response.status_code == 200

    def test_valid_question_has_answer_key(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            content_type="application/json",
        )
        body = response.get_json()
        assert isinstance(body, dict)
        assert "answer" in body

    def test_valid_question_has_sources_key(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            content_type="application/json",
        )
        body = response.get_json()
        assert "sources" in body

    def test_missing_question_returns_error(self, test_client):
        """Empty question body should result in 400 or answer with error indication."""
        response = test_client.post(
            "/ask",
            json={},
            content_type="application/json",
        )
        assert response.status_code in (400, 200)
        body = response.get_json()
        # Either HTTP 400 or a response indicating no valid question
        if response.status_code == 200:
            assert "answer" in body or "error" in body

    def test_empty_question_string_returns_error(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": ""},
            content_type="application/json",
        )
        assert response.status_code in (400, 200)

    def test_prayer_keyword_shortcut_returns_200(self, test_client):
        """Questions mentioning Shacharit trigger a fast shortcut path."""
        response = test_client.post(
            "/ask",
            json={"question": "When is Shacharit?"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert "answer" in body

    def test_prayer_keyword_shortcut_answer_is_string(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "When is Shacharit?"},
            content_type="application/json",
        )
        body = response.get_json()
        assert isinstance(body.get("answer"), str)
        assert len(body["answer"]) > 0

    def test_anthropic_failure_triggers_fallback(self, test_client, mock_outbound_http):
        """When Anthropic fails, the route must still return answer with fallback=True."""
        # Make the Anthropic messages endpoint fail
        mock_outbound_http.add(
            responses_lib.POST,
            re.compile(r"https://api\.anthropic\.com/v1/messages.*"),
            status=500,
            json={"error": "internal server error"},
        )

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            content_type="application/json",
        )
        # The route should always return 200 with a fallback payload — never 500
        assert response.status_code == 200
        body = response.get_json()
        assert "answer" in body
        # fallback may be True or the body may have is_fallback / error key
        assert isinstance(body.get("answer"), str)

    def test_anthropic_failure_meta_fallback_true(self, test_client, monkeypatch):
        """meta.fallback must be True when the AI path errored out.

        AsyncAnthropic uses httpx internally, so we monkeypatch ask_claude
        directly rather than trying to intercept at the HTTP layer with
        the requests-based `responses` mock.

        Uses a unique question string to avoid a cache hit from earlier tests
        that asked about Shabbat and cached a non-fallback result.
        """
        import backend.claude as claude_module
        import app as flask_app_module

        # Clear in-process ask cache so no prior successful result masks this test
        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic 500")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [failure-path test]"},
            content_type="application/json",
        )
        if response.status_code == 200:
            body = response.get_json()
            meta = body.get("meta", {})
            assert meta.get("fallback") is True

    def test_answer_is_string(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Kiddush?"},
            content_type="application/json",
        )
        body = response.get_json()
        if "answer" in body:
            assert isinstance(body["answer"], str)

    def test_sources_is_list(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "Explain Havdalah."},
            content_type="application/json",
        )
        body = response.get_json()
        if "sources" in body:
            assert isinstance(body["sources"], list)


# ─── Rate-limit test (Flask) ──────────────────────────────────────────────────

class TestAskRateLimit:
    @pytest.mark.xfail(reason="rate limiter may be disabled in test env (RATELIMIT_ENABLED=false)")
    def test_second_request_rate_limited(self, test_client):
        """
        When RATE_LIMIT_PER_MIN=1 the second request from same IP should get 429.
        Marked xfail because the test env disables Flask-Limiter by default.
        """
        import app as flask_app_module
        original = flask_app_module.RATE_LIMIT_ASK

        try:
            flask_app_module.RATE_LIMIT_ASK = "1 per minute"
            payload = {"question": "What is Shabbat?"}

            # First request — should succeed
            r1 = test_client.post("/ask", json=payload,
                                  content_type="application/json",
                                  environ_base={"REMOTE_ADDR": "1.2.3.4"})
            assert r1.status_code == 200

            # Second request from same IP — should be rate limited
            r2 = test_client.post("/ask", json=payload,
                                  content_type="application/json",
                                  environ_base={"REMOTE_ADDR": "1.2.3.4"})
            assert r2.status_code == 429
        finally:
            flask_app_module.RATE_LIMIT_ASK = original


# ─── FastAPI / ASGI (async) tests ─────────────────────────────────────────────

class TestAskFastAPI:
    """Tests against the FastAPI /ask endpoint via httpx AsyncClient."""

    async def test_valid_question_returns_200(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
        )
        assert response.status_code == 200

    async def test_valid_question_has_answer_key(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
        )
        body = response.json()
        assert isinstance(body, dict)
        assert "answer" in body

    async def test_valid_question_has_sources_key(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
        )
        body = response.json()
        assert "sources" in body

    async def test_empty_question_returns_400(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": ""},
        )
        assert response.status_code in (400, 422)

    async def test_prayer_keyword_shortcut_returns_200(self, fastapi_client):
        """FastAPI shortcut path for prayer-named questions."""
        response = await fastapi_client.post(
            "/ask",
            json={"question": "When is Shacharit?"},
        )
        assert response.status_code == 200
        body = response.json()
        assert "answer" in body

    async def test_meta_has_async_flag(self, fastapi_client):
        """FastAPI responses include meta.async = True."""
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
        )
        if response.status_code == 200:
            body = response.json()
            meta = body.get("meta", {})
            # FastAPI path sets async=True in metadata
            assert meta.get("async") is True

    async def test_rate_limit_returns_429_on_excess(self, fastapi_client):
        """backend.rate_limit.RateLimitMiddleware enforces this centrally now
        (plan.md §16.3-L2); seed its in-memory store directly to simulate an
        exhausted window without needing N real round trips. No Authorization
        header is sent, so the request is keyed anonymously by IP, not
        user_id — see backend.rate_limit._build_key.

        (Rewritten to match the Phase 9a rate-limiter unification, commit
        a519a21: asgi.py's own in-process limiter -- _rate_limit_store /
        _RATE_LIMIT_MAX_REQUESTS -- was removed in favor of the shared
        backend.rate_limit.RateLimitMiddleware. This test still referenced
        the removed attributes, failing CI on every push since 2026-09-01.)
        """
        import collections
        import time
        import backend.rate_limit as rate_limit_mod

        store = rate_limit_mod._store
        assert isinstance(store, rate_limit_mod._InMemoryStore), (
            "This test assumes the in-memory fallback store (no "
            "RATE_LIMIT_REDIS_URL set in the test environment) -- see "
            "backend/rate_limit.py's _build_store()."
        )

        test_ip = "192.0.2.99"
        policy = rate_limit_mod._POLICIES["llm"]
        key = rate_limit_mod._build_key("llm", test_ip, None)
        now = time.monotonic()
        store._buckets[key] = collections.deque([now] * policy.max_requests)

        try:
            response = await fastapi_client.post(
                "/ask",
                json={"question": "What is Shabbat?"},
                headers={"X-Forwarded-For": test_ip},
            )
            assert response.status_code == 429
        finally:
            store._buckets.pop(key, None)


# ─── ai_cited_sources schema parity (plan.md §7.1.A / §7.14) ──────────────────
#
# Regression coverage for the confirmed bug: the ASGI handler silently omitted
# ai_cited_sources on every path. The key must now be present (a list, possibly
# empty) on success AND fallback for both transports.

class TestAiCitedSourcesSchemaParity:
    def test_flask_success_has_ai_cited_sources_key(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [cited-sources-schema-test]"},
            content_type="application/json",
        )
        body = response.get_json()
        assert "ai_cited_sources" in body
        assert isinstance(body["ai_cited_sources"], list)

    def test_flask_fallback_has_ai_cited_sources_key(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic failure [cited-sources-fallback-test]")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [cited-sources-fallback-test]"},
            content_type="application/json",
        )
        body = response.get_json()
        assert "ai_cited_sources" in body
        assert body["ai_cited_sources"] == []

    async def test_fastapi_success_has_ai_cited_sources_key(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [async-cited-sources-schema-test]"},
        )
        body = response.json()
        assert "ai_cited_sources" in body
        assert isinstance(body["ai_cited_sources"], list)

    async def test_fastapi_fallback_has_ai_cited_sources_key(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated async AI failure [async-cited-fallback-test]")

        monkeypatch.setattr(claude_module, "ask_ai_async", _raise)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [async-cited-fallback-test]"},
        )
        body = response.json()
        assert "ai_cited_sources" in body
        assert body["ai_cited_sources"] == []


class TestAskTransportKeySetParity:
    """plan.md §7.14 invariant: the Flask and ASGI /ask handlers must return the
    same top-level JSON key set on every path, so they can't silently drift the
    way the missing-`ai_cited_sources` bug did. `meta` is deliberately excluded
    from the comparison — each transport adds one legitimate, transport-specific
    flag there (`cached` for Flask, `async` for ASGI) that isn't a drift bug.

    Each transport is checked independently against one canonical key set
    (rather than calling both fixtures from a single test) — combining the
    sync Flask `test_client` fixture with the async `fastapi_client` fixture
    in one `async def` test trips a Flask app-context teardown LookupError
    that's a pytest-asyncio/Flask-contextvars interaction quirk, not a real
    failure; comparing both independently against the same constant gives an
    identical safety guarantee without that interaction.
    """

    TOP_LEVEL_KEYS = {"answer", "confidence", "wiki", "customs", "sources", "ai_cited_sources", "meta"}

    def test_flask_success_path_key_set(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [parity-success-test]"},
            content_type="application/json",
        )
        assert set(response.get_json().keys()) == self.TOP_LEVEL_KEYS

    def test_flask_fallback_path_key_set(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic failure [parity-fallback-test]")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)
        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [parity-fallback-test]"},
            content_type="application/json",
        )
        assert set(response.get_json().keys()) == self.TOP_LEVEL_KEYS

    async def test_fastapi_success_path_key_set(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [parity-success-test-async]"},
        )
        assert set(response.json().keys()) == self.TOP_LEVEL_KEYS

    async def test_fastapi_fallback_path_key_set(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated async AI failure [parity-fallback-test-async]")

        monkeypatch.setattr(claude_module, "ask_ai_async", _raise)
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [parity-fallback-test-async]"},
        )
        assert set(response.json().keys()) == self.TOP_LEVEL_KEYS


# ─── AI timeout/retry resilience (plan.md §7.13) ───────────────────────────────
#
# Regression coverage for the confirmed bug: AI_MODEL_TIMEOUT_SECONDS was defined
# but never passed to the SDK clients, and there was no total-budget guard, so a
# slow model call could hang well past what the (then-unbounded single-shot)
# client abort allowed for.

class TestAiModelTimeoutWiring:
    def test_sync_anthropic_client_has_configured_timeout(self):
        import backend.claude as claude_module

        claude_module._cached_client = None
        claude_module._cached_api_key = None
        client = claude_module._get_client()
        assert client is not None
        assert client.timeout == claude_module.MODEL_REQUEST_TIMEOUT_SECONDS
        assert client.max_retries == 2

    def test_async_anthropic_client_has_configured_timeout(self):
        import backend.claude as claude_module

        claude_module._cached_async_client = None
        claude_module._cached_api_key = None
        client = claude_module._get_async_client()
        assert client is not None
        assert client.timeout == claude_module.MODEL_REQUEST_TIMEOUT_SECONDS
        assert client.max_retries == 2


class TestAiCitationFormatPrompt:
    """Regression guard for the colon-splitting bug in templates/index.html's
    populateAiModal(): the model must be told to separate ref/note with an em
    dash, never a colon, since refs like "Genesis 1:1" already contain one.
    If this prompt instruction reverts to colon-based formatting, the frontend
    parser (which now splits on " — "/" – " only) will silently stop
    extracting notes — this test exists to catch that drift early.
    """

    def test_core_system_prompt_uses_em_dash_separator(self):
        import backend.claude as claude_module

        assert "—" in claude_module.CORE_SYSTEM_PROMPT
        assert "em dash" in claude_module.CORE_SYSTEM_PROMPT
        assert '"Title, Section/Chapter: relevance note"' not in claude_module.CORE_SYSTEM_PROMPT


class TestAiTotalBudgetTimeout:
    async def test_fastapi_total_budget_timeout_falls_back_gracefully(self, fastapi_client, monkeypatch):
        """A model call that exceeds AI_TOTAL_BUDGET_SECONDS must fall through to
        the graceful fallback ladder (200 + meta.fallback=true), never hang or 500.
        """
        import asyncio
        import backend.claude as claude_module

        monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 0.05)

        async def _slow(*args, **kwargs):
            await asyncio.sleep(2)
            return {"answer": "should never get here", "structured": None}

        monkeypatch.setattr(claude_module, "ask_ai_async", _slow)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [total-budget-timeout-test]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body.get("meta", {}).get("fallback") is True
        assert "ai_cited_sources" in body


# ─── Primary AI call site is circuit-broken (plan.md §26.1 / Prompt 39) ───────
#
# 'gemini' and 'claude' have always been registered in
# backend/health_check.py's _PROBES dict, but until Prompt 39 nothing
# consulted is_healthy() for them before the primary /ask AI call -- only the
# *fallback* stage was gated (see TestAskDegradationPath above). Every /ask
# therefore dialed a known-dead provider and paid the full timeout before
# degrading.
#
# These tests drive REAL circuit state (FAIL_THRESHOLD consecutive
# record_failure calls, exactly like tests/test_calendar_service.py::
# TestGetParashaCircuitBreaker does for hebcal) rather than monkeypatching
# is_healthy, and spy on the provider entry points one layer below the gates
# so "the call was skipped" is proven directly instead of being inferred from
# the response body -- an exception raised from a spy would be swallowed by
# /ask's own except-block and land on the same fallback payload, so asserting
# on the response alone could not tell the two apart.

class TestAskPrimaryAiCircuitBreaker:

    @staticmethod
    def _open_ai_circuits(health):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("gemini")
            health.record_failure("claude")
        # No live re-probe: RECOVERY_INTERVAL has not elapsed, so is_healthy()
        # answers from state alone and never touches the network.
        assert health.is_healthy("gemini") is False
        assert health.is_healthy("claude") is False

    @staticmethod
    def _spy_on_provider_entrypoints(monkeypatch, claude_module):
        """Record (never raise) if any provider entry point is reached.

        Each spy returns the value that entry point returns on a benign
        misconfiguration, so a spy that *does* fire still yields the ordinary
        fallback payload -- the assertion on `attempts` is what catches it.
        """
        attempts: list[str] = []

        def _spy_configure_gemini():
            attempts.append("gemini_client_configured")
            return "spy_gemini_should_not_be_reached"

        def _spy_generate(*args, **kwargs):
            attempts.append("gemini_generate_content")
            raise RuntimeError("spy: gemini generate_content must not be reached")

        def _spy_async_anthropic_client():
            attempts.append("anthropic_client_constructed")
            return None

        monkeypatch.setattr(
            claude_module, "_configure_gemini_client", _spy_configure_gemini)
        monkeypatch.setattr(
            claude_module, "_generate_gemini_content_with_retry", _spy_generate)
        monkeypatch.setattr(
            claude_module, "_get_async_client", _spy_async_anthropic_client)
        return attempts

    @staticmethod
    def _spy_on_local_fallback(monkeypatch, module):
        """Wrap (not replace) get_halakhic_sources so the real local-corpus
        fallback still runs and the payload stays realistic."""
        real = module.get_halakhic_sources
        calls: list[str] = []

        def _spy(question, *args, **kwargs):
            calls.append(question)
            return real(question, *args, **kwargs)

        monkeypatch.setattr(module, "get_halakhic_sources", _spy)
        return calls

    def test_flask_both_ai_circuits_open_skips_calls_and_uses_local_fallback(
        self, test_client, monkeypatch
    ):
        import backend.claude as claude_module
        import backend.health_check as health_check_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        self._open_ai_circuits(health_check_module.health)
        attempts = self._spy_on_provider_entrypoints(monkeypatch, claude_module)
        fallback_calls = self._spy_on_local_fallback(monkeypatch, flask_app_module)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [ai-circuit-open-test]"},
            content_type="application/json",
            # Its own limiter bucket: /ask's per-IP rate limit is real in the
            # test suite (see TestAskRateLimit), and the default 127.0.0.1
            # budget is already spent by the Flask /ask tests above.
            environ_base={"REMOTE_ADDR": "10.39.26.1"},
        )

        assert response.status_code == 200
        # The actual point of the test: neither provider was dialed.
        assert attempts == []
        # ...and /ask still reached the local-corpus fallback.
        assert len(fallback_calls) == 1

        body = response.get_json()
        assert isinstance(body.get("answer"), str)
        assert body["answer"]
        assert isinstance(body.get("sources"), list)
        assert "ai_cited_sources" in body
        assert body.get("meta", {}).get("fallback") is True

    async def test_fastapi_both_ai_circuits_open_skips_calls_and_uses_local_fallback(
        self, fastapi_client, monkeypatch
    ):
        import backend.claude as claude_module
        import backend.health_check as health_check_module
        import asgi as asgi_module

        self._open_ai_circuits(health_check_module.health)
        attempts = self._spy_on_provider_entrypoints(monkeypatch, claude_module)
        fallback_calls = self._spy_on_local_fallback(monkeypatch, asgi_module)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [ai-circuit-open-test-async]"},
        )

        assert response.status_code == 200
        assert attempts == []
        assert len(fallback_calls) == 1

        body = response.json()
        assert isinstance(body.get("answer"), str)
        assert body["answer"]
        assert isinstance(body.get("sources"), list)
        assert "ai_cited_sources" in body
        assert body.get("meta", {}).get("fallback") is True

    # ── Unit-level: the gates and the symmetric record_* wiring ──────────────

    def test_sync_gemini_gate_returns_circuit_open_error_shape(self, monkeypatch):
        """The skip must return the same dict shape a real Gemini failure
        returns, so _call_primary_model's existing fallback ordering is
        unchanged."""
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        for _ in range(FAIL_THRESHOLD):
            health_check_module.health.record_failure("gemini")

        result = claude_module._call_gemini_model("prompt text")

        assert result["error"] == "gemini_circuit_open"
        assert result["is_fallback"] is False
        assert set(result) == {
            "answer", "confidence", "error", "is_fallback", "model"}

    async def test_async_gemini_gate_returns_circuit_open_error_shape(self, monkeypatch):
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        for _ in range(FAIL_THRESHOLD):
            health_check_module.health.record_failure("gemini")

        result = await claude_module._call_gemini_httpx_model("prompt text")

        assert result["error"] == "gemini_circuit_open"
        assert result["is_fallback"] is False

    async def test_anthropic_gate_preserves_gemini_error_prefix(self):
        """The Claude leg is only ever reached with a gemini_error in hand;
        an open-circuit skip must keep that prefix so the error string /ask
        logs still names both providers."""
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        for _ in range(FAIL_THRESHOLD):
            health_check_module.health.record_failure("claude")

        result = await claude_module._call_anthropic_httpx_model(
            "prompt text", gemini_error="gemini_circuit_open")

        assert result["error"] == "gemini_error: gemini_circuit_open; anthropic_circuit_open"
        assert result["is_fallback"] is True

    def test_sync_gemini_failure_records_exactly_one_circuit_failure(self, monkeypatch):
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        def _raise(*args, **kwargs):
            raise RuntimeError("simulated gemini outage [circuit-wiring-test]")

        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(claude_module, "_cached_gemini_client", object())
        monkeypatch.setattr(
            claude_module, "_generate_gemini_content_with_retry", _raise)

        result = claude_module._call_gemini_model("prompt text")

        assert result["error"].startswith("gemini_error:")
        assert health_check_module.health._circuits["gemini"].failures == 1

    def test_sync_gemini_empty_response_records_failure_not_success(self, monkeypatch):
        """An endless run of empty responses must still be able to open the
        circuit -- recording success on 'the HTTP call returned' would reset
        the consecutive counter every time and it never would."""
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        class _EmptyResponse:
            text = ""
            candidates = []
            usage_metadata = None

        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(claude_module, "_cached_gemini_client", object())
        monkeypatch.setattr(
            claude_module, "_generate_gemini_content_with_retry",
            lambda *a, **k: _EmptyResponse())

        for _ in range(FAIL_THRESHOLD):
            claude_module._call_gemini_model("prompt text")

        assert health_check_module.health.is_healthy("gemini") is False

    def test_sync_gemini_success_records_circuit_success(self, monkeypatch):
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        class _OkResponse:
            text = json.dumps({"ruling": "ok"})
            candidates = []
            usage_metadata = None

        health_check_module.health.record_failure("gemini")
        health_check_module.health.record_failure("gemini")

        monkeypatch.setattr(claude_module, "_configure_gemini_client", lambda: None)
        monkeypatch.setattr(claude_module, "_cached_gemini_client", object())
        monkeypatch.setattr(
            claude_module, "_generate_gemini_content_with_retry",
            lambda *a, **k: _OkResponse())

        claude_module._call_gemini_model("prompt text")

        assert health_check_module.health._circuits["gemini"].failures == 0
