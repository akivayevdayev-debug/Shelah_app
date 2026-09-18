"""
User blueprint for Sh'elah.

User settings, profiles, bookmarks, todos, and Clerk authentication hooks
extracted verbatim from ``app.py`` (Stage 2 blueprint split). Logic is
unchanged; only the route decorator target moved from ``@app.route`` to
``@routes_user.route`` and shared helpers/constants are imported from ``app``
and ``backend``.
"""

from datetime import datetime, timezone
from uuid import uuid4

from flask import Blueprint, jsonify, request, g

from backend import claude
from backend.auth import (
    _verify_clerk_token,
    _extract_bearer_token,
    require_clerk_auth,
    maybe_require_clerk_auth,
)

from app import (
    STRICT_SUPABASE_RLS,
    SUPABASE_PREFS_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE,
    SUPABASE_ASK_HISTORY_TABLE,
    LEGAL_TERMS_VERSION,
    LEGAL_PRIVACY_VERSION,
    _get_request_user_id,
    _get_supabase_client,
    _get_user_scoped_supabase_client,
    _capture_backend_error,
    hash_user_id,
)

routes_user = Blueprint("user", __name__)

_ERR_MISSING_USER_IDENTITY = "Missing user identity"
_ERR_SUPABASE_NOT_CONFIGURED = "Supabase not configured"


@routes_user.route("/api/accept-legal", methods=["POST"])
@maybe_require_clerk_auth
def accept_legal():
    """Record that a user has accepted the Terms of Service and Privacy
    Policy, and (plan.md §8.B-AGE.6) their 13+/16+ age attestation. Absence
    of an explicit ``age_attested: true`` in the request body is treated as
    not attested — the frontend gate blocks continued use until the
    checkbox is checked, so this is a real barrier, not a nudge.

    plan.md §8.A.1/§8.D.2: also records which document *version* the user
    accepted (``terms_version``/``privacy_version`` from the consent
    modal's payload, defaulting to the server's current constants if the
    client omits them). This is what makes "re-prompt on material version
    change" real — the consent modal keys its localStorage flag off the
    same LEGAL_TERMS_VERSION/LEGAL_PRIVACY_VERSION constants, so bumping
    either constant both re-shows the modal and produces a distinguishable
    acceptance record for the new version."""
    payload = request.get_json(silent=True) or {}
    age_attested = bool(payload.get("age_attested"))
    terms_version = str(payload.get("terms_version") or LEGAL_TERMS_VERSION)
    privacy_version = str(
        payload.get("privacy_version") or LEGAL_PRIVACY_VERSION)

    user_id = _get_request_user_id()
    if not user_id:
        # Not authenticated — store acceptance in localStorage only (handled client-side).
        return jsonify({"success": True, "stored": "client"}), 200

    try:
        supabase_client = _get_supabase_client()
        if supabase_client:
            record = {
                # plan.md §8.D bug fix: user_preferences' actual primary key
                # is `user_id` (scripts/sql/bookmarks_and_preferences_setup.sql)
                # -- there is no `clerk_id` column anywhere in the schema, so
                # the previous on_conflict="clerk_id" upsert was rejected by
                # PostgREST on every call and silently swallowed by the
                # except below. Consent records were never actually being
                # persisted to Supabase until this fix.
                "user_id": user_id,
                "legal_accepted": True,
                "legal_accepted_at": datetime.now(timezone.utc).isoformat(),
                "legal_terms_version": terms_version,
                "legal_privacy_version": privacy_version,
            }
            if age_attested:
                record["age_attested"] = True
                record["age_attested_at"] = datetime.now(
                    timezone.utc).isoformat()
            supabase_client.table("user_preferences").upsert(
                record,
                on_conflict="user_id",
            ).execute()
    except Exception as e:
        # plan.md §23.2.4: this is the exact function whose prior
        # on_conflict="clerk_id" bug (see the comment above) silently
        # dropped every legal-consent write — a bare app.logger.warning()
        # is the same swallow shape that hid it, just spelled differently.
        # Keep the write best-effort, but route the failure through
        # _capture_backend_error() so it reaches Sentry/structured logs
        # instead of a log line nobody watches.
        _capture_backend_error("accept_legal_persist_failed", e, {"user_id_hash": hash_user_id(user_id)})

    return jsonify({"success": True, "stored": "server"}), 200


