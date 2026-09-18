"""
Unified rate-limit middleware for Sh'elah (plan.md §16 Phase 9a / §16.3-L2).

Prior state (plan.md §16.8.1): Flask-Limiter (app.py) and a second,
independently-maintained in-process limiter (asgi.py, /ask only) enforced
two policies that happened to agree but had nothing keeping them in sync --
two stores, two key functions, two 429 body shapes. That is the "live,
divergent duplication" failure mode plan.md §2/§16.3 singles out as this
project's most dangerous anti-pattern, and it had been re-created in the
security layer. This module replaces both with one policy table, one
store, one key function, one 429 body shape, installed once as ASGI
middleware on ``asgi.fastapi_app``.

Starlette's middleware stack wraps ``app.router`` -- which handles dispatch
to ``Mount()``-ed sub-apps too -- so middleware registered here sees 100% of
traffic exactly once, including every Flask route reached through
``asgi.py``'s ``WSGIMiddleware`` mount, before any route handler (FastAPI
or Flask) runs. This module must not import ``app`` (plan.md §2 reuse
rule -- backend/* must not depend on the Flask app module).
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import logging
import os
import time
from dataclasses import dataclass

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.auth import extract_user_id_from_bearer_value
from backend.helpers import _resolve_client_ip
from backend.logging_setup import _capture_backend_error, log_mitigation

logger = logging.getLogger(__name__)


# ─── Policy table (plan.md §16.3-L2) ───────────────────────────────────────
# Every route falls into exactly one class; anything that matches no known
# prefix falls into "cheap" -- the same generous-default bucket the removed
# Flask-Limiter RATE_LIMIT_DEFAULT used to cover (plan.md §16.1 D4), so
# removing Flask-Limiter does not reopen D4.
#
# Anonymous/authenticated differentiation beyond a plain key swap, and
# Clerk-sub keying for classes other than llm, were deliberately NOT built
# in Phase 9a/9b -- that is Phase 9c ("identity & polish", plan.md §16.6),
# implemented below via _Policy.authenticated_max_requests/daily_max_requests
# for the llm class specifically (§16.3-L2's own example: "a yeshiva, day
# school, or shul behind one CGNAT egress" needs a higher signed-in
# allowance, not just a different bucket key).

@dataclass(frozen=True)
class _Policy:
    window_seconds: int
    max_requests: int
    # Posture when the shared store is unreachable (plan.md §16.3-L2):
    # fail OPEN for read-only/library-ish traffic (a reader should not be
    # blocked because Redis blipped), fail CLOSED for the one class that
    # spends real USD per call (an unmetered /ask during a store outage is
    # a budget hole, not a degraded feature). This asymmetry is the whole
    # point and must not be "simplified" later.
    fail_open: bool
    # Phase 9c identity-aware quotas (plan.md §16.3-L2, §16.6 Phase 9c).
    # None means "same as max_requests" / "no daily cap" -- only the llm
    # class sets these today; every other class keeps one flat per-minute
    # bucket regardless of auth state.
    authenticated_max_requests: int | None = None
    daily_max_requests: int | None = None


_POLICIES: dict[str, _Policy] = {
    "llm": _Policy(
        window_seconds=60, max_requests=20, fail_open=False,
        # Signed-in users get a higher per-minute allowance (2x anonymous)
        # plus an explicit daily quota -- anonymous traffic keeps the
        # tighter IP bucket with no daily cap of its own (it's already the
        # tighter of the two, and Turnstile -- see backend/turnstile.py --
        # is the anonymous-specific escalation past a request volume, not
        # a second numeric ceiling here).
        authenticated_max_requests=40,
        daily_max_requests=200,
    ),
    "heavy": _Policy(window_seconds=60, max_requests=10, fail_open=True),
    "fanout": _Policy(window_seconds=60, max_requests=30, fail_open=True),
    "feedback": _Policy(window_seconds=60, max_requests=10, fail_open=True),
    "telemetry": _Policy(window_seconds=60, max_requests=10, fail_open=True),
    "cheap": _Policy(window_seconds=60, max_requests=120, fail_open=True),
    # Privacy-sensitive routes that used to fall through to "cheap" (a 2026-
    # 09-02 audit flagged this as under-protected, since "cheap" is also the
    # bucket read-only library lookups share): account-data actions a
    # legitimate caller invokes rarely, so a low ceiling costs nothing real
    # while capping abuse -- and, for "account", keyed by Clerk user id (see
    # _build_key) so one NATed IP can't cap every other user behind it.
    "account": _Policy(window_seconds=60, max_requests=5, fail_open=True),
    # /api/webhooks/clerk stays IP-keyed (no per-caller identity to key on --
    # it's Clerk calling us, not an end user); Svix signature verification
    # is the real gate on this cascade-delete trigger, but a materially
    # tighter ceiling than "cheap" is still worth it as defense-in-depth.
    "webhook": _Policy(window_seconds=60, max_requests=15, fail_open=True),
}

_DAILY_WINDOW_SECONDS = 86400

# (path_prefix, class) -- first prefix match wins; exact-or-startswith.
_ROUTE_CLASSES: list[tuple[str, str]] = [
    ("/ask", "llm"),
    ("/api/export/chapter", "heavy"),
    ("/api/siddur/full/", "heavy"),
    ("/api/library/search", "fanout"),
    ("/api/text/", "fanout"),
    ("/api/word/meaning", "fanout"),
    ("/api/geocode", "fanout"),
    ("/api/feedback", "feedback"),
    ("/api/client-errors", "telemetry"),
    ("/api/user/delete-account", "account"),
    ("/api/user/data-export", "account"),
    ("/api/webhooks/clerk", "webhook"),
]


def classify_route(path: str) -> str:
    for prefix, cls in _ROUTE_CLASSES:
        if path == prefix or path.startswith(prefix):
            return cls
    return "cheap"


# ─── Store abstraction ──────────────────────────────────────────────────────

class _StoreUnavailable(Exception):
    """Raised by a store's incr() when the backend could not be reached."""


