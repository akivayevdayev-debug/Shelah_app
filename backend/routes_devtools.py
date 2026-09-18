"""
Devtools / diagnostics blueprint for Sh'elah.

Stats, health, reliability, and debugging routes extracted verbatim from
``app.py`` (Stage 2 blueprint split). Logic is unchanged; only the route
decorator target moved from ``@app.route`` to ``@routes_devtools.route`` and the
shared helpers/constants are now imported from ``app`` and ``backend``.
"""

import asyncio
import hmac
import json
import os
import time
from typing import Any

from flask import Blueprint, jsonify, request, g

from backend import cost_meter, rate_limit
from backend.auth import CLERK_ENFORCE_AUTH, maybe_require_clerk_auth, require_clerk_auth
from backend.helpers import _is_same_origin_request

from app import (
    app,
    DEVTOOLS_STATS,
    CLERK_PUBLISHABLE_KEY,
    CLERK_JWT_ISSUER,
    SUPABASE_URL,
    SUPABASE_SECRET_KEY,
    SUPABASE_PUBLISHABLE_KEY,
    SUPABASE_PREFS_TABLE,
    SUPABASE_USER_MEMORIES_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE,
    SUPABASE_ASK_HISTORY_TABLE,
    SUPABASE_ANSWER_FEEDBACK_TABLE,
    STRICT_SUPABASE_RLS,
    api_health,
    _get_supabase_client,
    _get_request_supabase_client,
    _extract_supabase_access_token,
    _get_request_user_id,
    _extract_client_ip,
    _capture_backend_error,
)

routes_devtools = Blueprint("devtools", __name__)