@routes_user.route("/api/auth/me")
def clerk_auth_me():
    """Returns Clerk auth status and a minimal user payload."""
    token = _extract_bearer_token()
    if not token:
        return jsonify({"authenticated": False})

    try:
        claims = _verify_clerk_token(token)
        return jsonify({
            "authenticated": True,
            "user_id": claims.get("sub"),
            "session_id": claims.get("sid"),
        })
    except Exception:
        return jsonify({"authenticated": False}), 401


_EMPTY_USER_PREFS_RESPONSE = {
    "prefs": None,
    "shelf": None,
    "notes": None,
    "reading_state": None,
    "updated_at": None,
}


def _user_preferences_get_response(table, user_id):
    """GET branch of user_preferences(): fetch + normalize the stored prefs
    shape. Split out to keep this branch out of that route's own complexity
    count (SonarCloud python:S3776).
    """
    result = table.select("prefs,updated_at").eq(
        "user_id", user_id).limit(1).execute()
    rows = result.data or []
    if not rows:
        return jsonify(_EMPTY_USER_PREFS_RESPONSE)

    record = rows[0]
    if not isinstance(record, dict):
        return jsonify(_EMPTY_USER_PREFS_RESPONSE)

    stored = record.get("prefs")
    prefs = None
    shelf = None
    notes = None
    reading_state = None
    if isinstance(stored, dict):
        if any(key in stored for key in ("prefs", "shelf", "notes", "reading_state")):
            prefs = stored.get("prefs") if isinstance(
                stored.get("prefs"), dict) else None
            shelf = stored.get("shelf") if isinstance(
                stored.get("shelf"), dict) else None
            notes = stored.get("notes") if isinstance(
                stored.get("notes"), dict) else None
            reading_state = stored.get("reading_state") if isinstance(
                stored.get("reading_state"), dict) else None
        else:
            # Legacy shape where prefs JSON was stored directly.
            prefs = stored

    return jsonify({
        "prefs": prefs,
        "shelf": shelf,
        "notes": notes,
        "reading_state": reading_state,
        "updated_at": record.get("updated_at"),
    })


def _user_preferences_put_response(table, user_id):
    """PUT branch of user_preferences(): validate + upsert the prefs
    payload. Split out to keep this branch out of that route's own
    complexity count (SonarCloud python:S3776).
    """
    payload = request.get_json(silent=True) or {}
    prefs = payload.get("prefs")
    if not isinstance(prefs, dict):
        return jsonify({"error": "prefs must be an object"}), 400

    shelf = payload.get("shelf")
    notes = payload.get("notes")
    reading_state = payload.get("reading_state")

    if shelf is None:
        shelf = {}
    if notes is None:
        notes = {}
    if reading_state is None:
        reading_state = {}

    if not isinstance(shelf, dict):
        return jsonify({"error": "shelf must be an object"}), 400
    if not isinstance(notes, dict):
        return jsonify({"error": "notes must be an object"}), 400
    if not isinstance(reading_state, dict):
        return jsonify({"error": "reading_state must be an object"}), 400

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    stored_payload = {
        "prefs": prefs,
        "shelf": shelf,
        "notes": notes,
        "reading_state": reading_state,
    }
    upsert_payload = {
        "user_id": user_id,
        "prefs": stored_payload,
        "updated_at": now_iso,
    }
    table.upsert(upsert_payload).execute()
    return jsonify({"ok": True, "updated_at": now_iso})


