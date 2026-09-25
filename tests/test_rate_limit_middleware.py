"""
Coverage for RateLimitMiddleware's fail-open/fail-closed posture when the
shared store is unreachable (plan.md §31.1 / §16.3-L2).

backend/rate_limit.py's _check() has one deliberately asymmetric branch: the
`llm` policy class (_POLICIES["llm"].fail_open = False) fails CLOSED on a
_StoreUnavailable -- an unmetered /ask during a store outage is a budget
hole, not a degraded feature -- while every other class (fail_open = True)
fails OPEN, so a Redis blip doesn't block ordinary reader/feedback traffic.
This was the one branch the original Prompt 29a spec asked to be tested and
never was (plan.md §31.1) -- confirmed uncovered by the full-suite coverage
report. tests/test_rate_limit.py covers the store abstraction's get/setex
surface; it does not stub incr() to raise, so it never exercises this
`except _StoreUnavailable` branch either.

No real Redis connection is used -- a stub store whose incr() always raises
_StoreUnavailable is the correct level of test for the _check()/
_StoreUnavailable contract itself.
"""

from __future__ import annotations

from backend import rate_limit


class _AlwaysUnavailableStore(rate_limit._RateLimitStore):
    """Stands in for backend.rate_limit._store; every incr() call raises
    _StoreUnavailable, simulating a Redis outage without a real connection."""

    async def incr(self, key: str, window_seconds: int) -> int:
        raise rate_limit._StoreUnavailable("simulated store outage")


class TestRateLimitFailClosedForLlmClass:
    async def test_ask_is_rejected_with_429_when_store_is_unavailable(self, fastapi_client, monkeypatch):
        """llm._Policy.fail_open is False -- a store outage must reject /ask
        rather than let an unmetered request through. Uses a fresh
        TEST-NET-2 IP (RFC 5737), unused elsewhere in the suite, though the
        stub store raises on the very first incr() so no per-IP bucket
        counting ever actually happens here."""
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            headers={"X-Forwarded-For": "198.51.100.201"},
        )

        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"

    async def test_ask_store_outage_reaches_capture_backend_error(self, fastapi_client, monkeypatch):
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())
        captured = []
        monkeypatch.setattr(
            rate_limit, "_capture_backend_error",
            lambda event, error, context=None: captured.append((event, context)),
        )

        response = await fastapi_client.post(
            "/ask",
            json={"question": "What is Shabbat?"},
            headers={"X-Forwarded-For": "198.51.100.201"},
        )

        assert response.status_code == 429
        assert len(captured) == 1
        event, context = captured[0]
        assert event == "rate_limit_store_unavailable"
        assert context["class"] == "llm"
        assert context["fail_open"] == "False"


class TestRateLimitFailOpenForOtherClasses:
    async def test_feedback_succeeds_when_store_is_unavailable(self, fastapi_client, monkeypatch):
        """The feedback class's fail_open is True -- a store outage must not
        block ordinary (non-llm) traffic. Uses a fresh TEST-NET-1 IP (RFC
        5737), unused elsewhere in the suite."""
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())

        response = await fastapi_client.post(
            "/api/feedback",
            json={"question": "What is Shabbat?", "verdict": "helpful"},
            headers={"X-Forwarded-For": "192.0.2.201"},
        )

        assert response.status_code == 200

    async def test_feedback_store_outage_reaches_capture_backend_error(self, fastapi_client, monkeypatch):
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())
        captured = []
        monkeypatch.setattr(
            rate_limit, "_capture_backend_error",
            lambda event, error, context=None: captured.append((event, context)),
        )

        response = await fastapi_client.post(
            "/api/feedback",
            json={"question": "What is Shabbat?", "verdict": "helpful"},
            headers={"X-Forwarded-For": "192.0.2.201"},
        )

        assert response.status_code == 200
        assert len(captured) == 1
        event, context = captured[0]
        assert event == "rate_limit_store_unavailable"
        assert context["class"] == "feedback"
        assert context["fail_open"] == "True"