@routes_devtools.route("/api/stack/health")
@require_clerk_auth
def stack_health():
    """Return runtime readiness for Bento stack components.

    Auth-gated (security audit P2): reveals rate-limit thresholds and
    Clerk/Supabase configuration state — unauthenticated reconnaissance
    value with no legitimate anonymous caller (not used by any frontend
    feature; scripts/verify_integrations.py, its one caller, treats a 401
    here as "the app is up and correctly protecting this route").
    """
    supabase_ready = bool(_get_supabase_client())
    # plan.md §16.8.1 fix: this used to report Flask-Limiter's own numbers,
    # which stopped being the limiter actually enforcing production /ask
    # traffic once asgi.py grew its own independent one (an operator reading
    # this endpoint got a confidently wrong answer). Now reports the one
    # real enforcement point (backend.rate_limit.RateLimitMiddleware).
    return jsonify({
        "flask": True,
        "vercel": True,
        "security": {
            "limiter_enabled": rate_limit.RATELIMIT_ENABLED,
            "limiter_store": "redis" if rate_limit.RATE_LIMIT_REDIS_URL else "in-memory (KNOWN GAP outside single-instance dev, plan.md §16.1 D3)",
            "policy": {
                cls: {"window_seconds": p.window_seconds, "max_requests": p.max_requests, "fail_open": p.fail_open}
                for cls, p in rate_limit._POLICIES.items()
            },
            "cost_breaker": (
                {"configured": True}
                if cost_meter._daily_budget_usd() > 0
                else {
                    "configured": False,
                    "note": "DAILY_BUDGET_USD unset -- global cost breaker and "
                    "budget-check cron are no-ops (plan.md §16 Phase 9b)",
                }
            ),
        },
        "clerk": {
            "configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
            "enforced": CLERK_ENFORCE_AUTH,
        },
        "supabase": {
            "configured": bool(SUPABASE_URL and SUPABASE_SECRET_KEY),
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


@routes_devtools.route("/api/devtools/feedback-digest", methods=["GET"])
@require_clerk_auth
def feedback_digest():
    """Recent answer-feedback rows (plan.md §12.4.3), newest first.

    Auth-gated like /api/stack/health: feedback comments are free-text
    supplied by readers (sanitized, but not meant for public display), so
    this has no legitimate anonymous caller.
    """
    limit = min(int(request.args.get("limit", 50) or 50), 200)
    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    try:
        result = (
            supabase.table(SUPABASE_ANSWER_FEEDBACK_TABLE)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        _capture_backend_error("feedback_digest_query_failed", e, {})
        return jsonify({"error": "Could not load feedback"}), 500

    helpful = sum(1 for r in rows if r.get("verdict") == "helpful")
    not_helpful = sum(1 for r in rows if r.get("verdict") == "not_helpful")
    return jsonify({
        "count": len(rows),
        "helpful": helpful,
        "not_helpful": not_helpful,
        "rows": rows,
    })


@routes_devtools.route("/api/devtools/heartbeat")
def devtools_heartbeat():
    """Low-noise diagnostics endpoint for inspector/devtools mode.

    Deliberately public/unauthenticated (security audit P2 note): unlike
    the other devtools/* routes, this one backs a real user-facing feature
    (templates/index.html's hidden devtools-inspector panel — Alt+Shift+I
    or shift-double-click the brand logo — reachable by any site visitor,
    not just logged-in ones). Configuration-presence booleans (is Clerk/
    Supabase configured) are computed for the internal `ok` rollup but
    deliberately excluded from the response body so this doesn't leak
    deployment configuration to an unauthenticated caller the way
    /api/stack/health used to.
    """
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

    ok = all(v for k, v in checks.items() if not k.endswith("_ms"))
    public_checks = {
        "library_popular_ready": checks["library_popular_ready"],
        "library_popular_ms": checks["library_popular_ms"],
    }

    return jsonify({
        "ok": ok,
        "ts": int(time.time()),
        "elapsed_ms": int((time.time() - started) * 1000),
        "checks": public_checks,
        "stats": DEVTOOLS_STATS,
    })


@routes_devtools.route("/api/devtools/reliability")
@require_clerk_auth
def devtools_reliability():
    """Auth-gated (security audit P2): not called by any frontend feature
    (the public devtools-inspector panel uses /api/devtools/heartbeat
    instead), so gating here is zero-regression."""
    return jsonify({
        "stats": DEVTOOLS_STATS,
        "ts": int(time.time()),
    })


# plan.md §21/§21.2.2 STEP 5: which tables an owner-scoped RLS policy
# actually governs today. ask_history is deliberately excluded -- its
# policy was dropped 2026-08-31 (STEP 6a): routes_user.py's history
# endpoints only ever read it through the service-role client, so a
# per-user policy on it was dead weight, not a real second gate.
_RLS_OBSERVED_TABLES = {
    "user_preferences": lambda: SUPABASE_PREFS_TABLE,
    "user_memories": lambda: SUPABASE_USER_MEMORIES_TABLE,
    "study_bookmarks": lambda: SUPABASE_STUDY_BOOKMARKS_TABLE,
}


def _observe_rls_row_counts(user_id):
    """Compare a user-scoped-client row count against a service-role-client
    row count for the CALLER'S OWN rows, on each RLS-relevant table.

    This is the "observed result of a real user-scoped query" plan.md
    §21.2.2 STEP 5 asks for, not just policy presence. It catches the
    specific silent failure §21.1 documents: if auth.uid() doesn't resolve,
    every RLS policy evaluates false and the user-scoped client returns
    zero rows for a user who actually has data -- not an error, not a 403,
    just an empty result indistinguishable from "no data yet" until you
    compare it against the service-role client's ground truth, which is
    exactly what this does. It does NOT test cross-user isolation (does
    RLS block OTHER users' rows) -- that needs a second real test user's
    token, which this endpoint (bound to the single caller's own session)
    can't produce; that half is scripts/verify_rls.py's job.
    """
    service_client = _get_supabase_client()
    scoped_client = _get_request_supabase_client()
    observed = {}
    for key, table_name_fn in _RLS_OBSERVED_TABLES.items():
        table_name = table_name_fn()
        if not service_client or not scoped_client:
            observed[key] = {"observed": False, "reason": "client_unavailable"}
            continue
        try:
            service_count = (
                service_client.table(table_name)
                .select("user_id", count="exact")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            ).count or 0
            scoped_count = (
                scoped_client.table(table_name)
                .select("user_id", count="exact")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            ).count or 0
            observed[key] = {
                "observed": True,
                "service_role_count": service_count,
                "user_scoped_count": scoped_count,
                "matches": service_count == scoped_count,
            }
        except Exception as e:
            _capture_backend_error(
                "rls_audit_observed_query_failed", e, {"table": table_name})
            observed[key] = {"observed": False, "reason": "query_failed"}
    return observed


@routes_devtools.route("/api/devtools/rls-audit")
@require_clerk_auth
def devtools_rls_audit():
    """Surface security posture for user-scoped Supabase table access.

    Auth-gated (security audit P2): this endpoint's whole purpose is
    introspecting the *caller's own* auth/RLS posture, so requiring the
    caller to actually be authenticated to see "am I authenticated" both
    matches the audit's fix and removes the unauthenticated-recon concern
    (RLS strict-mode flag, whether a Supabase token was present) entirely.
    """
    has_supabase_token = bool(_extract_supabase_access_token())
    user_id = _get_request_user_id()
    # plan.md §21.2.2 STEP 5: only attempt the observed-query comparison when
    # there's a real signed-in caller with a Supabase-bearing token -- an
    # anonymous or tokenless request has no "own rows" to compare.
    observed = _observe_rls_row_counts(
        user_id) if (has_supabase_token and user_id) else {}
    return jsonify({
        "strict_rls": STRICT_SUPABASE_RLS,
        "tables": {
            "user_preferences": SUPABASE_PREFS_TABLE,
            "user_memories": SUPABASE_USER_MEMORIES_TABLE,
            "study_bookmarks": SUPABASE_STUDY_BOOKMARKS_TABLE,
            # Service-role-only by design (plan.md §21 STEP 6a, 2026-08-31)
            # -- no RLS policy governs this table's access anymore; listed
            # here for completeness, not as an RLS-coverage gap.
            "ask_history": SUPABASE_ASK_HISTORY_TABLE,
        },
        "user": {
            "authenticated": bool(user_id),
            "user_id": user_id or None,
        },
        "auth": {
            "supabase_access_token_present": has_supabase_token,
            "request_scoped_client_ready": bool(_get_request_supabase_client()),
        },
        "observed": observed,
        "requirement": "RLS policies should use auth.uid() = user_id for user tables.",
        "ts": int(time.time()),
    })


# plan.md §16.2/§16.4: unauthenticated, unrate-limited, and (until this
# hardening) forwarding an attacker-controlled 8 KB stack straight to
# Sentry — ~5,000 forged POSTs blind error monitoring for the rest of the
# free-tier month. Cap kept well below the old 8000-char ceiling.
_CLIENT_ERROR_STACK_MAX_CHARS = 2000


@routes_devtools.route("/api/client-errors", methods=["POST"])
def client_errors():
    if not _is_same_origin_request():
        return jsonify({"error": "forbidden"}), 403

    payload = request.get_json(silent=True) or {}
    context = {
        "url": str(payload.get("url") or "")[:400],
        "stack": str(payload.get("stack") or "")[:_CLIENT_ERROR_STACK_MAX_CHARS],
        "component": str(payload.get("component") or "")[:120],
        "user_agent": (request.headers.get("User-Agent") or "")[:320],
        # _extract_client_ip() intentionally omitted even though plan.md
        # §16.1 D2 (spoofable CF-Connecting-IP) is fixed as of this commit:
        # sending a raw IP to a third-party error tracker is a separate
        # privacy call (§16.4 / §8.D — hash before logging, never log raw),
        # not something to flip on as a side effect of the spoofing fix.
    }
    _capture_backend_error("client_error_boundary", payload.get(
        "message") or "client_error", context)
    return jsonify({"ok": True})


@routes_devtools.route("/api/devtools/segment-report", methods=["POST"])
@maybe_require_clerk_auth
def report_segment_issue():
    payload = request.get_json(silent=True) or {}
    # plan.md §8.C.5 security-audit pass: a non-string JSON value for any of
    # these fields (e.g. {"kind": 1}) used to short-circuit the `or` fallback
    # and crash `.strip()` with an unhandled AttributeError -> 500. str()
    # first, matching the pattern client_errors() already uses above.
    report = {
        "ts": int(time.time()),
        "kind": str(payload.get("kind") or "segment").strip()[:60],
        "message": str(payload.get("message") or "").strip()[:2000],
        "segment": str(payload.get("segment") or "").strip()[:160],
        "ref": str(payload.get("ref") or "").strip()[:200],
        "view_type": str(payload.get("view_type") or "").strip()[:40],
        "view_value": str(payload.get("view_value") or "").strip()[:200],
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
@require_clerk_auth
def api_health_alias():
    """/api/health → /api/stack/health (backward compat). Auth-gated to
    match stack_health() (security audit P2) — stated directly here too,
    not just inherited via the wrapped stack_health() call, so the gating
    is obvious to a reader without tracing the indirection."""
    return stack_health()


@routes_devtools.route("/api/devtools/budget-check", methods=["GET"])
def budget_check():
    """Daily AI-spend guardrail — intended to be triggered by Vercel Cron.

    Gated by CRON_SECRET, which must be configured for this route to run
    at all (security audit P3: fail closed, not open, if CRON_SECRET is
    simply unset — a misconfigured deployment must not silently drop to
    "anyone can trigger this"). Vercel automatically sends CRON_SECRET as a
    Bearer token on Cron-triggered requests once the env var is set on the
    project. No-op (configured=False) until an operator also sets
    DAILY_BUDGET_USD (see cost_meter.py).
    """
    cron_secret = (os.environ.get("CRON_SECRET") or "").strip()
    if not cron_secret:
        return jsonify({"error": "CRON_SECRET is not configured"}), 503

    provided = (request.headers.get("Authorization") or "").strip()
    expected = f"Bearer {cron_secret}"
    if not hmac.compare_digest(provided, expected):
        return jsonify({"error": "unauthorized"}), 401

    result = asyncio.run(cost_meter.check_daily_budget_and_alert())
    return jsonify(result)