class _RateLimitStore:
    async def incr(self, key: str, window_seconds: int) -> int:
        raise NotImplementedError

    async def get(self, key: str) -> str | None:
        raise NotImplementedError

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        raise NotImplementedError


class _InMemoryStore(_RateLimitStore):
    """Sliding-window store ported from asgi.py's pre-unification in-process
    limiter. Per-process only -- this IS plan.md §16.1 D3, kept solely as
    the local-dev/test fallback when RATE_LIMIT_REDIS_URL is unset. Never
    the intended production store; see _build_store()'s startup warning.

    OrderedDict gives O(1) LRU eviction via move_to_end -- evicts the
    least-recently-used key instead of the oldest-inserted one, preventing
    an attacker cycling through many keys from flushing active callers'
    counters.
    """

    _MAX_KEYS = 2048

    def __init__(self) -> None:
        self._buckets: collections.OrderedDict[str, collections.deque] = collections.OrderedDict()
        # Separate from _buckets (sliding-window counters) -- this is a plain
        # value+expiry store for get()/setex() callers (e.g. the cost
        # breaker's cached total), which have nothing to do with request
        # counting and would corrupt the deque-based incr() logic if shared.
        self._values: dict[str, tuple[str, float]] = {}

    async def incr(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        if key not in self._buckets:
            if len(self._buckets) >= self._MAX_KEYS:
                self._buckets.popitem(last=False)
            self._buckets[key] = collections.deque()
        else:
            self._buckets.move_to_end(key)

        timestamps = self._buckets[key]
        cutoff = now - window_seconds
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        timestamps.append(now)
        return len(timestamps)

    async def get(self, key: str) -> str | None:
        entry = self._values.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._values[key]
            return None
        return value

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        if len(self._values) >= self._MAX_KEYS:
            self._values.pop(next(iter(self._values)))
        self._values[key] = (value, time.monotonic() + ttl_seconds)


class _RedisStore(_RateLimitStore):
    """Fixed-window counter over Upstash Redis (or any rediss://-reachable
    Redis), matching the fixed-window algorithm Vercel's own edge WAF uses
    (plan.md §16.3-L1) so the two layers behave consistently. INCR is
    atomic; EXPIRE is only set on the increment that creates the key (count
    == 1), so a steady stream of requests can't keep pushing the window
    forward -- classic "INCR then conditionally EXPIRE" fixed-window
    pattern, chosen over EXPIRE...NX for broad Redis/Upstash compatibility.

    Loop-safety: this store is a module-level singleton (``_store`` below,
    built once at import time -- see ``get_shared_store()``), but a
    ``redis.asyncio`` client's connection pool binds its asyncio primitives
    (locks/futures/transports) to whichever event loop is running the first
    time a command actually executes. A process that outlives one event
    loop and later serves requests on a *different* one -- every test here
    (pytest-asyncio creates a fresh loop per test function) and, in
    production, a warm Vercel Fluid Compute instance reused across
    invocations -- would otherwise reuse a pool wired to a closed loop and
    fail with "Task ... got Future ... attached to a different loop" /
    "Event loop is closed". ``_client_for_current_loop()`` re-binds (by
    building a fresh client against the same ``self._url``) whenever the
    currently-running loop differs from the one the cached client was last
    used on, so each event loop gets its own client instead of a stale one
    being force-reused across loop boundaries. The stale client/pool is not
    explicitly closed -- its transport belongs to a loop that may already be
    closed by the time we notice, so attempting to close it here could
    itself raise; it is simply dropped and left for GC.
    """

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis_asyncio  # local import: optional until configured

        self._url = url
        # Built eagerly so a malformed URL still raises synchronously out of
        # __init__ (matching _build_store()'s try/except, which must never
        # let a bad URL crash app boot) -- but not yet "bound" to any event
        # loop (_client_loop stays None until first real use binds it).
        self._client = redis_asyncio.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        self._client_loop: object | None = None

    def _client_for_current_loop(self):
        """Return an async Redis client guaranteed to be bound to the
        currently-running event loop, rebuilding it if the loop changed
        since it was last used. Test doubles built via
        ``_RateLimitStore.__new__(_RedisStore)`` (see tests/test_rate_limit.py)
        skip __init__ and assign ``_client`` directly with no ``_url`` --
        for those, there is nothing to rebind against, so the assigned fake
        client is returned unchanged."""
        url = getattr(self, "_url", None)
        if url is None:
            return self._client

        import redis.asyncio as redis_asyncio  # local import: optional until configured

        loop = asyncio.get_running_loop()
        if self._client is None or (
            self._client_loop is not None and self._client_loop is not loop
        ):
            self._client = redis_asyncio.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=2.0,
                socket_connect_timeout=2.0,
            )
        self._client_loop = loop
        return self._client

    async def incr(self, key: str, window_seconds: int) -> int:
        try:
            client = self._client_for_current_loop()
            count = await client.incr(key)
            if count == 1:
                await client.expire(key, window_seconds)
            return int(count)
        except Exception as exc:  # redis.exceptions.* + connection/timeout errors
            raise _StoreUnavailable(str(exc)) from exc

    async def get(self, key: str) -> str | None:
        try:
            client = self._client_for_current_loop()
            return await client.get(key)
        except Exception as exc:  # redis.exceptions.* + connection/timeout errors
            raise _StoreUnavailable(str(exc)) from exc

    async def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        try:
            client = self._client_for_current_loop()
            await client.setex(key, ttl_seconds, value)
        except Exception as exc:  # redis.exceptions.* + connection/timeout errors
            raise _StoreUnavailable(str(exc)) from exc


