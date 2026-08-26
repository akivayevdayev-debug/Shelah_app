"""
Privacy-operations blueprint for Sh'elah (plan.md §8.D).

Self-serve data-subject request (DSR) flow — "download my data" / "delete
my account + data" — plus the scheduled retention-enforcement job that
actually deletes data past the windows documented in templates/privacy.html
§3. New routes belong in backend/ per .agents/ENGINEERING_RULES.md.

Deletion/export deliberately use the service-role Supabase client with an
explicit `.eq("user_id", ...)` filter on every query, the same pattern
already used by backend/routes_user.py's get_ask_history()/
delete_ask_history_entry(), rather than the RLS-scoped
_get_user_scoped_supabase_client(). That user-scoped client only works when
the caller's bearer token is a Supabase-compatible JWT (a Clerk JWT minted
from the "supabase" token template) — the frontend's authHeaders() sends a
plain Clerk session token, so gating a DSR endpoint behind that would 403
for exactly the users this feature exists to serve. user_memories' RLS
policy blocks all client-side access outright (scripts/sql/
rag_identity_cache_setup.sql), so a service-role read/delete is required
there regardless.
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timedelta, timezone

import httpx
from flask import Blueprint, g, jsonify, request

from backend.auth import require_clerk_auth
from backend.cost_meter import expire_stale_budget_reservations

from app import (
    SUPABASE_PREFS_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE,
    SUPABASE_ASK_HISTORY_TABLE,
    SUPABASE_USER_MEMORIES_TABLE,
    _get_supabase_client,
    _capture_backend_error,
)

routes_privacy = Blueprint("privacy", __name__)

# cost_meter.py inserts into this table directly by string literal (no
# SUPABASE_*_TABLE constant exists for it yet) -- matching that here rather
# than introducing a constant only this module would use.
_AI_USAGE_LOG_TABLE = "ai_usage_log"

# (export key, table name) -- every table here is keyed by a `user_id`
# text column holding the Clerk `sub` claim.
_USER_DATA_TABLES = (
    ("preferences", SUPABASE_PREFS_TABLE),
    ("bookmarks", SUPABASE_STUDY_BOOKMARKS_TABLE),
    ("ask_history", SUPABASE_ASK_HISTORY_TABLE),
    ("memories", SUPABASE_USER_MEMORIES_TABLE),
    ("ai_usage_log", _AI_USAGE_LOG_TABLE),
)

# plan.md §8.D retention windows enforced by the scheduled job below. The
# other rows in that table (account info/inactivity, bookmarks, consent
# records, session cookies, Vercel-side security logs) are either
# user-controlled, defensibility records kept intentionally, or owned by a
# provider outside this app's Supabase project -- see docs/PRIVACY_OPERATIONS.md
# for why each of those is out of scope for this job.
_ASK_HISTORY_RETENTION_DAYS = 90
_AI_USAGE_LOG_RETENTION_DAYS = 90

# Matches backend/cost_meter.py's _fetch_today_usage_rows() pagination --
# PostgREST applies a default max-rows cap per request (commonly 1000), so
# a plain .select("*") would silently truncate the export for any table
# with more rows than that cap (ai_usage_log/ask_history can both realistically
# exceed it for a long-lived active user).
_EXPORT_PAGE_SIZE = 1000
_EXPORT_MAX_PAGES = 200

# Generic message returned to the client on a table failure -- the raw
# exception text is same-user-only exposure, not a cross-user leak, but
# still an unnecessary internal-detail disclosure; full detail goes to
# _capture_backend_error server-side instead.
_GENERIC_TABLE_ERROR = "This data category could not be read. Please try again or contact us."
_GENERIC_DELETE_ERROR = "This data category could not be deleted. Please try again or contact us."


def _export_table_rows(supabase, table_name, user_id):
    """Best-effort fetch of one table's rows for the export bundle. A single
    table failing (e.g. not provisioned in this deployment) must not sink
    the whole export -- returns (rows, error_message). Paginates via
    .range() so a high-volume table isn't silently truncated."""
    try:
        rows = []
        offset = 0
        for _ in range(_EXPORT_MAX_PAGES):
            result = (
                supabase.table(table_name)
                .select("*")
                .eq("user_id", user_id)
                .range(offset, offset + _EXPORT_PAGE_SIZE - 1)
                .execute()
            )
            page = result.data if isinstance(result.data, list) else []
            rows.extend(page)
            if len(page) < _EXPORT_PAGE_SIZE:
                break
            offset += _EXPORT_PAGE_SIZE
        return rows, None
    except Exception as e:
        _capture_backend_error("data_export_table_failed", e, {
            "user_id": user_id, "table": table_name,
        })
        return [], _GENERIC_TABLE_ERROR


