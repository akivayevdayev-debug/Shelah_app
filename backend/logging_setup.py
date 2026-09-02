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

import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

try:
    import sentry_sdk
except Exception:  # pragma: no cover
    sentry_sdk = None

_request_id_var: ContextVar[str] = ContextVar('request_id', default='')
_user_id_var: ContextVar[str] = ContextVar('user_id', default='')
_client_key_var: ContextVar[str] = ContextVar('client_key', default='')
_budget_reservation_var: ContextVar[str] = ContextVar('budget_reservation_id', default='')

# Route classes for _traces_sampler (plan.md §17.3 deviation 7 / §17.5).
# `/ask` and the fan-out routes are where wall-clock actually costs money
# (§14's Provisioned Memory model bills I/O waits too, so a slow Sefaria
# call inside /ask is a cost finding, not a perf curiosity) — sample those
# meaningfully. Health checks and static assets are high-volume and tell us
# nothing new on every hit — sample those at zero.
_TRACE_SAMPLE_RATE_ASK = 0.5
_TRACE_SAMPLE_RATE_FANOUT = 0.2
_TRACE_SAMPLE_RATE_DEFAULT = 0.05
_TRACE_ZERO_PATH_PREFIXES = ("/api/health", "/api/stack/health", "/api/async/health", "/static/")
_TRACE_FANOUT_PATH_PREFIXES = (
    "/api/library/search", "/api/text/", "/api/word/meaning", "/api/geocode",
    "/api/siddur/full/", "/api/export/chapter",
)


def _traces_sampler(sampling_context: dict) -> float:
    """Route-aware trace sampling — replaces the flat traces_sample_rate.

    Reads the request path from whichever integration populated the
    sampling context: `asgi_scope` for the FastAPI/Starlette layer that
    fronts every request (including ones later routed to the WSGI-mounted
    Flask app), `wsgi_environ` as a fallback for bare-Flask runs
    (`python3 app.py`, no ASGI layer — see plan.md §16.1 D1).
    """
    asgi_scope = sampling_context.get("asgi_scope") or {}
    path = asgi_scope.get("path") or ""
    if not path:
        wsgi_environ = sampling_context.get("wsgi_environ") or {}
        path = wsgi_environ.get("PATH_INFO") or ""

    if not path:
        return 0.0
    if path.startswith(_TRACE_ZERO_PATH_PREFIXES):
        return 0.0
    if path == "/ask" or path.startswith("/ask/"):
        return _TRACE_SAMPLE_RATE_ASK
    if path.startswith(_TRACE_FANOUT_PATH_PREFIXES):
        return _TRACE_SAMPLE_RATE_FANOUT
    return _TRACE_SAMPLE_RATE_DEFAULT


def _build_sentry_init_kwargs(dsn: str) -> dict:
    """Assemble sentry_sdk.init() kwargs.

    Split out from the module-level init call below so the exact arguments
    (send_default_pii=False, no hardcoded dsn, traces_sampler rather than a
    flat traces_sample_rate — plan.md §17.3 deviations 1, 2 and 7) are
    unit-testable without reloading this module or faking sys.modules.
    """
    return {
        "dsn": dsn,
        # Overrides Sentry's vendor-suggested default of True (plan.md
        # §17.3 deviation 1 — the single most dangerous line in the vendor
        # snippet): request headers/cookies/IP must never be attached
        # automatically. Halachic questions are routinely medical/marital/
        # mental-health/abuse-adjacent (§8.B/§8.D).
        "send_default_pii": False,
        "traces_sampler": _traces_sampler,
        "environment": os.environ.get("VERCEL_ENV", "development"),
        "release": os.environ.get("VERCEL_GIT_COMMIT_SHA") or None,
    }


