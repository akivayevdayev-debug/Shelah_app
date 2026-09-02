"""
Tests for backend/logging_setup.py — structured JSON logging, request-id
context binding, and the _capture_backend_error structured error logger.
No prior dedicated test file existed for this module.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

import backend.logging_setup as logging_setup


@pytest.fixture(autouse=True)
def _reset_request_id():
    logging_setup._request_id_var.set("")
    yield
    logging_setup._request_id_var.set("")


class TestJSONFormatter:
    def _make_record(self, msg="hello", **extra):
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname=__file__,
            lineno=42, msg=msg, args=(), exc_info=None,
        )
        for k, v in extra.items():
            setattr(record, k, v)
        return record

    def test_basic_fields_present(self):
        formatter = logging_setup._JSONFormatter()
        record = self._make_record("hello world")
        output = json.loads(formatter.format(record))
        assert output["message"] == "hello world"
        assert output["level"] == "INFO"
        assert output["logger"] == "test.logger"
        assert "timestamp" in output

    def test_request_id_included_when_set(self):
        logging_setup.bind_request_id("abc123")
        formatter = logging_setup._JSONFormatter()
        record = self._make_record()
        output = json.loads(formatter.format(record))
        assert output["request_id"] == "abc123"

    def test_request_id_absent_when_not_set(self):
        formatter = logging_setup._JSONFormatter()
        record = self._make_record()
        output = json.loads(formatter.format(record))
        assert "request_id" not in output

    def test_exception_info_included(self):
        formatter = logging_setup._JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test.logger", level=logging.ERROR, pathname=__file__,
                lineno=1, msg="failed", args=(), exc_info=sys.exc_info(),
            )
        output = json.loads(formatter.format(record))
        assert "boom" in output["exception"]

    def test_extra_fields_included(self):
        formatter = logging_setup._JSONFormatter()
        record = self._make_record(user_id="user-1", custom_field=42)
        output = json.loads(formatter.format(record))
        assert output["user_id"] == "user-1"
        assert output["custom_field"] == 42

    def test_non_serializable_extra_falls_back_gracefully(self):
        formatter = logging_setup._JSONFormatter()
        record = self._make_record(weird=object())
        # default=str in json.dumps handles this without raising.
        output_str = formatter.format(record)
        output = json.loads(output_str)
        assert "message" in output


class TestSetupLogging:
    def test_returns_root_logger(self):
        root = logging_setup.setup_logging(level="DEBUG")
        assert root is logging.getLogger()
        assert root.level == logging.DEBUG

    def test_does_not_add_duplicate_handlers_on_repeat_calls(self):
        logging_setup.setup_logging(level="INFO")
        handler_count_before = len([
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, logging_setup._JSONFormatter)
        ])
        logging_setup.setup_logging(level="INFO")
        handler_count_after = len([
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, logging_setup._JSONFormatter)
        ])
        assert handler_count_before == handler_count_after == 1

    def test_defaults_to_env_log_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        root = logging_setup.setup_logging()
        assert root.level == logging.WARNING

    def test_invalid_level_falls_back_to_info(self):
        root = logging_setup.setup_logging(level="NOT_A_REAL_LEVEL")
        assert root.level == logging.INFO

    def test_suppresses_noisy_third_party_loggers_at_info(self):
        logging_setup.setup_logging(level="INFO")
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("anthropic").level == logging.WARNING


class TestRequestIdBinding:
    def test_bind_with_explicit_id(self):
        result = logging_setup.bind_request_id("my-request-id")
        assert result == "my-request-id"
        assert logging_setup.get_request_id() == "my-request-id"

    def test_bind_without_id_generates_one(self):
        result = logging_setup.bind_request_id()
        assert result
        assert len(result) == 12
        assert logging_setup.get_request_id() == result

    def test_bind_with_empty_string_generates_one(self):
        result = logging_setup.bind_request_id("")
        assert result
        assert logging_setup.get_request_id() == result

    def test_get_request_id_empty_when_unset(self):
        assert logging_setup.get_request_id() == ""


class TestGetLogger:
    def test_returns_named_logger(self):
        logger = logging_setup.get_logger("my.module")
        assert logger.name == "my.module"


class TestCaptureBackendError:
    def test_logs_error_via_flask_app_logger(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append((msg, k)),
        )
        logging_setup._capture_backend_error("test_event", ValueError("boom"), {"key": "value"})
        assert len(logged) == 1
        assert "test_event" in logged[0][0] or "OBS_EVENT" in logged[0][0]

    def test_none_error_produces_empty_message(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append(msg),
        )
        logging_setup._capture_backend_error("test_event", None)
        assert len(logged) == 1

    def test_non_dict_context_normalized_to_empty_dict(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        # Should not raise despite a non-dict context.
        logging_setup._capture_backend_error("test_event", RuntimeError("x"), context="not a dict")

    def test_webhook_posted_when_configured(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setenv("ERROR_LOG_WEBHOOK_URL", "https://example.com/webhook")

        posted = []

        def fake_post(url, json=None, timeout=None):
            posted.append((url, json))

        import requests
        monkeypatch.setattr(requests, "post", fake_post)
        logging_setup._capture_backend_error("test_event", ValueError("boom"))
        assert len(posted) == 1
        assert posted[0][0] == "https://example.com/webhook"

    def test_webhook_failure_is_swallowed(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setenv("ERROR_LOG_WEBHOOK_URL", "https://example.com/webhook")

        def fake_post(url, json=None, timeout=None):
            raise ConnectionError("webhook down")

        import requests
        monkeypatch.setattr(requests, "post", fake_post)
        # Should not raise despite webhook failure.
        logging_setup._capture_backend_error("test_event", ValueError("boom"))

    def test_discord_webhook_gets_reshaped_to_embeds(self, monkeypatch):
        """Discord's webhook API rejects arbitrary JSON (requires content/embeds)
        and the caller never checks the response status — posting the flat
        payload as-is silently fails every time. Detect Discord URLs and shape
        an embed instead."""
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setenv(
            "ERROR_LOG_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc")

        posted = []
        import requests
        monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: posted.append(json))
        logging_setup._capture_backend_error(
            "clerk_auth_verify_failed", ValueError("Invalid audience"),
            {"path": "/api/user/preferences"})

        assert len(posted) == 1
        body = posted[0]
        assert "content" not in body
        assert len(body["embeds"]) == 1
        embed = body["embeds"][0]
        assert embed["title"] == "clerk_auth_verify_failed"
        assert embed["description"] == "Invalid audience"
        assert any(f["name"] == "context" for f in embed["fields"])

    def test_no_webhook_configured_skips_post(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.delenv("ERROR_LOG_WEBHOOK_URL", raising=False)

        posted = []
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: posted.append(1))
        logging_setup._capture_backend_error("test_event", ValueError("boom"))
        assert posted == []

    def test_sentry_capture_called_when_enabled(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)

        captured = []
        fake_sentry = type("FakeSentry", (), {
            "capture_exception": staticmethod(lambda exc, contexts=None: captured.append((exc, contexts))),
        })
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake_sentry)
        error = ValueError("boom")
        logging_setup._capture_backend_error("test_event", error, {"k": "v"})
        assert len(captured) == 1
        assert captured[0][0] is error

    def test_sentry_exception_is_swallowed(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)

        def _raise(exc, contexts=None):
            raise RuntimeError("sentry down")

        fake_sentry = type("FakeSentry", (), {"capture_exception": staticmethod(_raise)})
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake_sentry)
        # Should not raise despite Sentry failure.
        logging_setup._capture_backend_error("test_event", ValueError("boom"))

    def test_sentry_capture_includes_bound_request_id(self, monkeypatch):
        """plan.md §8.E.1: _capture_backend_error must route request-id
        context to Sentry, so an issue can be correlated back to its
        request's log lines."""
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)
        logging_setup.bind_request_id("sentry-correlated-id")

        captured = []
        fake_sentry = type("FakeSentry", (), {
            "capture_exception": staticmethod(
                lambda exc, contexts=None: captured.append(contexts)),
        })
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake_sentry)
        logging_setup._capture_backend_error("test_event", ValueError("boom"), {"k": "v"})

        assert len(captured) == 1
        assert captured[0]["backend_error"]["request_id"] == "sentry-correlated-id"
        # Original context keys must survive alongside the added request_id.
        assert captured[0]["backend_error"]["k"] == "v"

    def test_json_payload_includes_request_id_when_bound(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append(a),
        )
        logging_setup.bind_request_id("payload-request-id")
        logging_setup._capture_backend_error("test_event", ValueError("boom"))

        assert len(logged) == 1
        payload = json.loads(logged[0][0])
        assert payload["request_id"] == "payload-request-id"

    def test_json_payload_request_id_empty_when_unbound(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append(a),
        )
        logging_setup._capture_backend_error("test_event", ValueError("boom"))

        payload = json.loads(logged[0][0])
        assert payload["request_id"] == ""


