"""
Public share links for stored AI answers (deep-link Phase 4).

An ask_history row is private to its owner (``/answer/<id>``, routes_user).
The owner can mint a public link for it here: ``POST`` sets an unguessable
``share_token`` and ``is_public``; anyone holding ``/a/<token>`` can then
read the answer through ``GET /api/public/answer/<token>`` without signing
in. ``DELETE`` revokes it -- the token is cleared, so the old link 404s for
good and sharing again mints a new one.

ask_history is service-role only (RLS on, no policies), so every read and
write goes through ``_get_supabase_client()``. Owner routes always filter on
the caller's own verified Clerk ``sub``; the public route selects only the
answer columns -- never ``user_id`` or the row ``id``.

The columns come from scripts/sql/migrate_ask_history_share.sql. Until the
operator runs it, PostgREST rejects the unknown columns and these routes
answer 503 ``{"code": "share_unavailable"}`` instead of 500.
"""

import re
import secrets
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify

from backend.auth import require_clerk_auth

from app import (
    SUPABASE_ASK_HISTORY_TABLE,
    _get_supabase_client,
    _capture_backend_error,
    hash_user_id,
)

routes_answer_share = Blueprint("answer_share", __name__)

_ENTRY_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# secrets.token_urlsafe(16) yields 22 chars; the range leaves room to grow.
_SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SHARE_TOKEN_BYTES = 16

# PostgREST codes for "the migration hasn't run": undefined column (42703),
# column missing from the schema cache (PGRST204), undefined table (42P01)
# and table missing from the schema cache (PGRST205).
_SCHEMA_MISSING_CODES = frozenset({"42703", "PGRST204", "42P01", "PGRST205"})

_PUBLIC_COLUMNS = "question,answer,sources,ai_cited_sources,community,mode,language,safety_class,created_at"

_ERR_NOT_FOUND = "Not found"


def _share_path(token: str) -> str:
    return f"/a/{token}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_schema_missing(exc: Exception) -> bool:
    return str(getattr(exc, "code", "") or "") in _SCHEMA_MISSING_CODES


def _share_unavailable():
    return jsonify({"error": "Sharing isn't available yet.", "code": "share_unavailable"}), 503


