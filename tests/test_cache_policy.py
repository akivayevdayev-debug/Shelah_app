"""
Tests for backend/cache_policy.py (plan.md §14.3, Prompt 28).

Covers both the pure classification function and its two call sites:
app.py's Flask after_request hook (WSGI-mounted routes) and asgi.py's
request_id_middleware (native FastAPI routes, which previously shipped
with NO Cache-Control header at all -- see the regression test at the
bottom of this file).
"""

from __future__ import annotations

import pytest

import app as flask_app_module
from backend import cache_policy


# ─── classify_cache_tier: pure unit tests ───────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/library/index",
    "/api/library/leaf-refs",
    "/api/library/popular",
    "/api/texts-index",
    "/api/text/Genesis.1.1",
    "/api/prayer/shema",
    "/api/siddur/full/shacharit",
    "/api/library/category/Torah",
])
def test_classify_cache_tier_immutable_paths(path):
    assert cache_policy.classify_cache_tier("GET", path) == cache_policy.CACHE_TIER_IMMUTABLE


@pytest.mark.parametrize("path", [
    "/api/zmanim",
    "/api/zmanim/month",
    "/api/daily-study",
    "/api/holidays",
    "/api/parasha",
])
def test_classify_cache_tier_dated_paths(path):
    assert cache_policy.classify_cache_tier("GET", path) == cache_policy.CACHE_TIER_DATED


@pytest.mark.parametrize("path", [
    "/api/communities/list",
    "/api/communities",
    "/api/prayers/list",
    "/api/word/meaning",
    "/api/library/search",
    "/api/search/suggest",
    "/api/geocode",
    "/api/community/ashkenazi",
    "/api/community/ashkenazi/timeline",
])
def test_classify_cache_tier_corpus_paths(path):
    assert cache_policy.classify_cache_tier("GET", path) == cache_policy.CACHE_TIER_CORPUS


@pytest.mark.parametrize("path", [
    "/api/feedback",
    "/api/client-errors",
    "/ask",
    "/set_location",
])
def test_classify_cache_tier_private_paths(path):
    assert cache_policy.classify_cache_tier("GET", path) == cache_policy.CACHE_TIER_PRIVATE


def test_classify_cache_tier_unmatched_path_is_untouched():
    # Not /api/*, /ask, or /set_location -- caller's own static/HTML branch
    # decides, so this module must not opine.
    assert cache_policy.classify_cache_tier("GET", "/") is None
    assert cache_policy.classify_cache_tier("GET", "/service-worker.js") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_classify_cache_tier_non_get_never_promoted_even_on_a_cacheable_path(method):
    # A write endpoint is never cacheable by definition, regardless of path.
    assert cache_policy.classify_cache_tier(method, "/api/library/index") == cache_policy.CACHE_TIER_PRIVATE


def test_classify_cache_tier_unknown_api_path_defaults_private():
    assert cache_policy.classify_cache_tier("GET", "/api/some-new-route") == cache_policy.CACHE_TIER_PRIVATE


# ─── app.py's Flask after_request hook ──────────────────────────────────────

def test_flask_response_gets_immutable_cache_header(test_client):
    resp = test_client.get("/api/texts-index")
    assert resp.headers.get("Cache-Control") == cache_policy.CACHE_TIER_IMMUTABLE


def test_flask_response_gets_private_cache_header_for_ask(test_client):
    resp = test_client.post("/ask", json={"question": "test"})
    assert resp.headers.get("Cache-Control") == cache_policy.CACHE_TIER_PRIVATE


# ─── plan.md §46 / Prompt 58: Vary: Cookie must not defeat CDN caching ──────

def test_static_asset_carries_no_vary_cookie_header(test_client):
    """Regression test for plan.md §46: a static-asset request never reads
    or writes Flask's session, so it must not be marked "accessed" and must
    not get `Vary: Cookie` stamped on it -- that header becomes part of
    Vercel's edge-cache key and silently defeats CDN caching for every
    response that carries it, which is exactly what shipped from
    app.py's old unconditional `apply_session_cookie_policy()` before_request
    hook (removed by this fix)."""
    resp = test_client.get("/static/css/tokens.css")
    assert resp.status_code == 200
    assert "cookie" not in (resp.headers.get("Vary") or "").lower()


