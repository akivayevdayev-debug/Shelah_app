"""
Answer-feedback blueprint for Sh'elah (plan.md §12.4).

A single write-only endpoint: readers leave a thumbs up/down (and an
optional short comment) on an AI answer. Feedback is accepted from
signed-out readers too (nullable user_id), matching the accept_legal()
optional-auth pattern in backend/routes_user.py.

Feedback on a stored answer is linked to its ask_history row (deep-link
Phase 6): the owner's client sends ``history_id``, a visitor on a shared
link sends ``share_token``. Neither is trusted as-is -- a history_id counts
only if it is the caller's own row, a share token only while its link is
live -- and the stored link is the row id (``history_id``, plus
``shared_view`` for a visitor's feedback), never the token, which the owner
can revoke. The columns come from
scripts/sql/migrate_answer_feedback_history_link.sql; until it runs, the
feedback is still saved, just unlinked.
"""

import hashlib
import re

from flask import Blueprint, jsonify, request

from backend.claude import sanitize_user_query
from backend.auth import maybe_require_clerk_auth

from app import (
    SUPABASE_ANSWER_FEEDBACK_TABLE,
    SUPABASE_ASK_HISTORY_TABLE,
    _get_request_user_id,
    _get_supabase_client,
    _capture_backend_error,
)

routes_feedback = Blueprint("feedback", __name__)

_VALID_VERDICTS = ("helpful", "not_helpful")
_MAX_COMMENT_CHARS = 500
# The three metadata fields are enum-like short strings ("balanced"/"strict",
# "en"/"he", "ok"/"mental_health_or_self_harm" -- the longest safety class is
# 26 chars), so their caps are far tighter than the free-text comment's.
_MAX_MODE_CHARS = 32
_MAX_LANGUAGE_CHARS = 16
_MAX_SAFETY_CLASS_CHARS = 32

_HISTORY_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# PostgREST's "no such column": undefined column (42703) and column missing
# from the schema cache (PGRST204) -- the link migration hasn't run.
_LINK_COLUMNS_MISSING = frozenset({"42703", "PGRST204"})


def _short_field(payload, key, default, max_chars):
    """A capped, sanitized enum-like string field; `default` when the value
    is missing or sanitizes to nothing. Caps the way `comment` is capped
    (sanitize_user_query) so a malformed/oversized value can't land in a
    column that is expected to hold a short label (AI_SECURITY_REVIEW L2)."""
    return sanitize_user_query(str(payload.get(key) or ""), max_chars=max_chars) or default


def _linked_answer(supabase, payload, user_id):
    """{"history_id", "shared_view"} for the stored answer this feedback is
    about, or {} when it isn't one the caller may point at. Best effort: a
    failed lookup (e.g. the share columns don't exist yet) links nothing."""
    token = str(payload.get("share_token") or "")
    history_id = str(payload.get("history_id") or "")
    try:
        if _SHARE_TOKEN_RE.match(token):
            rows = (
                supabase.table(SUPABASE_ASK_HISTORY_TABLE).select("id")
                .eq("share_token", token).eq("is_public", True).limit(1).execute()
            ).data or []
            return {"history_id": rows[0]["id"], "shared_view": True} if rows else {}
        if user_id and _HISTORY_ID_RE.match(history_id):
            rows = (
                supabase.table(SUPABASE_ASK_HISTORY_TABLE).select("id")
                .eq("id", history_id).eq("user_id", user_id).limit(1).execute()
            ).data or []
            return {"history_id": rows[0]["id"], "shared_view": False} if rows else {}
    except Exception:
        return {}
    return {}


def _insert_feedback(supabase, record, link):
    table = supabase.table(SUPABASE_ANSWER_FEEDBACK_TABLE)
    if not link:
        table.insert(record).execute()
        return
    try:
        table.insert({**record, **link}).execute()
    except Exception as e:
        if str(getattr(e, "code", "") or "") not in _LINK_COLUMNS_MISSING:
            raise
        # The link columns aren't there yet: keep the verdict, drop the link.
        supabase.table(SUPABASE_ANSWER_FEEDBACK_TABLE).insert(record).execute()


@routes_feedback.route("/api/feedback", methods=["POST"])
@maybe_require_clerk_auth
def submit_feedback():
    payload = request.get_json(silent=True) or {}

    verdict = str(payload.get("verdict") or "").strip()
    if verdict not in _VALID_VERDICTS:
        return jsonify({"error": "verdict must be 'helpful' or 'not_helpful'"}), 400

    question = str(payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "question is required"}), 400
    question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()

    comment = sanitize_user_query(
        str(payload.get("comment") or ""), max_chars=_MAX_COMMENT_CHARS)

    record = {
        "user_id": _get_request_user_id(),
        "question_hash": question_hash,
        "verdict": verdict,
        "comment": comment,
        "mode": _short_field(payload, "mode", "balanced", _MAX_MODE_CHARS),
        "language": _short_field(payload, "language", "en", _MAX_LANGUAGE_CHARS),
        "fallback": bool(payload.get("fallback")),
        "safety_class": _short_field(payload, "safety_class", "ok", _MAX_SAFETY_CLASS_CHARS),
    }

    try:
        supabase_client = _get_supabase_client()
        if not supabase_client:
            return jsonify({"error": "Supabase not configured"}), 503
        link = _linked_answer(supabase_client, payload, record["user_id"])
        _insert_feedback(supabase_client, record, link)
    except Exception as e:
        _capture_backend_error("answer_feedback_persist_failed", e, {
            "verdict": verdict,
        })
        return jsonify({"error": "Could not save feedback"}), 500

    return jsonify({"success": True}), 200
