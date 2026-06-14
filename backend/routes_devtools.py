"""
Devtools / diagnostics blueprint for Sh'elah.

Stats, health, reliability, and debugging routes extracted verbatim from
``app.py`` (Stage 2 blueprint split). Logic is unchanged; only the route
decorator target moved from ``@app.route`` to ``@routes_devtools.route`` and the
shared helpers/constants are now imported from ``app`` and ``backend``.
"""

import json
import time
from typing import Any

from flask import Blueprint, jsonify, request, g

from app import (
    app,
    DEVTOOLS_STATS,
    limiter,
    RATE_LIMIT_ASK,
    RATE_LIMIT_DEFAULT,
    CLERK_PUBLISHABLE_KEY,
    CLERK_JWT_ISSUER,
    CLERK_ENFORCE_AUTH,
    SUPABASE_URL,
    SUPABASE_SERVICE_ROLE_KEY,
    SUPABASE_PUBLISHABLE_KEY,
    SUPABASE_PREFS_TABLE,
    SUPABASE_USER_MEMORIES_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE,
    STRICT_SUPABASE_RLS,
    api_health,
    maybe_require_clerk_auth,
    _get_supabase_client,
    _get_request_supabase_client,
    _extract_supabase_access_token,
    _get_request_user_id,
    _extract_client_ip,
    _capture_backend_error,
)

routes_devtools = Blueprint("devtools", __name__)


@routes_devtools.route("/api/stack/health")
def stack_health():
    """Return runtime readiness for Bento stack components."""
    supabase_ready = bool(_get_supabase_client())
    return jsonify({
        "flask": True,
        "vercel": True,
        "security": {
            "limiter_enabled": bool(limiter),
            "ask_limit": RATE_LIMIT_ASK,
            "global_limits": RATE_LIMIT_DEFAULT,
        },
        "clerk": {
            "configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
            "enforced": CLERK_ENFORCE_AUTH,
        },
        "supabase": {
            "configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
            "publishable_configured": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
            "ready": supabase_ready,
            "prefs_table": SUPABASE_PREFS_TABLE,
        },
        "calendar": {
            "pyluach": True,
            "zmanim": True,
        },
        "external_apis": api_health.status_summary(),
        "reliability": DEVTOOLS_STATS,
    })


@routes_devtools.route("/api/devtools/heartbeat")
def devtools_heartbeat():
    """Low-noise diagnostics endpoint for inspector/devtools mode."""
    started = time.time()

    checks: dict[str, Any] = {
        "clerk_configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
        "supabase_service_ready": bool(_get_supabase_client()),
        "supabase_publishable_ready": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
    }

    from backend.sefaria_library import get_popular_texts
    popular_started = time.time()
    popular = get_popular_texts()
    checks["library_popular_ready"] = bool(popular)
    checks["library_popular_ms"] = int((time.time() - popular_started) * 1000)

    return jsonify({
        "ok": all(v for k, v in checks.items() if not k.endswith("_ms")),
        "ts": int(time.time()),
        "elapsed_ms": int((time.time() - started) * 1000),
        "checks": checks,
        "stats": DEVTOOLS_STATS,
    })


@routes_devtools.route("/api/devtools/reliability")
def devtools_reliability():
    return jsonify({
        "stats": DEVTOOLS_STATS,
        "ts": int(time.time()),
    })


@routes_devtools.route("/api/devtools/rls-audit")
def devtools_rls_audit():
    """Surface security posture for user-scoped Supabase table access."""
    has_supabase_token = bool(_extract_supabase_access_token())
    user_id = _get_request_user_id()
    return jsonify({
        "strict_rls": STRICT_SUPABASE_RLS,
        "tables": {
            "user_preferences": SUPABASE_PREFS_TABLE,
            "user_memories": SUPABASE_USER_MEMORIES_TABLE,
            "study_bookmarks": SUPABASE_STUDY_BOOKMARKS_TABLE,
        },
        "user": {
            "authenticated": bool(user_id),
            "user_id": user_id or None,
        },
        "auth": {
            "supabase_access_token_present": has_supabase_token,
            "request_scoped_client_ready": bool(_get_request_supabase_client()),
        },
        "requirement": "RLS policies should use auth.uid() = user_id for user tables.",
        "ts": int(time.time()),
    })


@routes_devtools.route("/api/client-errors", methods=["POST"])
def client_errors():
    payload = request.get_json(silent=True) or {}
    context = {
        "url": str(payload.get("url") or "")[:400],
        "stack": str(payload.get("stack") or "")[:8000],
        "component": str(payload.get("component") or "")[:120],
        "user_agent": (request.headers.get("User-Agent") or "")[:320],
        "ip": _extract_client_ip() or "",
    }
    _capture_backend_error("client_error_boundary", payload.get(
        "message") or "client_error", context)
    return jsonify({"ok": True})


@routes_devtools.route("/api/devtools/segment-report", methods=["POST"])
@maybe_require_clerk_auth
def report_segment_issue():
    payload = request.get_json(silent=True) or {}
    report = {
        "ts": int(time.time()),
        "kind": (payload.get("kind") or "segment").strip()[:60],
        "message": (payload.get("message") or "").strip()[:2000],
        "segment": (payload.get("segment") or "").strip()[:160],
        "ref": (payload.get("ref") or "").strip()[:200],
        "view_type": (payload.get("view_type") or "").strip()[:40],
        "view_value": (payload.get("view_value") or "").strip()[:200],
        "client": {
            "ua": (request.headers.get("User-Agent") or "")[:300],
            "ip": _extract_client_ip() or "",
        },
    }
    claims = getattr(g, "clerk_claims", {}) or {}
    if claims.get("sub"):
        report["user_id"] = claims.get("sub")

    app.logger.warning("SEGMENT_REPORT %s",
                       json.dumps(report, ensure_ascii=True))
    DEVTOOLS_STATS["segment_reports"] += 1
    return jsonify({"ok": True, "logged": True})


# ─── Backward-compat route alias ──────────────────────────────────────────────
@routes_devtools.route("/api/health")
def api_health_alias():
    """/api/health → /api/stack/health (backward compat)."""
    return stack_health()
