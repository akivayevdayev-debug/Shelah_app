"""
Answer-feedback blueprint for Sh'elah (plan.md §12.4).

A single write-only endpoint: readers leave a thumbs up/down (and an
optional short comment) on an AI answer. Feedback is accepted from
signed-out readers too (nullable user_id), matching the accept_legal()
optional-auth pattern in backend/routes_user.py.
"""

import hashlib

from flask import Blueprint, jsonify, request

from backend.claude import sanitize_user_query
from backend.auth import maybe_require_clerk_auth

from app import (
    SUPABASE_ANSWER_FEEDBACK_TABLE,
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


def _short_field(payload, key, default, max_chars):
    """A capped, sanitized enum-like string field; `default` when the value
    is missing or sanitizes to nothing. Caps the way `comment` is capped
    (sanitize_user_query) so a malformed/oversized value can't land in a
    column that is expected to hold a short label (AI_SECURITY_REVIEW L2)."""
    return sanitize_user_query(str(payload.get(key) or ""), max_chars=max_chars) or default


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
        supabase_client.table(SUPABASE_ANSWER_FEEDBACK_TABLE).insert(record).execute()
    except Exception as e:
        _capture_backend_error("answer_feedback_persist_failed", e, {
            "verdict": verdict,
        })
        return jsonify({"error": "Could not save feedback"}), 500

    return jsonify({"success": True}), 200