class TestScrubErrorContext:
    """P1 audit finding: halachic question/answer text must never reach
    structured logs, the error webhook, or Sentry unredacted."""

    @pytest.mark.parametrize("key", [
        "question", "Question", "user_question", "answer", "ai_answer",
        "ruling", "summary", "practical_step", "body", "text",
    ])
    def test_sensitive_keys_are_filtered(self, key):
        scrubbed = logging_setup._scrub_error_context({key: "sensitive halachic content"})
        assert scrubbed[key] == "[Filtered]"

    def test_non_sensitive_keys_survive_untouched(self):
        context = {"mode": "strict", "user_id": "user_123", "call_count": 3}
        assert logging_setup._scrub_error_context(context) == context

    def test_long_non_sensitive_string_is_truncated(self):
        long_value = "x" * 500
        scrubbed = logging_setup._scrub_error_context({"trace_id": long_value})
        assert len(scrubbed["trace_id"]) < len(long_value)
        assert scrubbed["trace_id"].endswith("[truncated]")

    def test_short_non_sensitive_string_is_not_truncated(self):
        scrubbed = logging_setup._scrub_error_context({"mode": "strict"})
        assert scrubbed["mode"] == "strict"

    def test_non_string_values_pass_through(self):
        scrubbed = logging_setup._scrub_error_context({"call_count": 3, "exceeded": True})
        assert scrubbed == {"call_count": 3, "exceeded": True}

    def test_capture_backend_error_scrubs_question_from_json_log(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append(a),
        )
        logging_setup._capture_backend_error(
            "ask_ai_synthesis_failed", ValueError("boom"),
            {"question": "Is it permitted to drive on Shabbat for a medical emergency?"},
        )
        payload = json.loads(logged[0][0])
        assert payload["context"]["question"] == "[Filtered]"

    def test_capture_backend_error_scrubs_question_from_webhook(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setenv("ERROR_LOG_WEBHOOK_URL", "https://example.com/webhook")

        posted = []
        import requests
        monkeypatch.setattr(requests, "post", lambda url, json=None, timeout=None: posted.append(json))
        logging_setup._capture_backend_error(
            "ask_ai_synthesis_failed", ValueError("boom"),
            {"question": "a very sensitive question"},
        )
        assert posted[0]["context"]["question"] == "[Filtered]"

    def test_capture_backend_error_scrubs_question_from_sentry_context(self, monkeypatch):
        import app as flask_app_module
        monkeypatch.setattr(flask_app_module.app.logger, "error", lambda *a, **k: None)
        monkeypatch.setattr(logging_setup, "_sentry_enabled", True)

        captured = []
        fake_sentry = type("FakeSentry", (), {
            "capture_exception": staticmethod(lambda exc, contexts=None: captured.append(contexts)),
        })
        monkeypatch.setattr(logging_setup, "sentry_sdk", fake_sentry)
        logging_setup._capture_backend_error(
            "ask_ai_synthesis_failed", ValueError("boom"),
            {"question": "a very sensitive question", "mode": "strict"},
        )
        assert captured[0]["backend_error"]["question"] == "[Filtered]"
        assert captured[0]["backend_error"]["mode"] == "strict"

    def test_error_message_itself_is_truncated(self, monkeypatch):
        import app as flask_app_module
        logged = []
        monkeypatch.setattr(
            flask_app_module.app.logger, "error",
            lambda msg, *a, **k: logged.append(a),
        )
        logging_setup._capture_backend_error("test_event", ValueError("x" * 500))
        payload = json.loads(logged[0][0])
        assert len(payload["message"]) < 500
        assert payload["message"].endswith("[truncated]")


class TestTracesSampler:
    """plan.md §17.3 deviation 7: traces_sampler replaces the flat
    traces_sample_rate=0.1 — /ask and fan-out routes sample meaningfully,
    health/statics sample at zero."""

    def test_ask_route_sampled_meaningfully(self):
        rate = logging_setup._traces_sampler({"asgi_scope": {"path": "/ask"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_ASK
        assert rate > 0

    def test_health_route_sampled_at_zero(self):
        assert logging_setup._traces_sampler({"asgi_scope": {"path": "/api/health"}}) == 0.0

    def test_stack_health_route_sampled_at_zero(self):
        assert logging_setup._traces_sampler({"asgi_scope": {"path": "/api/stack/health"}}) == 0.0

    def test_async_health_route_sampled_at_zero(self):
        assert logging_setup._traces_sampler({"asgi_scope": {"path": "/api/async/health"}}) == 0.0

    def test_static_asset_sampled_at_zero(self):
        assert logging_setup._traces_sampler({"asgi_scope": {"path": "/static/js/main.js"}}) == 0.0

    def test_fanout_route_sampled_moderately(self):
        rate = logging_setup._traces_sampler({"asgi_scope": {"path": "/api/library/search"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_FANOUT

    def test_siddur_fanout_route_sampled_moderately(self):
        rate = logging_setup._traces_sampler({"asgi_scope": {"path": "/api/siddur/full/weekday"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_FANOUT

    def test_export_chapter_fanout_route_sampled_moderately(self):
        rate = logging_setup._traces_sampler({"asgi_scope": {"path": "/api/export/chapter"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_FANOUT

    def test_unclassified_route_gets_light_default(self):
        rate = logging_setup._traces_sampler({"asgi_scope": {"path": "/api/bookmarks/list"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_DEFAULT

    def test_falls_back_to_wsgi_environ_when_no_asgi_scope(self):
        """python3 app.py (bare Flask, no ASGI layer — plan.md §16.1 D1)
        only populates wsgi_environ, never asgi_scope."""
        rate = logging_setup._traces_sampler({"wsgi_environ": {"PATH_INFO": "/ask"}})
        assert rate == logging_setup._TRACE_SAMPLE_RATE_ASK

    def test_missing_path_returns_zero(self):
        assert logging_setup._traces_sampler({}) == 0.0


class TestSentryInitKwargs:
    """plan.md §17.3 deviations 1 and 2: verify the exact arguments that
    would be passed to sentry_sdk.init(), independent of whether SENTRY_DSN
    happens to be set in this test run."""

    def test_send_default_pii_is_explicitly_false(self):
        kwargs = logging_setup._build_sentry_init_kwargs("https://key@o1.ingest.us.sentry.io/1")
        assert kwargs["send_default_pii"] is False

    def test_uses_traces_sampler_not_a_flat_rate(self):
        kwargs = logging_setup._build_sentry_init_kwargs("https://key@o1.ingest.us.sentry.io/1")
        assert kwargs["traces_sampler"] is logging_setup._traces_sampler
        assert "traces_sample_rate" not in kwargs

    def test_dsn_is_passed_through_verbatim_never_hardcoded(self):
        dsn = "https://env-supplied-key@o1.ingest.us.sentry.io/1"
        kwargs = logging_setup._build_sentry_init_kwargs(dsn)
        assert kwargs["dsn"] == dsn

    def test_environment_and_release_read_from_vercel_env_vars(self, monkeypatch):
        monkeypatch.setenv("VERCEL_ENV", "preview")
        monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "abc123")
        kwargs = logging_setup._build_sentry_init_kwargs("https://key@o1.ingest.us.sentry.io/1")
        assert kwargs["environment"] == "preview"
        assert kwargs["release"] == "abc123"

    def test_environment_defaults_to_development(self, monkeypatch):
        monkeypatch.delenv("VERCEL_ENV", raising=False)
        kwargs = logging_setup._build_sentry_init_kwargs("https://key@o1.ingest.us.sentry.io/1")
        assert kwargs["environment"] == "development"


class TestSubmitWithContext:
    """submit_with_context() propagates contextvars (request_id) into
    ThreadPoolExecutor workers, which don't inherit them by default."""

    def test_propagates_request_id_into_worker_thread(self):
        logging_setup.bind_request_id("thread-context-id")
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            future = logging_setup.submit_with_context(
                pool, logging_setup.get_request_id)
            assert future.result(timeout=5) == "thread-context-id"
        finally:
            pool.shutdown(wait=True)

    def test_plain_submit_does_not_propagate(self):
        """Documents the gap submit_with_context exists to close."""
        logging_setup.bind_request_id("thread-context-id")
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            future = pool.submit(logging_setup.get_request_id)
            assert future.result(timeout=5) == ""
        finally:
            pool.shutdown(wait=True)

    def test_passes_args_and_kwargs_through(self):
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            future = logging_setup.submit_with_context(
                pool, lambda a, b, c=0: a + b + c, 1, 2, c=3)
            assert future.result(timeout=5) == 6
        finally:
            pool.shutdown(wait=True)

    def test_propagates_exceptions_from_the_submitted_function(self):
        def _boom():
            raise ValueError("worker failure")

        pool = ThreadPoolExecutor(max_workers=2)
        try:
            future = logging_setup.submit_with_context(pool, _boom)
            with pytest.raises(ValueError, match="worker failure"):
                future.result(timeout=5)
        finally:
            pool.shutdown(wait=True)