# Sentry is a true no-op unless SENTRY_DSN is set: no import errors, no
# network calls, no warnings. Initialization happens once at module import.
#
# INIT-ORDERING (plan.md §17.4 — load-bearing, do not regress): Sentry
# requires sentry_sdk.init() to run before the app object is constructed.
# This module is imported by asgi.py (see the `from backend.logging_setup
# import ...` line near the top of that file) well before
# `fastapi_app = FastAPI(...)` is constructed further down — so init() below
# always completes first, but only as a side effect of import order, not by
# explicit design. If asgi.py's imports are ever reshuffled (e.g. a future
# Phase-4-style cleanup) so that FastAPI(...) is constructed before this
# module is first imported, Sentry silently stops instrumenting the app —
# no exception, no log line, just missing data. See asgi.py's matching
# comment at the `from backend.logging_setup import ...` line.
_sentry_enabled = False
_sentry_dsn = (os.environ.get("SENTRY_DSN") or "").strip()
if sentry_sdk is not None and _sentry_dsn:
    try:
        sentry_sdk.init(**_build_sentry_init_kwargs(_sentry_dsn))
        _sentry_enabled = True
    except Exception:  # pragma: no cover
        _sentry_enabled = False


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


def submit_with_context(executor, fn, *args, **kwargs):
    """Submit *fn* to *executor*, propagating the calling contextvars.

    ThreadPoolExecutor workers don't inherit the submitting thread's
    contextvars by default, so request_id (and anything else on a
    ContextVar) silently drops out of every background-thread log line
    unless the submitting context is copied and replayed inside the worker.
    """
    ctx = copy_context()
    return executor.submit(ctx.run, fn, *args, **kwargs)


def bind_user_id(user_id: str | None = None) -> str:
    """Set the authenticated user_id in the current context and return it.

    Mirrors bind_request_id()'s contextvar pattern so cost-tracking calls
    deep in backend/claude.py can tag ai_usage_log rows with the caller's
    identity without threading user_id through every function signature.
    """
    uid = (user_id or "").strip()
    _user_id_var.set(uid)
    return uid


def get_user_id() -> str:
    """Return the current context's user_id (empty string if anonymous/unset)."""
    return _user_id_var.get()


def bind_client_key(client_key: str | None = None) -> str:
    """Set an anonymous-caller identifier (e.g. "ip:1.2.3.4") in the current
    context. Companion to bind_user_id() for the per-caller budget check —
    lets ai_usage_log rows from unauthenticated callers still be attributed
    to *something* for cost-ceiling purposes."""
    key = (client_key or "").strip()
    _client_key_var.set(key)
    return key


def get_client_key() -> str:
    """Return the current context's client_key (empty string if unset)."""
    return _client_key_var.get()


def bind_budget_reservation(reservation_id: str | None = None) -> str:
    """Set the current request's atomic budget-reservation id (plan.md
    §20.2 Phase 20b). Mirrors bind_user_id()'s contextvar pattern: set once
    when check_user_budget_and_enforce() reserves budget, read deep inside
    backend/claude.py by record_llm_call() so it can SETTLE the reservation
    with the real cost instead of inserting a second ai_usage_log row.
    Passing "" (the default) clears it -- record_llm_call() does this after
    settling so a second billed call in the same request (e.g. a Gemini
    primary call followed by a Claude fallback) inserts a normal additive
    row rather than re-settling an already-settled reservation."""
    rid = (reservation_id or "").strip()
    _budget_reservation_var.set(rid)
    return rid


