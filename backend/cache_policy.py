"""CDN/browser cache-tier classification (plan.md §14.3).

Single source of truth for Cache-Control on the /api/* + /ask + /set_location
surface, consulted from BOTH app.py's Flask ``after_request`` hook (routes
reached through the WSGIMiddleware mount) and asgi.py's ``request_id_middleware``
(the two native FastAPI routes -- POST /ask and GET /api/async/health -- that
are matched before the WSGI mount and therefore never see Flask's hook at
all). Before this module existed, that gap meant production /ask shipped with
NO Cache-Control header whatsoever, not even an explicit no-store -- a
correctness gap this module closes as a side effect of unifying the two call
sites onto one table (plan.md §2: one policy, not two that can drift).

Every route lands in exactly one tier below; anything unmatched falls back to
the pre-existing blanket behavior (no-store for /api/*, /ask, /set_location;
unset for everything else, matching the historical fallthrough). This module
does NOT own the static/HTML/service-worker/manifest tiers -- those keep
their existing handling in app.py, untouched by this pass.

Some routes are cacheable ONLY when their response is a pure function of the
URL. GET /api/zmanim (and /api/zmanim/month) fall back to the caller's
SESSION-remembered location when lat/lon query params are absent
(app.py::get_engine() reads session['lat']/session['lon']) -- caching that
response publicly would serve one user's location-derived zmanim to another.
GET /api/holidays' last-resort fallback chain has the same property. Both
call sites set ``g.cache_tier_force_private = True`` themselves (see
backend/routes_calendar.py) when they take the session-dependent branch;
apply_response_cache_policy() checks that flag before consulting this table.
Static, per-request-parameter facts belong in this table; response-content-
dependent facts belong in that per-request flag, decided by the handler that
actually knows which branch it took.
"""

from __future__ import annotations

CACHE_TIER_IMMUTABLE = "public, s-maxage=86400, stale-while-revalidate=604800"
CACHE_TIER_DATED = "public, s-maxage=3600, stale-while-revalidate=86400"
CACHE_TIER_CORPUS = "public, s-maxage=3600, stale-while-revalidate=86400"
CACHE_TIER_PRIVATE = "private, no-store"

# Sefaria/Torah-library content: sourced from an external, independently
# versioned corpus our own deploys don't mutate the *content* of.
_IMMUTABLE_EXACT = {
    "/api/library/index",
    "/api/library/leaf-refs",
    "/api/library/popular",
    "/api/texts-index",
}
_IMMUTABLE_PREFIXES = (
    "/api/text/",        # by-ref text + /links + /graph (commentary/source graph)
    "/api/prayer/",       # singular: preview text -- distinct from /api/prayers/list below
    "/api/siddur/full/",
)
_IMMUTABLE_CATEGORY_PREFIX = "/api/library/category/"

# Deterministic by today's wall-clock date (or an explicit date-shaped query
# param); safe to cache for up to an hour without becoming visibly stale.
_DATED_EXACT = {
    "/api/zmanim",         # ONLY public when lat+lon are both explicit query params -- see module docstring
    "/api/zmanim/month",   # same caveat
    "/api/daily-study",    # date-only; verified location-independent (backend/data_service.py::get_daily_learning)
    "/api/holidays",       # location-independent on its primary + pyluach-fallback paths; last-resort branch opts out itself
    "/api/parasha",
}

# Corpus-derived / external-lookup content: changes only when the underlying
# community corpus is edited or an external proxy's answer changes, neither
# of which happens more than about once an hour in practice.
_CORPUS_EXACT = {
    "/api/communities/list",
    "/api/communities",
    "/api/prayers/list",
    "/api/word/meaning",
    "/api/library/search",
    "/api/search/suggest",
    "/api/geocode",
}
_CORPUS_PREFIX = "/api/community/"  # /api/community/<name> and /api/community/<name>/timeline


def classify_cache_tier(method: str, path: str) -> str | None:
    """Return the Cache-Control value for (method, path), or None to leave
    the header untouched (the caller's existing static/HTML logic applies).

    Only GET is ever promoted to a public tier -- every other method stays
    on the private/no-store fail-safe regardless of path, since a write
    endpoint is never cacheable by definition.
    """
    if method == "GET":
        if path in _IMMUTABLE_EXACT or path.startswith(_IMMUTABLE_PREFIXES) or \
                path.startswith(_IMMUTABLE_CATEGORY_PREFIX):
            return CACHE_TIER_IMMUTABLE
        if path in _DATED_EXACT:
            return CACHE_TIER_DATED
        if path in _CORPUS_EXACT or path.startswith(_CORPUS_PREFIX):
            return CACHE_TIER_CORPUS

    return _private_default(path)


def _private_default(path: str) -> str | None:
    """Fail-safe default: every /api/* route plus the two historical
    always-private aliases get an explicit no-store; anything else is left
    for the caller's own (unchanged) static/HTML branch."""
    if path.startswith("/api/") or path in {"/ask", "/set_location"}:
        return CACHE_TIER_PRIVATE
    return None