class TestPrivacySensitiveRoutesGetAStricterPolicyThanCheap:
    """A 2026-09-02 audit flagged /api/user/delete-account, /api/user/
    data-export, and /api/webhooks/clerk as falling through _ROUTE_CLASSES'
    prefix table into the generic "cheap" bucket (120 req/min, IP-keyed only)
    -- the same policy read-only library lookups get, despite one being a
    destructive cascade-delete and another a full personal-data export.
    These confirm all three now classify into a materially stricter,
    explicitly-declared policy instead."""

    def test_delete_account_and_data_export_classify_stricter_than_cheap(self):
        cheap_limit = rate_limit._POLICIES["cheap"].max_requests

        for path in ("/api/user/delete-account", "/api/user/data-export"):
            route_class = rate_limit.classify_route(path)
            assert route_class != "cheap"
            assert rate_limit._POLICIES[route_class].max_requests < cheap_limit

    def test_clerk_webhook_classifies_stricter_than_cheap(self):
        route_class = rate_limit.classify_route("/api/webhooks/clerk")
        assert route_class != "cheap"
        assert rate_limit._POLICIES[route_class].max_requests < rate_limit._POLICIES["cheap"].max_requests

    def test_delete_account_and_data_export_are_keyed_by_clerk_user_id(self):
        """The two user-account routes must bucket an authenticated caller by
        Clerk user id, not by IP -- otherwise one NATed IP's abuse of either
        route caps every other user behind it."""
        for path in ("/api/user/delete-account", "/api/user/data-export"):
            route_class = rate_limit.classify_route(path)
            keyed = rate_limit._build_key(route_class, "203.0.113.90", "user-account-key-test")
            assert keyed == f"rl:{route_class}:user:user-account-key-test"

    def test_clerk_webhook_stays_ip_keyed_even_if_a_user_id_were_present(self):
        """/api/webhooks/clerk has no end-user identity to key on -- it's
        Clerk calling us -- so it must stay IP-keyed regardless of what
        _build_key is passed for user_id."""
        route_class = rate_limit.classify_route("/api/webhooks/clerk")
        keyed = rate_limit._build_key(route_class, "203.0.113.91", "should-be-ignored")
        assert keyed == f"rl:{route_class}:ip:203.0.113.91"

    async def test_delete_account_returns_429_once_its_stricter_ceiling_is_exceeded(self, fastapi_client):
        """End-to-end through RateLimitMiddleware: unauthenticated requests
        are rejected 401 by the route itself, but they still consume the
        rate-limit bucket (the middleware runs before the handler), so the
        account policy's low ceiling -- not "cheap"'s 120 -- governs when a
        429 appears."""
        policy = rate_limit._POLICIES[rate_limit.classify_route("/api/user/delete-account")]
        ip = "203.0.113.92"

        for _ in range(policy.max_requests):
            response = await fastapi_client.post(
                "/api/user/delete-account", headers={"X-Forwarded-For": ip},
            )
            assert response.status_code == 401

        over_limit = await fastapi_client.post(
            "/api/user/delete-account", headers={"X-Forwarded-For": ip},
        )
        assert over_limit.status_code == 429

    async def test_clerk_webhook_returns_429_once_its_stricter_ceiling_is_exceeded(self, fastapi_client):
        """Same shape as above for the IP-keyed webhook class: an unsigned
        POST is rejected downstream (503 -- CLERK_WEBHOOK_SIGNING_SECRET is
        blanked out for the whole suite, tests/conftest.py), but each
        attempt still counts against the webhook policy's ceiling."""
        policy = rate_limit._POLICIES[rate_limit.classify_route("/api/webhooks/clerk")]
        ip = "203.0.113.93"

        for _ in range(policy.max_requests):
            response = await fastapi_client.post(
                "/api/webhooks/clerk", headers={"X-Forwarded-For": ip},
            )
            assert response.status_code == 503

        over_limit = await fastapi_client.post(
            "/api/webhooks/clerk", headers={"X-Forwarded-For": ip},
        )
        assert over_limit.status_code == 429