@routes_privacy.route("/api/user/data-export", methods=["GET"])
@require_clerk_auth
def export_user_data():
    """plan.md §8.D.1: self-serve "download my data" -- a JSON export of
    every Supabase row belonging to the signed-in user, across every
    user-scoped table this app writes to."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    data = {}
    errors = {}
    for export_key, table_name in _USER_DATA_TABLES:
        rows, error = _export_table_rows(supabase, table_name, user_id)
        data[export_key] = rows
        if error:
            errors[export_key] = error

    response = {
        "user_id": user_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    if errors:
        response["partial_errors"] = errors
    return jsonify(response)


def _delete_table_rows(supabase, table_name, user_id):
    """Best-effort delete of one table's rows for this user. Delete is
    naturally idempotent (deleting zero remaining rows is not an error), so
    a retried delete-account call after a partial prior failure is safe."""
    try:
        supabase.table(table_name).delete().eq("user_id", user_id).execute()
        return True, None
    except Exception as e:
        _capture_backend_error("account_delete_table_failed", e, {
            "user_id": user_id, "table": table_name,
        })
        return False, _GENERIC_DELETE_ERROR


async def _delete_clerk_user_async(user_id, secret_key):
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.delete(
            f"https://api.clerk.com/v1/users/{user_id}",
            headers={"Authorization": f"Bearer {secret_key}"},
        )
    # 404 -- the Clerk user is already gone -- is treated as success so a
    # retried delete-account call (e.g. after a prior partial failure) is
    # idempotent rather than erroring forever.
    if response.status_code not in (200, 202, 204, 404):
        raise RuntimeError(
            f"Clerk delete-user returned {response.status_code}: {response.text[:300]}")


def _delete_clerk_user(user_id):
    """Sync wrapper (asyncio.run), matching the existing
    routes_devtools.budget_check() pattern for calling async code from a
    sync Flask view. Returns (deleted: bool, error: str | None)."""
    secret_key = (os.environ.get("CLERK_SECRET_KEY") or "").strip()
    if not secret_key:
        # Without this, an unset CLERK_SECRET_KEY silently downgrades a
        # GDPR-relevant delete-account request to a partial success (Supabase
        # rows gone, Clerk identity retained) with zero operator-facing
        # signal beyond the `clerk_error` field in the HTTP response body,
        # which nothing in production actually watches. Capture it exactly
        # like the genuine-failure branch below so it reaches Sentry/logs.
        _capture_backend_error(
            "clerk_account_delete_skipped_no_secret_key",
            RuntimeError("CLERK_SECRET_KEY not configured"),
            {"user_id": user_id},
        )
        return False, "CLERK_SECRET_KEY not configured"

    import asyncio
    try:
        asyncio.run(_delete_clerk_user_async(user_id, secret_key))
        return True, None
    except Exception as e:
        _capture_backend_error(
            "clerk_account_delete_failed", e, {"user_id": user_id})
        return False, str(e)


_DELETE_CONFIRMATION_PHRASE = "DELETE"


@routes_privacy.route("/api/user/delete-account", methods=["POST"])
@require_clerk_auth
def delete_account():
    """plan.md §8.D.1: self-serve "delete my account + data". Cascades
    through every Supabase table this app writes to for the user, then
    deletes the Clerk identity itself via Clerk's Backend API.

    Supabase rows are deleted first, Clerk identity last, and the Clerk
    call only happens if every table delete succeeded: the Clerk identity
    is this user's only way to authenticate and retry, so deleting it while
    Supabase rows remain would orphan that data permanently with no
    self-serve recovery path. If any table delete fails, the Clerk account
    is deliberately left intact and the response says so, so the client
    can prompt the user to retry while still signed in.
    """
    payload = request.get_json(silent=True) or {}
    if str(payload.get("confirmation") or "").strip() != _DELETE_CONFIRMATION_PHRASE:
        return jsonify({
            "error": f'Send {{"confirmation": "{_DELETE_CONFIRMATION_PHRASE}"}} to confirm this irreversible action.',
        }), 400

    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    deleted = {}
    table_errors = {}
    for _export_key, table_name in _USER_DATA_TABLES:
        ok, error = _delete_table_rows(supabase, table_name, user_id)
        deleted[table_name] = ok
        if error:
            table_errors[table_name] = error

    response = {
        "ok": False,
        "deleted_tables": deleted,
        "clerk_deleted": False,
    }

    if table_errors:
        # At least one table failed to wipe -- do NOT delete the Clerk
        # identity. Deleting it here would strand the user signed out with
        # no way to retry, while their remaining data sits undeleted
        # forever. Leave the account intact so the client can prompt a retry.
        response["table_errors"] = table_errors
        response["clerk_skipped"] = (
            "Clerk account was not deleted because one or more data "
            "tables failed to delete. Please retry."
        )
        return jsonify(response), 207

    clerk_deleted, clerk_error = _delete_clerk_user(user_id)
    response["ok"] = clerk_deleted
    response["clerk_deleted"] = clerk_deleted
    if clerk_error:
        response["clerk_error"] = clerk_error
    return jsonify(response), 200 if clerk_deleted else 207


def _delete_rows_older_than(supabase, table_name, days):
    cutoff = (datetime.now(timezone.utc) -
              timedelta(days=days)).isoformat()
    result = (
        supabase.table(table_name)
        .delete()
        .lt("created_at", cutoff)
        .execute()
    )
    return len(result.data or []), cutoff


@routes_privacy.route("/api/devtools/retention-enforce", methods=["GET"])
def retention_enforce():
    """plan.md §8.D.5: scheduled job (Vercel Cron, see vercel.json) that
    actually deletes data past the retention windows in templates/
    privacy.html §3, rather than leaving that a policy-only promise.

    Gated by CRON_SECRET exactly like routes_devtools.budget_check() --
    fails closed if unset, so a misconfigured deployment can't be triggered
    by an unauthenticated caller.
    """
    cron_secret = (os.environ.get("CRON_SECRET") or "").strip()
    if not cron_secret:
        return jsonify({"error": "CRON_SECRET is not configured"}), 503

    provided = (request.headers.get("Authorization") or "").strip()
    expected = f"Bearer {cron_secret}"
    if not hmac.compare_digest(provided, expected):
        return jsonify({"error": "unauthorized"}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    result = {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        deleted, cutoff = _delete_rows_older_than(
            supabase, SUPABASE_ASK_HISTORY_TABLE, _ASK_HISTORY_RETENTION_DAYS)
        result["ask_history"] = {"deleted": deleted, "cutoff": cutoff}
    except Exception as e:
        _capture_backend_error("retention_enforce_ask_history_failed", e, {})
        result["ok"] = False
        result["ask_history"] = {"error": str(e)}

    try:
        deleted, cutoff = _delete_rows_older_than(
            supabase, _AI_USAGE_LOG_TABLE, _AI_USAGE_LOG_RETENTION_DAYS)
        result["ai_usage_log"] = {"deleted": deleted, "cutoff": cutoff}
    except Exception as e:
        _capture_backend_error("retention_enforce_ai_usage_log_failed", e, {})
        result["ok"] = False
        result["ai_usage_log"] = {"error": str(e)}

    # plan.md §20.2 Phase 20b, reservation lifecycle step 3: sweep abandoned
    # atomic-budget reservations (process died between reserve and settle)
    # into this existing daily cron rather than standing up a new job.
    reservation_result = expire_stale_budget_reservations()
    result["budget_reservations"] = reservation_result
    if reservation_result.get("error"):
        result["ok"] = False

    return jsonify(result), 200 if result["ok"] else 500