# Cache-Control needs nothing here: backend/cache_policy.py already stamps
# every /api/* response "private, no-store", so neither a CDN nor the
# browser keeps a copy of an answer whose link may be revoked.
def _noindex(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _owner_context(entry_id):
    """Resolve (user_id, supabase) for an owner route, or an error response."""
    user_id = str((getattr(g, "clerk_claims", {}) or {}).get("sub") or "").strip()
    if not user_id:
        return None, None, (jsonify({"error": "Missing user identity"}), 401)
    if not _ENTRY_ID_RE.match(str(entry_id or "")):
        return None, None, (jsonify({"error": _ERR_NOT_FOUND}), 404)
    supabase = _get_supabase_client()
    if not supabase:
        return None, None, (jsonify({"error": "Supabase not configured"}), 503)
    return user_id, supabase, None


def _owned_share_row(supabase, entry_id, user_id):
    result = (
        supabase
        .table(SUPABASE_ASK_HISTORY_TABLE)
        .select("share_token,is_public")
        .eq("id", str(entry_id))
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _share_state(row):
    token = str((row or {}).get("share_token") or "")
    if row and row.get("is_public") and token:
        return {"shared": True, "share_token": token, "path": _share_path(token)}
    return {"shared": False}


def _owner_error(event, exc, user_id, entry_id, message):
    if _is_schema_missing(exc):
        return _share_unavailable()
    _capture_backend_error(event, exc, {"user_id_hash": hash_user_id(user_id), "entry_id": entry_id})
    return jsonify({"error": message}), 500


@routes_answer_share.route("/api/user/history/<entry_id>/share", methods=["GET"])
@require_clerk_auth
def get_answer_share(entry_id):
    """The owner's share state for one stored answer."""
    user_id, supabase, error = _owner_context(entry_id)
    if error:
        return error
    try:
        row = _owned_share_row(supabase, entry_id, user_id)
    except Exception as e:
        return _owner_error("answer_share_get_failed", e, user_id, entry_id, "Failed to load share state")
    if row is None:
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    return jsonify(_share_state(row))


@routes_answer_share.route("/api/user/history/<entry_id>/share", methods=["POST"])
@require_clerk_auth
def create_answer_share(entry_id):
    """Make a stored answer public. Idempotent: an answer that is already
    shared keeps its token (200), so a link someone already copied never
    changes. Otherwise a new token is minted (201)."""
    user_id, supabase, error = _owner_context(entry_id)
    if error:
        return error
    try:
        row = _owned_share_row(supabase, entry_id, user_id)
        if row is None:
            return jsonify({"error": _ERR_NOT_FOUND}), 404
        state = _share_state(row)
        if state["shared"]:
            return jsonify(state)

        token = secrets.token_urlsafe(_SHARE_TOKEN_BYTES)
        # `.eq("is_public", False)` makes this a compare-and-set: if a
        # concurrent POST shared the row first, this update matches nothing
        # and the re-read below returns that request's token instead of
        # overwriting a link that may already have been copied.
        updated = (
            supabase
            .table(SUPABASE_ASK_HISTORY_TABLE)
            .update({"share_token": token, "is_public": True, "shared_at": _now_iso()})
            .eq("id", str(entry_id))
            .eq("user_id", user_id)
            .eq("is_public", False)
            .execute()
        )
        if updated.data:
            return jsonify(_share_state({"share_token": token, "is_public": True})), 201
        state = _share_state(_owned_share_row(supabase, entry_id, user_id))
        if state["shared"]:
            return jsonify(state)
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    except Exception as e:
        return _owner_error("answer_share_create_failed", e, user_id, entry_id, "Failed to share answer")


@routes_answer_share.route("/api/user/history/<entry_id>/share", methods=["DELETE"])
@require_clerk_auth
def revoke_answer_share(entry_id):
    """Stop sharing: clears the token, so the public link 404s from now on.
    The owner's own /answer/<id> link is untouched."""
    user_id, supabase, error = _owner_context(entry_id)
    if error:
        return error
    try:
        row = _owned_share_row(supabase, entry_id, user_id)
        if row is None:
            return jsonify({"error": _ERR_NOT_FOUND}), 404
        (
            supabase
            .table(SUPABASE_ASK_HISTORY_TABLE)
            .update({"share_token": None, "is_public": False, "share_revoked_at": _now_iso()})
            .eq("id", str(entry_id))
            .eq("user_id", user_id)
            .execute()
        )
        return jsonify({"shared": False})
    except Exception as e:
        return _owner_error("answer_share_revoke_failed", e, user_id, entry_id, "Failed to stop sharing")


@routes_answer_share.route("/api/public/answer/<token>", methods=["GET"])
def get_public_answer(token):
    """A shared answer, for anyone holding its link. Malformed, unknown,
    revoked and private tokens all answer the same 404, so the response
    never says whether a token once existed."""
    if not _SHARE_TOKEN_RE.match(str(token or "")):
        return _noindex(jsonify({"error": _ERR_NOT_FOUND})), 404
    supabase = _get_supabase_client()
    if not supabase:
        return _noindex(jsonify({"error": "Supabase not configured"})), 503
    try:
        result = (
            supabase
            .table(SUPABASE_ASK_HISTORY_TABLE)
            .select(_PUBLIC_COLUMNS)
            .eq("share_token", token)
            .eq("is_public", True)
            .limit(1)
            .execute()
        )
    except Exception as e:
        if _is_schema_missing(e):
            response, status = _share_unavailable()
            return _noindex(response), status
        _capture_backend_error("public_answer_get_failed", e, {})
        return _noindex(jsonify({"error": "Failed to load answer"})), 500

    rows = result.data or []
    if not rows:
        return _noindex(jsonify({"error": _ERR_NOT_FOUND})), 404
    row = rows[0]
    payload = {key: row.get(key) for key in _PUBLIC_COLUMNS.split(",") if key != "safety_class"}
    payload["meta"] = {
        "safety_class": row.get("safety_class") or "ok",
        "mode": row.get("mode"),
        "language": row.get("language"),
    }
    payload["public"] = True
    return _noindex(jsonify(payload))