class TestConversationAskClassifiesAsLlm:
    """/api/conversations/<id>/ask runs the same AI synthesis as /ask, but
    its model-calling segment sits after a path parameter, so the prefix
    table alone sent it to "cheap" (120/min, fail-open) -- an unmetered
    model route. _ROUTE_PATTERNS carves it out ahead of the prefix table;
    the conversation CRUD siblings stay "cheap" by explicit choice."""

    def test_conversation_ask_is_llm(self):
        for path in (
            "/api/conversations/3f2b1c9e-0000-4000-8000-000000000001/ask",
            "/api/conversations/abc/ask/",
        ):
            assert rate_limit.classify_route(path) == "llm"

    def test_conversation_crud_routes_stay_cheap(self):
        for path in (
            "/api/conversations",
            "/api/conversations/abc",
            "/api/conversations/abc/messages",
            # Not the ask route: extra segment after /ask, or /ask as the id.
            "/api/conversations/abc/ask/extra",
            "/api/conversations/ask",
        ):
            assert rate_limit.classify_route(path) == "cheap"

    def test_conversation_ask_is_keyed_by_clerk_user_id(self):
        route_class = rate_limit.classify_route("/api/conversations/abc/ask")
        keyed = rate_limit._build_key(route_class, "203.0.113.94", "user-conv-key-test")
        assert keyed == "rl:llm:user:user-conv-key-test"

    async def test_conversation_ask_fails_closed_when_store_is_unavailable(self, fastapi_client, monkeypatch):
        """End-to-end through RateLimitMiddleware: a store outage rejects the
        request with 429 before it can reach the (unauthenticated -> 401)
        Flask handler -- same fail-closed posture /ask gets."""
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())
        monkeypatch.setattr(rate_limit, "_capture_backend_error", lambda *a, **k: None)

        response = await fastapi_client.post(
            "/api/conversations/abc/ask",
            json={"question": "What is Shabbat?"},
            headers={"X-Forwarded-For": "198.51.100.211"},
        )

        assert response.status_code == 429
        assert response.json()["code"] == "rate_limited"

    async def test_conversation_list_fails_open_when_store_is_unavailable(self, fastapi_client, monkeypatch):
        """Contrast case: the CRUD routes are "cheap" (fail-open), so the same
        outage lets the request through to the route's own 401."""
        monkeypatch.setattr(rate_limit, "_store", _AlwaysUnavailableStore())
        monkeypatch.setattr(rate_limit, "_capture_backend_error", lambda *a, **k: None)

        response = await fastapi_client.get(
            "/api/conversations", headers={"X-Forwarded-For": "198.51.100.212"},
        )

        assert response.status_code == 401


class _DownRedisClient:
    """A redis.asyncio client double for an unreachable server; counts every
    command so a test can prove the circuit breaker kept requests off it."""

    def __init__(self):
        self.calls = 0

    async def incr(self, key):
        self.calls += 1
        raise ConnectionError("Timeout connecting to server")


def _redis_store_that_is_down():
    store = rate_limit._RateLimitStore.__new__(rate_limit._RedisStore)
    store._client = _DownRedisClient()
    store._breaker = rate_limit._CircuitBreaker()
    return store


class TestStoreCircuitBreakerThroughTheMiddleware:
    """One Redis failure opens the store's breaker: later requests in the
    cooldown get their class's posture immediately (no 2s connect timeout
    each), and the outage is captured once, not once per request."""

    async def test_outage_touches_redis_and_captures_once_then_short_circuits(
        self, fastapi_client, monkeypatch
    ):
        store = _redis_store_that_is_down()
        monkeypatch.setattr(rate_limit, "_store", store)
        captured = []
        monkeypatch.setattr(
            rate_limit, "_capture_backend_error",
            lambda event, error, context=None: captured.append(context["class"]),
        )

        feedback = [
            await fastapi_client.post(
                "/api/feedback",
                json={"question": "What is Shabbat?", "verdict": "helpful"},
                headers={"X-Forwarded-For": "192.0.2.221"},
            )
            for _ in range(3)
        ]
        asks = [
            await fastapi_client.post(
                "/ask",
                json={"question": "What is Shabbat?"},
                headers={"X-Forwarded-For": "198.51.100.221"},
            )
            for _ in range(2)
        ]

        assert [r.status_code for r in feedback] == [200, 200, 200]  # fail-open
        assert [r.status_code for r in asks] == [429, 429]  # llm stays fail-closed
        assert store._client.calls == 1
        assert captured == ["feedback"]
