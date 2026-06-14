"""
User blueprint for Sh'elah.

User settings, profiles, bookmarks, todos, and Clerk authentication hooks
extracted verbatim from ``app.py`` (Stage 2 blueprint split). Logic is
unchanged; only the route decorator target moved from ``@app.route`` to
``@routes_user.route`` and shared helpers/constants are imported from ``app``
and ``backend``.
"""

from datetime import datetime
from uuid import uuid4

from flask import Blueprint, jsonify, request, g

from backend import claude
from backend.auth import _verify_clerk_token

from app import (
    app,
    require_clerk_auth,
    maybe_require_clerk_auth,
    STRICT_SUPABASE_RLS,
    SUPABASE_PREFS_TABLE,
    SUPABASE_STUDY_BOOKMARKS_TABLE,
    _get_request_user_id,
    _get_supabase_client,
    _get_user_scoped_supabase_client,
    _get_request_supabase_client,
    _extract_bearer_token,
    _capture_backend_error,
    _env_int,
)

routes_user = Blueprint("user", __name__)


@routes_user.route("/api/accept-legal", methods=["POST"])
@maybe_require_clerk_auth
def accept_legal():
    """Record that a user has accepted the Terms of Service and Privacy Policy."""
    user_id = _get_request_user_id()
    if not user_id:
        # Not authenticated — store acceptance in localStorage only (handled client-side).
        return jsonify({"success": True, "stored": "client"}), 200

    try:
        supabase_client = _get_supabase_client()
        if supabase_client:
            supabase_client.table("user_preferences").upsert(
                {
                    "clerk_id": user_id,
                    "legal_accepted": True,
                    "legal_accepted_at": datetime.utcnow().isoformat(),
                },
                on_conflict="clerk_id",
            ).execute()
    except Exception as e:
        app.logger.warning(
            "accept_legal: could not persist to Supabase: %s", e)

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


@routes_user.route("/api/user/preferences", methods=["GET", "PUT"])
@require_clerk_auth
def user_preferences():
    """Persist and fetch per-user UI preferences from Supabase."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = claims.get("sub")
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_user_scoped_supabase_client()
    if not supabase and not STRICT_SUPABASE_RLS:
        supabase = _get_supabase_client()
    if not supabase:
        if STRICT_SUPABASE_RLS:
            return jsonify({"error": "Supabase authenticated session required for RLS-protected preferences."}), 403
        return jsonify({"error": "Supabase not configured"}), 503

    table = supabase.table(SUPABASE_PREFS_TABLE)

    try:
        if request.method == "GET":
            result = table.select("prefs,updated_at").eq(
                "user_id", user_id).limit(1).execute()
            rows = result.data or []
            if not rows:
                return jsonify({
                    "prefs": None,
                    "shelf": None,
                    "notes": None,
                    "reading_state": None,
                    "updated_at": None,
                })

            record = rows[0]
            if not isinstance(record, dict):
                return jsonify({
                    "prefs": None,
                    "shelf": None,
                    "notes": None,
                    "reading_state": None,
                    "updated_at": None,
                })

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

        now_iso = datetime.utcnow().isoformat() + "Z"
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
    except Exception as e:
        _capture_backend_error("user_preferences_sync_failed", e, {
                               "user_id": str(user_id)})
        return jsonify({"error": "Failed to sync user preferences to the cloud."}), 500


@routes_user.route("/api/bookmarks/semantic", methods=["GET", "POST"])
@require_clerk_auth
def semantic_bookmarks():
    """Persist and retrieve semantic bookmarks with notes and AI summaries."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_user_scoped_supabase_client()
    if not supabase and not STRICT_SUPABASE_RLS:
        supabase = _get_supabase_client()
    if not supabase:
        if STRICT_SUPABASE_RLS:
            return jsonify({"error": "Supabase authenticated session required for RLS-protected bookmarks."}), 403
        return jsonify({"error": "Supabase not configured"}), 503

    table = supabase.table(SUPABASE_STUDY_BOOKMARKS_TABLE)
    ref = ""

    try:
        if request.method == "GET":
            query_limit = _env_int("SEMANTIC_BOOKMARK_LIMIT", 50)
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
            "created_at": datetime.utcnow().isoformat() + "Z",
        }

        table.insert(record).execute()
        return jsonify({
            "ok": True,
            "item": record,
            "summary_generated": bool(ai_summary),
            "summary_error": summary_error,
        })
    except Exception as e:
        _capture_backend_error("semantic_bookmark_failed", e, {
            "user_id": user_id,
            "ref": ref,
        })
        return jsonify({"error": "Failed to save semantic bookmark."}), 500


@routes_user.route("/api/todos")
def list_todos():
    """Flask equivalent of the Next.js server query for todos."""
    supabase = _get_request_supabase_client()
    if not supabase:
        return jsonify({
            "error": "Supabase publishable client is not configured",
            "hint": "Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY",
        }), 503

    try:
        result = supabase.from_("todos").select("id,name").execute()
        return jsonify({"todos": result.data or []})
    except Exception as e:
        message = str(e)
        if "PGRST205" in message or "Could not find the table 'public.todos'" in message:
            # Treat a missing optional table as an empty list so the UI keeps working.
            return jsonify({"todos": [], "warning": "Supabase todos table is not configured"})
        return jsonify({"error": f"Failed to load todos: {message}"}), 500


# ─── Backward-compat route alias ──────────────────────────────────────────────
@routes_user.route("/api/preferences", methods=["GET", "PUT"])
def api_preferences_alias():
    """/api/preferences → /api/user/preferences (backward compat)."""
    return user_preferences()