RATE_LIMIT_REDIS_URL = (os.environ.get("RATE_LIMIT_REDIS_URL") or "").strip()
# Preserves the pre-unification kill switch's name and contract (used by
# tests/conftest.py and ci.yml) -- was previously scoped to Flask-Limiter's
# own `enabled` kwarg only, so it silently protected nothing on the ASGI
# /ask path it was meant to cover. Now a single switch for the one limiter.
RATELIMIT_ENABLED = (os.environ.get("RATELIMIT_ENABLED") or "true").strip().lower() == "true"


def _build_store() -> _RateLimitStore:
    if RATE_LIMIT_REDIS_URL:
        try:
            return _RedisStore(RATE_LIMIT_REDIS_URL)
        except Exception as exc:
            logger.critical(
                "RATE_LIMIT_REDIS_URL is set but invalid (%s: %s) -- rate limiting "
                "is falling back to an in-process store. This does NOT enforce a "
                "real limit across Vercel Fluid's multiple concurrent instances. A "
                "malformed store must never crash app boot -- check the value in "
                "the Vercel dashboard (expected: rediss://default:<password>@<host>:<port>).",
                type(exc).__name__, exc,
                exc_info=True,
            )
            return _InMemoryStore()
    logger.warning(
        "RATE_LIMIT_REDIS_URL is not set -- rate limiting is falling back to "
        "an in-process store. Fine for local dev, but per plan.md §16.1 "
        "D3 this does NOT enforce a real limit across Vercel Fluid's "
        "multiple concurrent instances. Do not deploy to production "
        "without RATE_LIMIT_REDIS_URL set."
    )
    return _InMemoryStore()


