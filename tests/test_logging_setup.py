"""
Tests for backend/logging_setup.py — structured JSON logging, request-id
context binding, and the _capture_backend_error structured error logger.
No prior dedicated test file existed for this module.
"""

from __future__ import annotations

import json
import logging

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
