"""
Tests for backend/routes_feedback.py (plan.md §12.4).

Covers the happy path, validation errors, oversize/HTML-injection comment
sanitization, and the rate limit. The non-rate-limit tests each use a
distinct REMOTE_ADDR out of old habit, but it's no longer load-bearing:
Flask itself carries no rate limiter (plan.md §16.8.1 -- Flask-Limiter
removed), only backend.rate_limit.RateLimitMiddleware does, and that only
sees traffic routed through the ASGI layer (fastapi_client), not the bare
Flask test_client these tests use.
"""


class TestFeedbackHappyPath:
    def test_helpful_verdict_returns_200(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={"question": "What is Shabbat?", "verdict": "helpful"},
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.1"},
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True

    def test_not_helpful_verdict_with_comment_returns_200(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={
                "question": "What is Shabbat?",
                "verdict": "not_helpful",
                "comment": "Missed the eruv tavshilin case.",
                "mode": "strict",
                "language": "he",
                "fallback": True,
                "safety_class": "ok",
            },
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.2"},
        )
        assert response.status_code == 200


class TestFeedbackValidation:
    def test_missing_verdict_returns_400(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={"question": "What is Shabbat?"},
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.3"},
        )
        assert response.status_code == 400

    def test_invalid_verdict_returns_400(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={"question": "What is Shabbat?", "verdict": "meh"},
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.4"},
        )
        assert response.status_code == 400

    def test_missing_question_returns_400(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={"verdict": "helpful"},
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.5"},
        )
        assert response.status_code == 400


class TestFeedbackCommentSanitization:
    def test_oversize_comment_is_truncated(self, test_client):
        long_comment = "a" * 5000
        response = test_client.post(
            "/api/feedback",
            json={
                "question": "What is Shabbat?",
                "verdict": "not_helpful",
                "comment": long_comment,
            },
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.6"},
        )
        assert response.status_code == 200

    def test_html_injection_in_comment_is_stripped(self, test_client):
        response = test_client.post(
            "/api/feedback",
            json={
                "question": "What is Shabbat?",
                "verdict": "not_helpful",
                "comment": "<script>alert(1)</script>",
            },
            content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.7"},
        )
        assert response.status_code == 200


class TestFeedbackRateLimit:
    async def test_requests_beyond_the_configured_limit_are_rate_limited(self, fastapi_client):
        """
        /api/feedback is served entirely by the Flask app mounted under
        asgi.fastapi_app -- Flask itself enforces no rate limit of its own
        (plan.md §16.8.1), so this must drive traffic through the ASGI layer
        (fastapi_client) where backend.rate_limit.RateLimitMiddleware
        actually intercepts requests before they reach Flask. Uses a
        dedicated TEST-NET-3 IP (RFC 5737, distinct from the ones
        tests/test_ask.py uses) via X-Forwarded-For so this test's bucket
        can't collide with any other test's in the shared in-process store.
        """
        import backend.rate_limit as rate_limit_mod

        limit_count = rate_limit_mod._POLICIES["feedback"].max_requests
        payload = {"question": "What is Shabbat?", "verdict": "helpful"}
        headers = {"X-Forwarded-For": "192.0.2.150"}

        for _ in range(limit_count):
            response = await fastapi_client.post("/api/feedback", json=payload, headers=headers)
            assert response.status_code == 200

        over_limit_response = await fastapi_client.post("/api/feedback", json=payload, headers=headers)
        assert over_limit_response.status_code == 429


class TestFeedbackDigestRoute:
    def test_requires_auth(self, test_client):
        response = test_client.get("/api/devtools/feedback-digest")
        assert response.status_code in (401, 403)
