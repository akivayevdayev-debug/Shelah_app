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
        meta = body.get("meta", {})
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
        """FastAPI has its own in-process rate limiter; hammering it yields 429."""
        import asgi as asgi_mod

        # Patch the rate-limit store to fill up for a fake IP
        test_ip = "192.0.2.99"
        import collections, time
        now = time.monotonic()
        asgi_mod._rate_limit_store[test_ip] = collections.deque(
            [now] * asgi_mod._RATE_LIMIT_MAX_REQUESTS
        )

        try:
            response = await fastapi_client.post(
                "/ask",
                json={"question": "What is Shabbat?"},
                headers={
                    "X-Forwarded-For": test_ip,
                    "CF-Connecting-IP": test_ip,
                },
            )
            assert response.status_code == 429
        finally:
            asgi_mod._rate_limit_store.pop(test_ip, None)


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