def get_budget_reservation() -> str:
    """Return the current context's budget-reservation id (empty string if
    unset/already settled)."""
    return _budget_reservation_var.get()


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits the JSON formatter from the root."""
    return logging.getLogger(name)


_T = TypeVar("_T")


def submit_with_context(
    pool: ThreadPoolExecutor, fn: Callable[..., _T], *args: Any, **kwargs: Any
) -> "Future[_T]":
    """Submit *fn* to *pool*, preserving the caller's contextvars.

    ThreadPoolExecutor workers run with their own top-level context, not a
    copy of the submitting thread's — so request_id (and any other
    contextvar) silently vanishes from log records emitted inside
    pool-submitted work unless the caller's context is explicitly copied
    across the thread boundary, as done here.
    """
    ctx = copy_context()
    return pool.submit(ctx.run, fn, *args, **kwargs)


# ── Structured backend error logger ──────────────────────────────────────────

# Any of these substrings, found case-insensitively in a context dict key,
# marks that value as carrying halachic question/answer text (routinely
# medical/marital/mental-health/abuse-adjacent, plan.md §8.B/§8.D) rather
# than an operational value like a mode/id/count. Mirrors
# static/js/sentry-init.js's SENSITIVE_KEY_SUBSTRINGS.
_SENSITIVE_CONTEXT_KEY_SUBSTRINGS = (
    "question", "answer", "ruling", "summary", "practical_step", "body", "text",
)
# Backstop for any other free-text value that isn't caught by key name —
# mirrors sentry-init.js's MAX_FREE_TEXT_LENGTH. Not a substitute for the
# key-based redaction above, just defence-in-depth on top of it.
_MAX_FREE_TEXT_LENGTH = 250


def _is_sensitive_context_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(s in lowered for s in _SENSITIVE_CONTEXT_KEY_SUBSTRINGS)


def _truncate_free_text(value: str) -> str:
    if len(value) <= _MAX_FREE_TEXT_LENGTH:
        return value
    return value[:_MAX_FREE_TEXT_LENGTH] + "… [truncated]"


def _scrub_error_context(context: dict) -> dict:
    """Redact halachic question/answer text before a backend-error payload
    reaches structured logs, the error webhook, or Sentry.

    send_default_pii=False (this module's sentry_sdk.init() call) only
    suppresses Sentry's *automatic* PII capture (headers/cookies/IP); it
    does nothing to stop application code from manually attaching sensitive
    content via contexts=, which is exactly what _capture_backend_error's
    callers do when they pass {"question": question, ...}. This is the
    server-side mirror of static/js/sentry-init.js's scrubEventInPlace().
    """
    scrubbed: dict = {}
    for key, value in context.items():
        if _is_sensitive_context_key(key):
            scrubbed[key] = "[Filtered]"
        elif isinstance(value, str):
            scrubbed[key] = _truncate_free_text(value)
        else:
            scrubbed[key] = value
    return scrubbed


def hash_user_id(user_id) -> str:
    """One-way digest of a Clerk `sub` for _capture_backend_error context
    (plan.md §39.3): a real DELETE already removes this identity from
    every live table on account deletion, but the raw value would still
    persist in Sentry/webhook history for however long that store retains
    events. Mirrors backend/rate_limit.py's _hash_key -- same construction
    (sha256, truncated to 16 hex chars), so the same user_id always hashes
    to the same short, non-reversible value, preserving the ability to
    correlate a user's own error reports without exposing their raw
    identity in third-party telemetry."""
    return hashlib.sha256(str(user_id or "").encode()).hexdigest()[:16]


def _is_discord_webhook_url(url: str) -> bool:
    """Discord webhook URLs are always ``https://[sub.]discord(app).com/api/webhooks/...`` —
    matches subdomains (``ptb.``, ``canary.``) via substring containment."""
    return "discord.com/api/webhooks" in url or "discordapp.com/api/webhooks" in url


def _discord_webhook_body(payload: dict) -> dict:
    """Reshape ``_capture_backend_error``'s flat payload into Discord's webhook
    contract (``embeds``), which rejects arbitrary JSON — it requires a non-empty
    ``content`` or ``embeds`` and enforces its own per-field length limits. Posting
    the flat payload as-is (the pre-fix behavior) gets a silent 400 from Discord:
    the caller never checks the response status, so nothing has ever actually
    reached Discord for any event using this webhook, with no error anywhere to
    show for it."""
    context_str = json.dumps(payload.get("context") or {}, ensure_ascii=True)
    if len(context_str) > 950:
        context_str = context_str[:950] + "…"
    fields = [{"name": "request_id", "value": str(payload.get("request_id") or "—"), "inline": True}]
    if context_str != "{}":
        fields.append({"name": "context", "value": f"```{context_str}```", "inline": False})
    ts = payload.get("ts")
    embed = {
        "title": str(payload.get("event") or "unknown")[:256],
        "description": (str(payload.get("message") or "(no message)"))[:4096],
        "color": 0xE74C3C,
        "fields": fields,
    }
    if isinstance(ts, (int, float)):
        embed["timestamp"] = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return {"embeds": [embed]}


def _capture_backend_error(event_name, error, context=None):
    """Sentry-style structured logger for backend failures and AI prompt issues.

    Uses the Flask app logger when available (imports lazily to avoid circular
    dependency); falls back to the standard Python logger when called outside
    of a Flask context (e.g. from asgi.py async tasks).
    """
    import app as _flask_app  # lazy — avoids circular import at module load time

    context = _scrub_error_context(context if isinstance(context, dict) else {})
    message = _truncate_free_text(str(error) if error is not None else "")
    request_id = get_request_id()
    payload = {
        "event": str(event_name or "unknown"),
        "message": message,
        "context": context,
        "request_id": request_id,
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
            webhook_body = (
                _discord_webhook_body(payload)
                if _is_discord_webhook_url(error_log_webhook_url)
                else payload
            )
            _requests.post(
                error_log_webhook_url,
                json=webhook_body,
                timeout=2,
            )
        except Exception:
            pass

    # Forward to Sentry when configured. True no-op when SENTRY_DSN is unset:
    # _sentry_enabled is only True after a successful sentry_sdk.init() above.
    # Isolated in its own try/except so a Sentry SDK failure can never break
    # the caller — this must remain purely additive to the logging above.
    if _sentry_enabled:
        try:
            sentry_sdk.capture_exception(
                error if isinstance(error, BaseException) else None,
                contexts={
                    "backend_error": {**(context or {}), "request_id": request_id},
                },
            )
        except Exception:
            pass


_mitigation_logger = logging.getLogger("shelah.mitigation")


def log_mitigation(tier: str, route_class: str, key_hash: str, route: str) -> None:
    """One structured log line + Sentry breadcrumb per abuse-mitigation
    action (plan.md §16.4 observability / Prompt 29c §16.6 Phase 9c).

    Deliberately NOT routed through _capture_backend_error(): rate-limit
    429s and quota rejections are routine and expected under normal load
    (unlike a store outage, which IS still a _capture_backend_error event
    elsewhere in this file) -- turning every one into a Sentry *event* would
    burn the free-tier 5,000-events/month quota on ordinary traffic shaping
    (see plan.md §17.1's DSN-exhaustion warning for why that quota is
    treated as itself attackable). A breadcrumb instead keeps routine
    mitigations visible as context on whatever real error/event follows,
    without costing quota.

    tier is "waf" | "middleware" | "breaker" per plan.md §16.4's schema --
    Turnstile challenges (backend/turnstile.py) also log as "middleware"
    since Turnstile is L2 supporting hardening (§16.4 sits directly under
    L2 in plan.md §16.3), not a fourth independent layer. key_hash must
    already be hashed by the caller -- this function never receives or logs
    a raw IP or Clerk sub (plan.md §8.D privacy).
    """
    _mitigation_logger.info(
        "mitigation_triggered",
        extra={"tier": tier, "class": route_class, "key_hash": key_hash, "route": route},
    )
    if _sentry_enabled:
        try:
            sentry_sdk.add_breadcrumb(
                category="mitigation",
                message=f"{tier}:{route_class}",
                level="info",
                data={"key_hash": key_hash, "route": route},
            )
        except Exception:
            pass