@routes_user.route("/api/user/preferences", methods=["GET", "PUT"])
@require_clerk_auth
def user_preferences():
    """Persist and fetch per-user UI preferences from Supabase."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = claims.get("sub")
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase = _get_user_scoped_supabase_client()
    if not supabase and not STRICT_SUPABASE_RLS:
        supabase = _get_supabase_client()
    if not supabase:
        if STRICT_SUPABASE_RLS:
            return jsonify({"error": "Supabase authenticated session required for RLS-protected preferences."}), 403
        return jsonify({"error": _ERR_SUPABASE_NOT_CONFIGURED}), 503

    table = supabase.table(SUPABASE_PREFS_TABLE)

    try:
        if request.method == "GET":
            return _user_preferences_get_response(table, user_id)
        return _user_preferences_put_response(table, user_id)
    except Exception as e:
        _capture_backend_error("user_preferences_sync_failed", e, {
                               "user_id_hash": hash_user_id(user_id)})
        return jsonify({"error": "Failed to sync user preferences to the cloud."}), 500


def _semantic_bookmark_create_response(table, user_id, payload, ref):
    """POST branch of semantic_bookmarks(): validate the payload, optionally
    AI-summarize, and insert the new bookmark record. `ref` is computed by
    the caller (kept there so the except handler still has it for error
    context). Split out to keep this branch out of that route's own
    complexity count (SonarCloud python:S3776).
    """
    label = str(payload.get("label") or ref).strip()[:260]
    segment_text = str(payload.get("segment_text") or "").strip()[:6000]
    notes = str(payload.get("notes") or "").strip()[:3000]
    ai_summary = str(payload.get("ai_summary") or "").strip()[:3000]

    if not ref and not segment_text:
        return jsonify({"error": "A reference or text segment is required."}), 400

    summary_error = ""
    if not ai_summary and segment_text:
        summary_result = claude.summarize_with_gemini(
            segment_text, notes=notes)
        ai_summary = str(summary_result.get(
            "summary") or "").strip()[:3000]
        summary_error = str(summary_result.get("error") or "").strip()

    record = {
        "id": str(uuid4()),
        "user_id": user_id,
        "ref": ref,
        "label": label,
        "segment_text": segment_text,
        "ai_summary": ai_summary,
        "notes": notes,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    table.insert(record).execute()
    return jsonify({
        "ok": True,
        "item": record,
        "summary_generated": bool(ai_summary),
        "summary_error": summary_error,
    })


@routes_user.route("/api/bookmarks/semantic", methods=["GET", "POST"])
@require_clerk_auth
def semantic_bookmarks():
    """Persist and retrieve semantic bookmarks with notes and AI summaries."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase = _get_user_scoped_supabase_client()
    if not supabase and not STRICT_SUPABASE_RLS:
        supabase = _get_supabase_client()
    if not supabase:
        if STRICT_SUPABASE_RLS:
            return jsonify({"error": "Supabase authenticated session required for RLS-protected bookmarks."}), 403
        return jsonify({"error": _ERR_SUPABASE_NOT_CONFIGURED}), 503

    table = supabase.table(SUPABASE_STUDY_BOOKMARKS_TABLE)
    ref = ""

    try:
        if request.method == "GET":
            query_limit = 50
            result = (
                table
                .select("id,ref,label,segment_text,ai_summary,notes,created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .limit(max(1, min(query_limit, 200)))
                .execute()
            )
            return jsonify({"items": result.data or []})

        payload = request.get_json(silent=True) or {}
        ref = str(payload.get("ref") or "").strip()[:260]
        return _semantic_bookmark_create_response(table, user_id, payload, ref)
    except Exception as e:
        _capture_backend_error("semantic_bookmark_failed", e, {
            "user_id_hash": hash_user_id(user_id),
            "ref": ref,
        })
        return jsonify({"error": "Failed to save semantic bookmark."}), 500


# ─── Backward-compat route alias ──────────────────────────────────────────────
@routes_user.route("/api/preferences", methods=["GET", "PUT"])
def api_preferences_alias():
    """/api/preferences → /api/user/preferences (backward compat)."""
    return user_preferences()


# ─── Ask history ───────────────────────────────────────────────────────────────

@routes_user.route("/api/user/history", methods=["GET"])
@require_clerk_auth
def get_ask_history():
    """Return the signed-in user's ask history, newest first."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": _ERR_SUPABASE_NOT_CONFIGURED}), 503

    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
        result = (
            supabase
            .table(SUPABASE_ASK_HISTORY_TABLE)
            .select("id,question,answer,sources,ai_cited_sources,community,mode,language,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"items": result.data or []})
    except Exception as e:
        _capture_backend_error("ask_history_fetch_failed", e, {"user_id_hash": hash_user_id(user_id)})
        return jsonify({"error": "Failed to load history"}), 500


@routes_user.route("/api/user/history/<entry_id>", methods=["DELETE"])
@require_clerk_auth
def delete_ask_history_entry(entry_id):
    """Delete a single history entry owned by the signed-in user."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": _ERR_SUPABASE_NOT_CONFIGURED}), 503

    try:
        supabase.table(SUPABASE_ASK_HISTORY_TABLE).delete().eq(
            "id", str(entry_id)
        ).eq("user_id", user_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        _capture_backend_error("ask_history_delete_failed", e, {"user_id_hash": hash_user_id(user_id), "entry_id": entry_id})
        return jsonify({"error": "Failed to delete history entry"}), 500
