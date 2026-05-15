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
import traceback
from datetime import datetime, timezone


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
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits the JSON formatter from the root."""
    return logging.getLogger(name)
