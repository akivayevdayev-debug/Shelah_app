"""
Structured JSON logging configuration for Sh'elah.

Usage:
    from backend.logging_setup import setup_logging
    setup_logging()          # call once at app startup

All loggers in the application will automatically inherit the JSON formatter
through the root logger. Each log record is emitted as a single-line JSON
object — easy to ingest by Vercel log drains, Datadog, Papertrail, etc.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

_request_id_var: ContextVar[str] = ContextVar('request_id', default='')


class _JSONFormatter(logging.Formatter):
    """Emit each log record as a compact single-line JSON object."""

    # Fields that are always included.
    _BASE_KEYS = {"timestamp", "level", "logger",
                  "message", "module", "function", "line"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Attach request_id when present.
        req_id = _request_id_var.get()
        if req_id:
            payload["request_id"] = req_id

        # Attach exception traceback when present.
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text

        # Attach any extra fields the caller passed via LogRecord.__dict__.
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in self._BASE_KEYS:
                continue
            if key in {
                "args", "asctime", "created", "exc_info", "exc_text",
                "filename", "funcName", "levelname", "levelno", "lineno",
                "message", "module", "msecs", "msg", "name", "pathname",
                "process", "processName", "relativeCreated", "stack_info",
                "taskName", "thread", "threadName",
            }:
                continue
            payload[key] = value

        try:
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            payload["message"] = str(record.getMessage())
            return json.dumps(payload, ensure_ascii=True, default=str)


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure root logger to emit structured JSON.

    Args:
        level: Log level string (DEBUG/INFO/WARNING/ERROR). Defaults to the
               ``LOG_LEVEL`` environment variable, falling back to INFO.

    Returns:
        The root logger (already configured).
    """
    resolved_level = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    numeric_level = getattr(logging, resolved_level, logging.INFO)

    root = logging.getLogger()

    # Avoid double-adding handlers when called multiple times (e.g. in tests).
    if not any(isinstance(h, logging.StreamHandler) and isinstance(h.formatter, _JSONFormatter)
               for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        root.addHandler(handler)

    root.setLevel(numeric_level)

    # Suppress chatty third-party loggers when root is INFO or below.
    if numeric_level <= logging.INFO:
        for noisy in ("httpx", "hpack", "anthropic"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def bind_request_id(request_id: str | None = None) -> str:
    """Set request_id in the current context and return it.

    If *request_id* is None or empty, a 12-hex-char UUID fragment is generated.
    """
    rid = (request_id or "").strip() or uuid.uuid4().hex[:12]
    _request_id_var.set(rid)
    return rid


def get_request_id() -> str:
    """Return the current context's request_id (empty string if not set)."""
    return _request_id_var.get()


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits the JSON formatter from the root."""
    return logging.getLogger(name)


# ── Structured backend error logger ──────────────────────────────────────────


def _capture_backend_error(event_name, error, context=None):
    """Sentry-style structured logger for backend failures and AI prompt issues.

    Uses the Flask app logger when available (imports lazily to avoid circular
    dependency); falls back to the standard Python logger when called outside
    of a Flask context (e.g. from asgi.py async tasks).
    """
    import app as _flask_app  # lazy — avoids circular import at module load time

    context = context if isinstance(context, dict) else {}
    message = str(error) if error is not None else ""
    payload = {
        "event": str(event_name or "unknown"),
        "message": message,
        "context": context,
        "ts": int(time.time()),
    }

    _flask_app.app.logger.error(
        "OBS_EVENT %s", json.dumps(payload, ensure_ascii=True),
        exc_info=error if isinstance(error, Exception) else False,
    )

    error_log_webhook_url = (os.environ.get("ERROR_LOG_WEBHOOK_URL") or "").strip()
    if error_log_webhook_url:
        try:
            import requests as _requests
            _requests.post(
                error_log_webhook_url,
                json=payload,
                timeout=2,
            )
        except Exception:
            pass