def test_anonymous_index_request_carries_no_vary_cookie_header(test_client):
    """Same regression as above for the index page: an anonymous request
    that never calls get_engine()/set_location() must not touch the
    session, so it must not get Vary: Cookie either."""
    resp = test_client.get("/")
    assert resp.status_code == 200
    assert "cookie" not in (resp.headers.get("Vary") or "").lower()


def test_set_location_request_still_gets_vary_cookie_header(test_client):
    """A route that genuinely writes to the session must still mark it
    accessed -- this response IS request-specific (it depends on the
    caller's own cookie) and is already forced to the private cache tier
    (see test_classify_cache_tier_private_paths above), so Vary: Cookie
    here is correct, not a regression."""
    resp = test_client.post(
        "/set_location",
        json={"lat": 40.7, "lon": -74.0},
        headers={"Origin": "http://localhost"},
    )
    assert resp.status_code == 200
    assert "cookie" in (resp.headers.get("Vary") or "").lower()


def test_daily_study_carries_no_vary_cookie_header(test_client):
    """Regression test for plan.md §47: /api/daily-study is CACHE_TIER_DATED
    (public, CDN-cacheable) but used to call get_engine(), which touches
    Flask's session even though get_daily_learning() never uses lat/lon --
    a narrower recurrence of the same §46 defect on a route §46's own
    verification pass didn't probe. Fixed by calling sefaria.get_daily_study()
    directly instead of going through get_engine()."""
    resp = test_client.get("/api/daily-study")
    assert resp.status_code == 200
    assert "cookie" not in (resp.headers.get("Vary") or "").lower()


def test_flask_g_force_private_overrides_table():
    """A route that sets g.cache_tier_force_private = True must win over
    whatever classify_cache_tier() would otherwise return for that path,
    even if the path itself is in a public tier -- this is the escape
    hatch backend/routes_calendar.py uses for the session-fallback branches
    of /api/zmanim, /api/zmanim/month, and /api/holidays. Exercised via a
    request context + direct hook call rather than registering a scratch
    route, so it doesn't leave a stray endpoint on the shared app object."""
    from flask import Response, g

    with flask_app_module.app.test_request_context("/api/zmanim"):
        g.cache_tier_force_private = True
        response = flask_app_module.apply_response_cache_policy(Response())

    assert response.headers.get("Cache-Control") == cache_policy.CACHE_TIER_PRIVATE


def test_flask_response_carries_deploy_hash_header_when_configured(test_client, monkeypatch):
    monkeypatch.setattr(flask_app_module, "SENTRY_RELEASE", "abc123deadbeef")
    resp = test_client.get("/api/texts-index")
    assert resp.headers.get("X-Deploy-Hash") == "abc123deadbeef"


def test_flask_private_response_never_carries_deploy_hash_header(test_client, monkeypatch):
    monkeypatch.setattr(flask_app_module, "SENTRY_RELEASE", "abc123deadbeef")
    resp = test_client.post("/ask", json={"question": "test"})
    assert "X-Deploy-Hash" not in resp.headers


# ─── asgi.py's request_id_middleware (native FastAPI routes) ───────────────

async def test_native_route_previously_had_no_cache_control_now_gets_private(fastapi_client):
    """Regression test: before backend/cache_policy.py existed, native
    FastAPI routes (matched before the WSGIMiddleware Flask mount) never
    passed through app.py's after_request hook and shipped with NO
    Cache-Control header whatsoever. /api/async/health is one such route
    and isn't in any public tier, so it must now get an explicit
    private/no-store rather than staying unset."""
    resp = await fastapi_client.get("/api/async/health")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == cache_policy.CACHE_TIER_PRIVATE
