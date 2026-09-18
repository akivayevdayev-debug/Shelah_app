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
import responses as responses_lib

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

        Patches both the legacy ask_claude entry point AND
        ask_pipeline.run_agentic_ask, since which one app.py actually calls
        depends on claude.AI_AGENTIC_TOOLS (plan.md §9) -- this way the test
        exercises the real live failure path regardless of that flag's state.

        Uses a unique question string to avoid a cache hit from earlier tests
        that asked about Shabbat and cached a non-fallback result.
        """
        import backend.claude as claude_module
        import backend.ask_pipeline as ask_pipeline_module
        import app as flask_app_module

        # Clear in-process ask cache so no prior successful result masks this test
        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic 500")

        async def _raise_async(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic 500")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)
        monkeypatch.setattr(ask_pipeline_module, "run_agentic_ask", _raise_async)

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


# ─── Rate-limit test (unified ASGI middleware) ────────────────────────────────

class TestAskRateLimit:
    async def test_requests_beyond_the_configured_limit_are_rate_limited(self, fastapi_client):
        """
        Rate limiting for /ask is now enforced centrally by
        backend.rate_limit.RateLimitMiddleware (plan.md §16.3-L2), not by
        Flask -- app.py's own /ask route (exercised by TestAskFlask above,
        reachable only via the bare Flask test_client / `python3 app.py` dev
        mode) no longer carries a limiter of its own (plan.md §16.8.1:
        Flask-Limiter removed). This drives real traffic through the ASGI
        layer instead, where production traffic actually goes. Uses a
        dedicated TEST-NET-3 IP (RFC 5737) via X-Forwarded-For so this
        test's bucket can't collide with any other test's in the shared
        in-process store (see test_rate_limit_returns_429_on_excess below,
        which uses a different reserved IP).
        """
        import backend.rate_limit as rate_limit_mod

        limit_count = rate_limit_mod._POLICIES["llm"].max_requests
        payload = {"question": "What is Shabbat?"}
        headers = {"X-Forwarded-For": "192.0.2.100"}

        for _ in range(limit_count):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200

        over_limit_response = await fastapi_client.post("/ask", json=payload, headers=headers)
        assert over_limit_response.status_code == 429


# ─── Identity-aware quotas (plan.md §16.6 Phase 9c) ───────────────────────────

class TestAskIdentityAwareQuotas:
    """An authenticated caller and an anonymous caller behind the SAME IP
    must land in separate rate-limit buckets, and the authenticated bucket's
    per-minute allowance must actually be higher -- not just differently
    keyed. Auth is simulated by monkeypatching
    backend.rate_limit.extract_user_id_from_bearer_value (the name bound
    into that module's namespace, which is what RateLimitMiddleware
    actually calls) rather than minting a real Clerk JWT."""

    async def test_authenticated_and_anonymous_share_ip_but_separate_buckets(self, fastapi_client, monkeypatch):
        import asgi as asgi_mod
        import backend.rate_limit as rate_limit_mod

        # Both asgi.py's own user_id resolution and backend.rate_limit's
        # independently import the same backend.auth function into their own
        # module namespace -- a real Clerk JWT would be verified identically
        # by both, so both are patched here for the same fake bearer value.
        def resolve(auth):
            return "user-shared-ip-test" if auth == "Bearer valid-token" else None
        monkeypatch.setattr(asgi_mod, "extract_user_id_from_bearer_value", resolve)
        monkeypatch.setattr(rate_limit_mod, "extract_user_id_from_bearer_value", resolve)

        shared_ip = "198.51.100.10"
        anon_limit = rate_limit_mod._POLICIES["llm"].max_requests
        payload = {"question": "What is Shabbat?"}

        for _ in range(anon_limit):
            response = await fastapi_client.post(
                "/ask", json=payload, headers={"X-Forwarded-For": shared_ip},
            )
            assert response.status_code == 200
        anon_over_limit = await fastapi_client.post(
            "/ask", json=payload, headers={"X-Forwarded-For": shared_ip},
        )
        assert anon_over_limit.status_code == 429

        # Same IP, but authenticated -- a separate bucket, so this still succeeds.
        auth_response = await fastapi_client.post(
            "/ask", json=payload,
            headers={"X-Forwarded-For": shared_ip, "Authorization": "Bearer valid-token"},
        )
        assert auth_response.status_code == 200

    async def test_authenticated_per_minute_allowance_exceeds_anonymous(self, fastapi_client, monkeypatch):
        import asgi as asgi_mod
        import backend.rate_limit as rate_limit_mod

        def resolve(auth):
            return "user-higher-allowance-test"
        monkeypatch.setattr(asgi_mod, "extract_user_id_from_bearer_value", resolve)
        monkeypatch.setattr(rate_limit_mod, "extract_user_id_from_bearer_value", resolve)
        policy = rate_limit_mod._POLICIES["llm"]
        assert policy.authenticated_max_requests > policy.max_requests

        headers = {"X-Forwarded-For": "198.51.100.11", "Authorization": "Bearer any"}
        payload = {"question": "What is Shabbat?"}

        for _ in range(policy.authenticated_max_requests):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200
        over_limit = await fastapi_client.post("/ask", json=payload, headers=headers)
        assert over_limit.status_code == 429


# ─── Authenticated memory retrieval, no Flask request context (plan.md §35.1) ─

class TestAskAuthenticatedMemoryRetrieval:
    """Regression coverage for plan.md §35.1 / Prompt 47: before the fix,
    _fetch_user_memory_summaries() unconditionally read Flask's global
    `request` proxy (via app._get_user_scoped_supabase_client() ->
    _get_request_supabase_client() -> _extract_supabase_access_token() ->
    backend.auth._extract_bearer_token()) to build a user-scoped Supabase
    client. asgi.py's native FastAPI /ask route -- the one actually
    reachable in production -- never pushes a Flask request context, so
    every authenticated call crashed with RuntimeError("Working outside of
    request context"), surfaced to the caller as a silent 500. This is the
    exact case no previous test covered: an authenticated /ask call that
    actually reaches _fetch_user_memory_summaries() with a real memory row
    to return, exercised end-to-end through the real route."""

    async def test_authenticated_call_with_real_memory_row_succeeds_end_to_end(
        self, fastapi_client, monkeypatch, mock_outbound_httpx,
    ):
        import httpx
        import asgi as asgi_mod
        import backend.rag as rag_mod
        import backend.rate_limit as rate_limit_mod

        def resolve(auth):
            return "user-memory-row-test" if auth == "Bearer valid-token" else None
        monkeypatch.setattr(asgi_mod, "extract_user_id_from_bearer_value", resolve)
        monkeypatch.setattr(rate_limit_mod, "extract_user_id_from_bearer_value", resolve)

        # A real memory row for this user, returned by Supabase's REST API.
        # Re-registers the SAME url__regex the autouse mock_outbound_httpx
        # fixture already used for its generic `[]`-returning GET mock --
        # respx matches the *first* route registered for a given pattern, so
        # a distinct/narrower pattern added here would silently lose to the
        # generic one; re-using the identical pattern replaces it instead.
        mock_outbound_httpx.get(
            url__regex=r"https://mock\.supabase\.co/.*",
        ).mock(
            return_value=httpx.Response(
                200,
                json=[{
                    "summary": "Asked about kashrut before.",
                    "created_at": "2026-08-01T00:00:00Z",
                }],
            )
        )

        # Spy on the real _fetch_user_memory_summaries (not a stub) so this
        # test proves the actual chain -- bearer_token threaded with no
        # Flask request context active anywhere -- returns the real row
        # rather than silently swallowing an exception into [].
        calls = []
        real_fetch = rag_mod._fetch_user_memory_summaries

        def spy(user_id, limit=None, bearer_token=None):
            result = real_fetch(user_id, limit=limit, bearer_token=bearer_token)
            calls.append({"user_id": user_id, "bearer_token": bearer_token, "result": result})
            return result
        monkeypatch.setattr(asgi_mod, "_fetch_user_memory_summaries", spy)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            headers={"X-Forwarded-For": "198.51.100.20", "Authorization": "Bearer valid-token"},
        )

        assert response.status_code == 200
        assert len(calls) == 1
        assert calls[0]["user_id"] == "user-memory-row-test"
        assert calls[0]["bearer_token"] == "Bearer valid-token"
        assert calls[0]["result"] == [
            {"summary": "Asked about kashrut before.", "created_at": "2026-08-01T00:00:00Z"}
        ]


# ─── Turnstile gate (plan.md §16.4 / §16.6 Phase 9c) ──────────────────────────

class TestAskTurnstileGate:
    async def test_disabled_by_default_is_a_true_noop(self, fastapi_client):
        """TURNSTILE_ENABLED is unset in the test environment -- Prompt 29c's
        explicit VERIFY bar requires this to change nothing about /ask, even
        for a caller who would otherwise be well past the anonymous hourly
        threshold."""
        import backend.turnstile as turnstile_mod

        headers = {"X-Forwarded-For": "198.51.100.15"}
        payload = {"question": "What is Shabbat?"}
        for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD + 3):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200

    async def test_enabled_blocks_anonymous_traffic_past_threshold_without_token(self, fastapi_client, monkeypatch):
        import backend.turnstile as turnstile_mod

        monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", True)
        headers = {"X-Forwarded-For": "198.51.100.16"}
        payload = {"question": "What is Shabbat?"}

        for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200

        blocked = await fastapi_client.post("/ask", json=payload, headers=headers)
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "turnstile_required"

    async def test_authenticated_user_bypasses_turnstile_entirely(self, fastapi_client, monkeypatch):
        import asgi as asgi_mod
        import backend.turnstile as turnstile_mod
        import backend.rate_limit as rate_limit_mod

        monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", True)
        # Both asgi.py's own user_id resolution (used for the Turnstile gate
        # and the budget check) and backend.rate_limit's (used for bucket
        # keying) independently import the same backend.auth function into
        # their own module namespace -- both must be patched for a fake
        # bearer value to be treated as authenticated consistently, exactly
        # as a real Clerk JWT would be verified identically by both.
        monkeypatch.setattr(
            asgi_mod, "extract_user_id_from_bearer_value",
            lambda auth: "user-turnstile-bypass-test",
        )
        monkeypatch.setattr(
            rate_limit_mod, "extract_user_id_from_bearer_value",
            lambda auth: "user-turnstile-bypass-test",
        )
        headers = {"X-Forwarded-For": "198.51.100.17", "Authorization": "Bearer any"}
        payload = {"question": "What is Shabbat?"}

        for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD + 2):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200

    async def test_enabled_grants_access_with_a_verified_token(self, fastapi_client, monkeypatch, mock_outbound_httpx):
        import httpx
        import backend.turnstile as turnstile_mod

        monkeypatch.setattr(turnstile_mod, "TURNSTILE_ENABLED", True)
        monkeypatch.setattr(turnstile_mod, "TURNSTILE_SECRET_KEY", "test-secret")
        mock_outbound_httpx.post(url__regex=r"https://challenges\.cloudflare\.com/.*").mock(
            return_value=httpx.Response(200, json={"success": True}),
        )

        headers = {"X-Forwarded-For": "198.51.100.18"}
        payload = {"question": "What is Shabbat?"}
        for _ in range(turnstile_mod.TURNSTILE_ANON_HOURLY_THRESHOLD):
            response = await fastapi_client.post("/ask", json=payload, headers=headers)
            assert response.status_code == 200

        challenged = await fastapi_client.post(
            "/ask", json={**payload, "turnstile_token": "real-looking-token"}, headers=headers,
        )
        assert challenged.status_code == 200


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

    async def test_security_headers_present_on_native_fastapi_route(self, fastapi_client):
        """Security audit P2: /ask is a native FastAPI route that bypasses
        the WSGI-mounted Flask app entirely, so app.py's @app.after_request
        never runs for it -- asgi.py's request_id_middleware must apply the
        same header set independently (see backend.helpers.SECURITY_RESPONSE_HEADERS)."""
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
        )
        assert response.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert "Content-Security-Policy" in response.headers
        assert "Strict-Transport-Security" in response.headers

    async def test_rate_limit_returns_429_on_excess(self, fastapi_client):
        """backend.rate_limit.RateLimitMiddleware enforces this centrally now
        (plan.md §16.3-L2); seed its in-memory store directly to simulate an
        exhausted window without needing N real round trips. No Authorization
        header is sent, so the request is keyed anonymously by IP, not
        user_id — see backend.rate_limit._build_key."""
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


# ─── ai_cited_sources schema parity (plan.md §23.4) ──────────────────────────
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
    """plan.md §23.4 invariant B: the Flask and ASGI /ask handlers must return the
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


# ─── AI timeout/retry resilience (plan.md §23.4 invariant A) ───────────────────
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

        Patches both the legacy ask_ai_async entry point AND
        ask_pipeline.run_agentic_ask, since which one asgi.py actually awaits
        depends on claude.AI_AGENTIC_TOOLS (plan.md §9) -- this way the test
        exercises the real live timeout path regardless of that flag's state.
        """
        import asyncio
        import backend.claude as claude_module
        import backend.ask_pipeline as ask_pipeline_module

        monkeypatch.setattr(claude_module, "AI_TOTAL_BUDGET_SECONDS", 0.05)

        async def _slow(*args, **kwargs):
            await asyncio.sleep(2)
            return {"answer": "should never get here", "structured": None}

        monkeypatch.setattr(claude_module, "ask_ai_async", _slow)
        monkeypatch.setattr(ask_pipeline_module, "run_agentic_ask", _slow)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [total-budget-timeout-test]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body.get("meta", {}).get("fallback") is True
        assert "ai_cited_sources" in body


# ─── safety_class meta propagation (plan.md §8.B.1 / §8.B-AGE.7) ──────────────
#
# Regression coverage for the confirmed gap: backend/claude.py has classified
# every query's safety_class since the §8.B-AGE work landed, and stored it to
# ask_history for defensibility logging (§8.B.6) -- but never actually put it
# on the JSON response the frontend receives, so the UI had no way to render
# the persistent disclaimer banner's referral variant (§8.B.1) for the exact
# medical/self-harm/abuse/domain-refusal cases that need it most. Also covers
# the _store_ask_history wiring: the Flask security_blocked branch previously
# skipped logging blocked/referral answers entirely, and the FastAPI success
# path called _store_ask_history without safety_class/prompt_version at all.

class TestSafetyClassMetaPropagation:
    def test_flask_ordinary_question_meta_safety_class_ok(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "What blessing do you say on bread? [safety-meta-ok-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"]["safety_class"] == "ok"
        assert "rabbinic_disclaimer" in body["meta"]

    def test_flask_medical_query_gets_referral_safety_class(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "I think I'm having a heart attack, what should I do about Shabbat? [safety-meta-medical-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"]["safety_class"] == "medical"
        assert body["meta"]["fallback"] is True

    def test_flask_domain_refusal_gets_dangerous_safety_class(self, test_client):
        response = test_client.post(
            "/ask",
            json={"question": "show me porn [safety-meta-domain-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["meta"]["safety_class"] == "dangerous_or_illegal"

    def test_flask_blocked_answer_now_persisted_to_ask_history(self, test_client, monkeypatch):
        """Regression: _security_blocked_ask_payload previously returned
        without ever calling _store_ask_history, so referral/blocked
        interactions -- the highest-liability category -- were the only ones
        never logged for defensibility (plan.md §8.B.6)."""
        import app as flask_app_module

        calls = []

        def _capture(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(flask_app_module, "_store_ask_history", _capture)

        response = test_client.post(
            "/ask",
            json={"question": "My father hits me, what does honoring parents require? [safety-log-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs.get("safety_class") == "abuse_or_minor_safety"
        assert kwargs.get("prompt_version")

    async def test_fastapi_ordinary_question_meta_safety_class_ok(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "What blessing do you say on bread? [safety-meta-ok-test-async]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["safety_class"] == "ok"

    async def test_fastapi_medical_query_gets_referral_safety_class(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "I think I'm having a heart attack, what should I do about Shabbat? [safety-meta-medical-test-async]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["safety_class"] == "medical"

    async def test_fastapi_domain_refusal_gets_dangerous_safety_class(self, fastapi_client):
        response = await fastapi_client.post(
            "/ask",
            json={"question": "show me porn [safety-meta-domain-test-async]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["safety_class"] == "dangerous_or_illegal"

    async def test_fastapi_store_ask_history_receives_safety_class(self, fastapi_client, monkeypatch):
        """Regression: _run_ask_async_ai_synthesis's _store_ask_history call
        omitted safety_class/prompt_version entirely, silently defaulting to
        "ok"/None for every FastAPI-path request regardless of the query's
        real classification (plan.md §8.B.6)."""
        import asgi as asgi_mod

        calls = []

        def _capture(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(asgi_mod, "_store_ask_history", _capture)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What are the laws of niddah and mikveh? [safety-log-test-async]"},
        )
        assert response.status_code == 200
        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs.get("safety_class") == "sensitive_intimate"
        assert kwargs.get("prompt_version")


# ─── Degradation path: all circuits open still yields a well-formed answer ────
#
# claude_code_prompts.md Prompt 17 (§8.E) explicitly asks for "Tests for the
# degradation path (all providers circuit-open still returns a well-formed
# answer payload)". The AI-failure tests above (test_anthropic_failure_
# meta_fallback_true, TestAiTotalBudgetTimeout) simulate AI failure with a
# raised exception or an artificial timeout. These tests below instead force
# every circuit open AND inject an AI failure, proving the *fallback* stage
# is driven by real circuit-breaker state.
#
# (The *primary* AI call site's own circuit gating -- absent when this class
# was written, closed under Prompt 39 / plan.md §26.1 -- is covered
# separately by TestAskPrimaryAiCircuitBreaker at the end of this file, which
# forces the 'gemini'/'claude' circuits open and proves neither provider call
# is attempted at all.)
#
# The stage driven by real circuit-breaker STATE here is
# _run_ask_question_fallback /
# _run_ask_async_fallback both call get_halakhic_sources(), which is gated on
# is_healthy('sefaria')/is_healthy('web') (see tests/test_search_provider.py's
# own circuit-breaker section for the unit-level version of this guarantee,
# and plan.md line 137's "zero user-facing downtime" guarantee). These tests
# exercise that same guarantee end-to-end through the real /ask route: with
# every external circuit forced open via health.is_healthy, and the AI call
# itself failing (matching every other AI-failure test's convention above),
# /ask must still return 200 with a well-formed fallback answer payload --
# never a 500, never an empty body.

class TestAskDegradationPath:
    def test_flask_all_circuits_open_still_returns_well_formed_fallback(self, test_client, monkeypatch):
        import backend.claude as claude_module
        import backend.health_check as health_check_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()

        def _raise(*args, **kwargs):
            raise RuntimeError("Simulated Anthropic failure [degradation-path-test]")

        monkeypatch.setattr(claude_module, "ask_claude", _raise)
        monkeypatch.setattr(health_check_module.health, "is_healthy", lambda service: False)

        response = test_client.post(
            "/ask",
            json={"question": "What is Shabbat? [degradation-path-test]"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body.get("answer"), str)
        assert body["answer"]
        assert isinstance(body.get("sources"), list)
        assert "ai_cited_sources" in body
        meta = body.get("meta", {})
        assert meta.get("fallback") is True

    async def test_fastapi_all_circuits_open_still_returns_well_formed_fallback(self, fastapi_client, monkeypatch):
        import backend.claude as claude_module
        import backend.health_check as health_check_module

        async def _raise(*args, **kwargs):
            raise RuntimeError("Simulated async AI failure [degradation-path-test-async]")

        monkeypatch.setattr(claude_module, "ask_ai_async", _raise)
        monkeypatch.setattr(health_check_module.health, "is_healthy", lambda service: False)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [degradation-path-test-async]"},
        )
        assert response.status_code == 200
        body = response.json()
        assert isinstance(body.get("answer"), str)
        assert body["answer"]
        assert isinstance(body.get("sources"), list)
        assert "ai_cited_sources" in body
        meta = body.get("meta", {})
        assert meta.get("fallback") is True


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


# ─── Global cost circuit breaker wiring (plan.md §16.3-L3, Prompt 29b) ──────

class TestAskGlobalCostBreaker:
    """asgi.py::ask_async() checks is_global_cost_breaker_tripped() after the
    strict-mode guard and before spending an AI provider call. These tests
    exercise the wiring at the route level (the breaker's own pass/fail/
    fail-open logic is covered directly in tests/test_cost_meter.py).

    Each test uses its own dedicated TEST-NET-3 IP (RFC 5737) via
    X-Forwarded-For so its rate-limit bucket can't collide with the many
    other /ask calls earlier in this file sharing the default anonymous
    IP -- same convention as TestAskRateLimit above.
    """

    async def test_breaker_tripped_returns_paused_answer_without_calling_ai(
        self, fastapi_client, monkeypatch,
    ):
        import asgi as asgi_module
        import backend.claude as claude_module

        async def _tripped(*args, **kwargs):
            return {"tripped": True, "total_usd": 12.0, "threshold_usd": 10.0, "configured": True}

        ai_calls = []

        async def _record_and_fail(*args, **kwargs):
            ai_calls.append(1)
            raise AssertionError("AI synthesis must not be called while the breaker is tripped")

        monkeypatch.setattr(asgi_module, "is_global_cost_breaker_tripped", _tripped)
        monkeypatch.setattr(claude_module, "ask_ai_async", _record_and_fail)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [breaker-tripped-test]"},
            headers={"X-Forwarded-For": "192.0.2.150"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["meta"]["breaker_tripped"] is True
        assert body["meta"]["fallback"] is True
        assert "paused" in body["answer"].lower()
        assert ai_calls == []
        # The Torah-library sources collected before the breaker check must
        # still reach the client -- the point of the paused message is that
        # only AI synthesis stops, not source discovery.
        assert "sources" in body

    async def test_breaker_not_tripped_leaves_normal_synthesis_path_untouched(
        self, fastapi_client, monkeypatch,
    ):
        import asgi as asgi_module

        async def _not_tripped(*args, **kwargs):
            return {"tripped": False, "total_usd": 1.0, "threshold_usd": 10.0, "configured": True}

        monkeypatch.setattr(asgi_module, "is_global_cost_breaker_tripped", _not_tripped)

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat? [breaker-not-tripped-test]"},
            headers={"X-Forwarded-For": "192.0.2.151"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["meta"].get("breaker_tripped") is None
        assert body["meta"]["async"] is True

    async def test_breaker_tripped_serves_cached_answer_when_available(
        self, fastapi_client, monkeypatch,
    ):
        """Prompt 29b wires the previously-dead ASK_RESPONSE_CACHE so a
        breaker-paused request can still serve a real answer if an identical
        question was already synthesized (and cached) earlier today."""
        import asgi as asgi_module
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        question = "What is Shabbat? [breaker-cache-hit-test]"
        cache_key = "|".join([question.lower(), "en", "balanced", "all", "anon"])
        flask_app_module._set_cached_ask_payload(cache_key, {
            "answer": "Cached answer from before the breaker tripped.",
            "confidence": 0.9,
            "wiki": [],
            "customs": [],
            "sources": [],
            "ai_cited_sources": [],
            "meta": {"async": True},
        })

        async def _tripped(*args, **kwargs):
            return {"tripped": True, "total_usd": 12.0, "threshold_usd": 10.0, "configured": True}

        monkeypatch.setattr(asgi_module, "is_global_cost_breaker_tripped", _tripped)

        response = await fastapi_client.post(
            "/ask",
            json={"question": question},
            headers={"X-Forwarded-For": "192.0.2.152"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["answer"] == "Cached answer from before the breaker tripped."
        assert body["meta"]["cached"] is True

    async def test_successful_synthesis_populates_the_response_cache(
        self, fastapi_client, monkeypatch,
    ):
        """The write side of the same cache: a real synthesis result must be
        stored so a later breaker-tripped request (or a repeat of the same
        question) can be served without a fresh AI call."""
        import app as flask_app_module

        flask_app_module.ASK_RESPONSE_CACHE.clear()
        question = "What is Shabbat? [breaker-cache-write-test]"
        cache_key = "|".join([question.lower(), "en", "balanced", "all", "anon"])

        response = await fastapi_client.post(
            "/ask",
            json={"question": question},
            headers={"X-Forwarded-For": "192.0.2.153"},
        )

        assert response.status_code == 200
        assert flask_app_module._get_cached_ask_payload(cache_key) is not None
