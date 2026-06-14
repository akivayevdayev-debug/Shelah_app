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
"""

from __future__ import annotations

import time
import threading
from collections import OrderedDict
from typing import Any, Callable, Optional


class TTLCache:
    """Thread-safe LRU + TTL in-memory cache.

    Evicts expired entries on access and evicts the least-recently-used
    entry when maxsize is reached.
    """

    def __init__(self, maxsize: int = 256, ttl: float = 3600.0) -> None:
        self._maxsize = max(1, maxsize)
        self._ttl = float(ttl)
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()

    # ── Public interface ──────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            return self._get_locked(key)

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expires = time.monotonic() + (ttl if ttl is not None else self._ttl)
        with self._lock:
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
        with self._lock:
            cached = self._get_locked(key)
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