_store: _RateLimitStore = _build_store()


def get_shared_store() -> _RateLimitStore:
    """Expose the module-level store for reuse outside the rate limiter
    itself (plan.md §3 reuse rule / §16.3-L3) -- e.g. backend/cost_meter.py's
    global cost breaker caches its measurement here instead of opening a
    second Redis connection or inventing a second store abstraction."""
    return _store


def _build_key(route_class: str, client_ip: str, user_id: str | None) -> str:
    if route_class in ("llm", "account") and user_id:
        # Identity-aware for classes where an authenticated caller should
        # get a per-account bucket instead of sharing a NATed IP's bucket
        # with every other user behind it: "llm" shipped this pre-
        # unification (asgi.py's old /ask-only limiter); "account" (the
        # delete-account/data-export routes) was added per a 2026-09-02
        # audit. "webhook" (/api/webhooks/clerk) deliberately stays IP-keyed
        # -- see _ROUTE_CLASSES -- there is no caller identity to key on,
        # it's Clerk calling us, not an end user.
        return f"rl:{route_class}:user:{user_id}"
    return f"rl:{route_class}:ip:{client_ip}"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def _check(route_class: str, client_ip: str, user_id: str | None, path: str) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds)."""
    policy = _POLICIES[route_class]
    authenticated = bool(user_id)
    key = _build_key(route_class, client_ip, user_id)
    per_minute_limit = (
        policy.authenticated_max_requests
        if authenticated and policy.authenticated_max_requests is not None
        else policy.max_requests
    )

    try:
        count = await _store.incr(key, policy.window_seconds)
        if count > per_minute_limit:
            log_mitigation("middleware", route_class, _hash_key(key), path)
            return False, policy.window_seconds

        if authenticated and policy.daily_max_requests is not None:
            daily_key = f"{key}:daily"
            daily_count = await _store.incr(daily_key, _DAILY_WINDOW_SECONDS)
            if daily_count > policy.daily_max_requests:
                log_mitigation("middleware", route_class, _hash_key(daily_key), path)
                return False, _DAILY_WINDOW_SECONDS

        return True, policy.window_seconds
    except _StoreUnavailable as exc:
        key_hash = _hash_key(key)
        logger.error(
            "rate_limit_store_unavailable class=%s key_hash=%s fail_open=%s",
            route_class, key_hash, policy.fail_open,
        )
        await asyncio.to_thread(
            _capture_backend_error,
            "rate_limit_store_unavailable",
            exc,
            {"class": route_class, "key_hash": key_hash, "fail_open": str(policy.fail_open)},
        )
        return policy.fail_open, policy.window_seconds


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Installed on asgi.fastapi_app (see asgi.py) -- the single point of
    rate-limit enforcement for both native FastAPI routes and every Flask
    route reached through the WSGIMiddleware mount underneath.
    """

    async def dispatch(self, request: Request, call_next):
        if not RATELIMIT_ENABLED:
            return await call_next(request)

        path = request.url.path
        route_class = classify_route(path)

        client_ip = _resolve_client_ip(
            request.headers,
            remote_addr=(request.client.host if request.client else None),
        )
        user_id = None
        if route_class in ("llm", "account"):
            user_id = extract_user_id_from_bearer_value(request.headers.get("authorization"))

        allowed, retry_after = await _check(route_class, client_ip, user_id, path)
        if not allowed:
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded. Please wait before sending another request.",
                    "code": "rate_limited",
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)
