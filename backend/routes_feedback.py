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
    app,
    SUPABASE_ANSWER_FEEDBACK_TABLE,
    _get_request_user_id,
    _get_supabase_client,
    _capture_backend_error,
)

routes_feedback = Blueprint("feedback", __name__)

_VALID_VERDICTS = ("helpful", "not_helpful")
_MAX_COMMENT_CHARS = 500


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
        "mode": str(payload.get("mode") or "balanced"),
        "language": str(payload.get("language") or "en"),
        "fallback": bool(payload.get("fallback")),
        "safety_class": str(payload.get("safety_class") or "ok"),
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
