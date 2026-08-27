"""
backend/cache.py

Shared bounded TTL + LRU cache.  Replaces the ≥6 hand-rolled cache dicts
scattered across sefaria_library, search, zmanim_engine, sefaria, etc.

Usage::

    from backend.cache import TTLCache

    _cache = TTLCache(maxsize=512, ttl=3600)

    value = _cache.get(key)
    _cache.set(key, value)
    value = _cache.get_or_fetch(key, lambda: expensive_call())

Cross-instance tier
--------------------
Vercel Fluid Compute runs many concurrent, ephemeral serverless instances,
each with its own process memory -- a plain TTLCache only ever protects the
instance that populated it, so every *other* concurrently-cold instance
still pays full origin latency. Passing ``redis_prefix=`` makes a TTLCache
transparently also read/write a shared Redis tier (memory -> Redis, both
populated on write, memory checked first), closing that gap for any cache
whose values are safe to share across all users/instances (i.e. not
per-session/personalized data).

backend/rate_limit.py already solved this exact problem for rate limiting
via ``get_shared_store()`` -- a module-level ``redis.asyncio`` client. This
module intentionally does NOT reuse that client. Every call site that needs
this tier (get_library_index()/_cached_get() in sefaria_library.py,
get_daily_study() in sefaria.py) runs as a synchronous Flask view executed
in a worker thread by Starlette's WSGIMiddleware -- there is no running
asyncio event loop to await into, and redis.asyncio connections are bound
to the event loop that first used them, so borrowing rate_limit's client
from a different thread/loop is not safe. This module opens a second,
*synchronous* `redis.Redis` client instead, pointed at the same
RATE_LIMIT_REDIS_URL / same Redis deployment -- never a new env var, never
a second Redis instance -- which is the concrete exception the project's
reuse rule anticipates rather than a duplicated store.
"""

from __future__ import annotations

import json
import logging
import os
import time
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class _RedisUnavailable(Exception):
    """Raised internally when the shared sync Redis client can't be reached."""


class _SyncRedisClient:
    """Minimal synchronous Redis wrapper for the cross-instance cache tier.
    See module docstring for why this is a second, sync-only client rather
    than a reuse of backend/rate_limit.py's async one."""

    def __init__(self, url: str) -> None:
        import redis  # local import: optional dependency until configured

        self._client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )

    def get(self, key: str) -> Optional[str]:
        try:
            return self._client.get(key)
        except Exception as exc:  # redis.exceptions.* + connection/timeout errors
            raise _RedisUnavailable(str(exc)) from exc

    def setex(self, key: str, ttl_seconds: int, value: str) -> None:
        try:
            self._client.setex(key, ttl_seconds, value)
        except Exception as exc:  # redis.exceptions.* + connection/timeout errors
            raise _RedisUnavailable(str(exc)) from exc


def _build_shared_redis_client() -> Optional[_SyncRedisClient]:
    # Same env var rate_limit.py reads -- intentionally not imported from
    # there (that module pulls in starlette/backend.auth/backend.helpers,
    # unwanted weight on this module's already-broad import graph) -- see
    # module docstring for why a *second client object* on the same var is
    # still "reuse" in the sense that matters (one Redis deployment, one
    # config knob).
    url = (os.environ.get("RATE_LIMIT_REDIS_URL") or "").strip()
    if not url:
        return None
    try:
        return _SyncRedisClient(url)
    except Exception as exc:
        logger.warning(
            "shared Redis cache tier: RATE_LIMIT_REDIS_URL is set but invalid "
            "(%s: %s) -- falling back to in-process-only caching. A malformed "
            "store must never crash app boot.",
            type(exc).__name__, exc,
            exc_info=True,
        )
        return None


_shared_redis_client: Optional[_SyncRedisClient] = _build_shared_redis_client()


def redis_cache_get(key: str) -> Optional[Any]:
    """Best-effort cross-instance cache read. Returns None on a miss OR any
    failure (unconfigured, unreachable, corrupt payload) -- callers always
    have a working fallback and must never treat this as authoritative."""
    if _shared_redis_client is None:
        return None
    try:
        raw = _shared_redis_client.get(key)
    except _RedisUnavailable as exc:
        logger.debug("shared Redis cache read skipped for %s: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def redis_cache_set(key: str, value: Any, ttl_seconds: float) -> None:
    """Best-effort cross-instance cache write. Never raises -- a write
    failure just means the next cold instance pays full price again."""
    if _shared_redis_client is None:
        return
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return
    try:
        _shared_redis_client.setex(key, max(1, int(ttl_seconds)), payload)
    except _RedisUnavailable as exc:
        logger.debug("shared Redis cache write skipped for %s: %s", key, exc)


class TTLCache:
    """Thread-safe LRU + TTL in-memory cache, optionally backed by a shared
    Redis tier for cross-instance consistency (see module docstring).

    Evicts expired entries on access and evicts the least-recently-used
    entry when maxsize is reached.
    """

    def __init__(
        self,
        maxsize: int = 256,
        ttl: float = 3600.0,
        redis_prefix: Optional[str] = None,
    ) -> None:
        self._maxsize = max(1, maxsize)
        self._ttl = float(ttl)
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        # Values must be JSON-serializable when set -- only opt a cache in
        # here when every value it holds is (all current uses store parsed
        # JSON API responses, or plain dicts/strings/lists derived from
        # them). None if this cache is memory-only (default/unchanged).
        self._redis_prefix = redis_prefix

    # ── Public interface ──────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            cached = self._get_locked(key)
        if cached is not None or self._redis_prefix is None:
            return cached
        value = redis_cache_get(self._redis_prefix + key)
        if value is not None:
            # Backfill local memory so the rest of this instance's requests
            # skip the Redis round-trip too.
            with self._lock:
                if key not in self._store:
                    self._set_locked(key, value, self._ttl)
        return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        resolved_ttl = ttl if ttl is not None else self._ttl
        with self._lock:
            self._set_locked(key, value, resolved_ttl)
        if self._redis_prefix is not None:
            redis_cache_set(self._redis_prefix + key, value, resolved_ttl)

    def _set_locked(self, key: str, value: Any, ttl: float) -> None:
        expires = time.monotonic() + ttl
        if key in self._store:
            del self._store[key]
        elif len(self._store) >= self._maxsize:
            self._store.popitem(last=False)
        self._store[key] = (value, expires)

    def get_or_fetch(
        self,
        key: str,
        fetch_fn: Callable[[], Any],
        ttl: Optional[float] = None,
    ) -> Any:
        """Return cached value or call fetch_fn, cache its result, and return it."""
        cached = self.get(key)
        if cached is not None:
            return cached
        value = fetch_fn()
        if value is not None:
            self.set(key, value, ttl=ttl)
        return value

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_locked(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires = entry
        if time.monotonic() > expires:
            del self._store[key]
            return None
        self._store.move_to_end(key)
        return value
