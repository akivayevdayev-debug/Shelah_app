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


class _RecordingFeedbackClient:
    """Stand-in for the Supabase client: records what is inserted, where."""

    def __init__(self, *, execute_error=None):
        self.tables: list[str] = []
        self.inserted: list[dict] = []
        self._execute_error = execute_error

    def table(self, name):
        self.tables.append(name)
        return self

    def insert(self, record):
        self.inserted.append(record)
        return self

    def execute(self):
        if self._execute_error is not None:
            raise self._execute_error
        return None


class TestFeedbackPersistence:
    def _post(self, test_client, **overrides):
        payload = {"question": "What is Shabbat?", "verdict": "helpful", **overrides}
        return test_client.post(
            "/api/feedback", json=payload, content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.20"},
        )

    def test_the_record_stores_a_hash_not_the_question_and_applies_defaults(self, test_client, monkeypatch):
        import hashlib

        import backend.routes_feedback as routes_feedback_module

        client = _RecordingFeedbackClient()
        monkeypatch.setattr(routes_feedback_module, "_get_supabase_client", lambda: client)

        response = self._post(test_client)

        assert response.status_code == 200
        assert client.tables == [routes_feedback_module.SUPABASE_ANSWER_FEEDBACK_TABLE]
        [record] = client.inserted
        assert record["question_hash"] == hashlib.sha256(b"What is Shabbat?").hexdigest()
        assert "question" not in record
        assert record["verdict"] == "helpful"
        assert record["comment"] == ""
        assert record["mode"] == "balanced"
        assert record["language"] == "en"
        assert record["fallback"] is False
        assert record["safety_class"] == "ok"

    def test_supplied_metadata_is_stored(self, test_client, monkeypatch):
        import backend.routes_feedback as routes_feedback_module

        client = _RecordingFeedbackClient()
        monkeypatch.setattr(routes_feedback_module, "_get_supabase_client", lambda: client)

        self._post(test_client, verdict="not_helpful", mode="strict", language="he",
                   fallback=True, safety_class="sensitive", comment="Missed a case.")

        [record] = client.inserted
        assert (record["verdict"], record["mode"], record["language"]) == ("not_helpful", "strict", "he")
        assert record["fallback"] is True
        assert record["safety_class"] == "sensitive"
        assert record["comment"] == "Missed a case."

    def test_an_unconfigured_database_is_a_503(self, test_client, monkeypatch):
        import backend.routes_feedback as routes_feedback_module

        monkeypatch.setattr(routes_feedback_module, "_get_supabase_client", lambda: None)

        response = self._post(test_client)

        assert response.status_code == 503
        assert response.get_json() == {"error": "Supabase not configured"}

    def test_a_failed_insert_is_a_500_and_is_reported_without_the_question(self, test_client, monkeypatch):
        import backend.routes_feedback as routes_feedback_module

        boom = RuntimeError("insert refused")
        monkeypatch.setattr(
            routes_feedback_module, "_get_supabase_client",
            lambda: _RecordingFeedbackClient(execute_error=boom),
        )
        captured: list[tuple] = []
        monkeypatch.setattr(
            routes_feedback_module, "_capture_backend_error",
            lambda *args: captured.append(args),
        )

        response = self._post(test_client, verdict="not_helpful")

        assert response.status_code == 500
        assert response.get_json() == {"error": "Could not save feedback"}
        assert captured == [("answer_feedback_persist_failed", boom, {"verdict": "not_helpful"})]


class TestFeedbackMetadataFieldCaps:
    """AI_SECURITY_REVIEW L2: mode/language/safety_class were stored with no
    length cap; they are now capped (and defaulted) the way `comment` is."""

    def _post_and_get_record(self, test_client, monkeypatch, **overrides):
        import backend.routes_feedback as routes_feedback_module

        client = _RecordingFeedbackClient()
        monkeypatch.setattr(routes_feedback_module, "_get_supabase_client", lambda: client)
        payload = {"question": "What is Shabbat?", "verdict": "helpful", **overrides}
        response = test_client.post(
            "/api/feedback", json=payload, content_type="application/json",
            environ_base={"REMOTE_ADDR": "10.0.1.21"},
        )
        assert response.status_code == 200
        [record] = client.inserted
        return record

    def test_oversized_metadata_fields_are_truncated_to_their_caps(self, test_client, monkeypatch):
        import backend.routes_feedback as routes_feedback_module

        record = self._post_and_get_record(
            test_client, monkeypatch,
            mode="m" * 5000, language="l" * 5000, safety_class="s" * 5000,
        )

        assert record["mode"] == "m" * routes_feedback_module._MAX_MODE_CHARS
        assert record["language"] == "l" * routes_feedback_module._MAX_LANGUAGE_CHARS
        assert record["safety_class"] == "s" * routes_feedback_module._MAX_SAFETY_CLASS_CHARS

    def test_the_comment_cap_is_unchanged(self, test_client, monkeypatch):
        import backend.routes_feedback as routes_feedback_module

        record = self._post_and_get_record(test_client, monkeypatch, comment="c" * 5000)

        assert record["comment"] == "c" * routes_feedback_module._MAX_COMMENT_CHARS

    def test_every_legitimate_value_fits_within_its_cap(self, test_client, monkeypatch):
        """The caps must never clip a real value: the longest safety class
        the classifier can emit, and the values the UI actually sends."""
        from backend.claude import SAFETY_REFERRAL_CLASSES

        longest_safety_class = max(SAFETY_REFERRAL_CLASSES, key=len)
        record = self._post_and_get_record(
            test_client, monkeypatch,
            mode="practical", language="he", safety_class=longest_safety_class,
        )

        assert record["mode"] == "practical"
        assert record["language"] == "he"
        assert record["safety_class"] == longest_safety_class

    def test_missing_blank_and_whitespace_only_values_keep_the_defaults(self, test_client, monkeypatch):
        record = self._post_and_get_record(
            test_client, monkeypatch, mode="   ", language="", safety_class=None,
        )

        assert (record["mode"], record["language"], record["safety_class"]) == ("balanced", "en", "ok")

    def test_control_characters_and_markup_are_stripped_from_metadata_fields(self, test_client, monkeypatch):
        record = self._post_and_get_record(
            test_client, monkeypatch,
            mode="stri​ct", language="<b>he</b>", safety_class="o\x00k",
        )

        assert record["mode"] == "strict"
        assert "<" not in record["language"]
        assert ">" not in record["language"]
        assert record["safety_class"] == "ok"
