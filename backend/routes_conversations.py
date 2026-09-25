"""
Conversations blueprint for Sh'elah -- multi-turn AI conversation storage.

New, separate schema from ask_history (backend/routes_user.py's `/api/user/
history*` routes): conversations/messages/citations, backed by
scripts/sql/conversations_setup.sql. ask_history is untouched and keeps
serving the existing single-shot Q&A history + `?chat=<id>` deep link;
these routes are additive, for the multi-turn conversation feature.

Unlike ask_history (service-role-only, decided 2026-08-31 per its own
migration's header comment), these tables use this codebase's more common
RLS-backed pattern -- see `_get_user_scoped_supabase_client()` and
backend/routes_user.py's `user_preferences`/`semantic_bookmarks` routes for
the precedent this follows: a per-request Supabase client carrying the
caller's Clerk bearer token, gated by real `(auth.jwt() ->> 'sub') =
user_id` policies, with a service-role fallback only when
STRICT_SUPABASE_RLS is off (local/dev). Every route below ALSO hard-filters
to the caller's own verified `g.clerk_claims["sub"]` at the application
layer, same as every other route in this file family -- RLS is
defense-in-depth here, not the only thing standing between users' data
(see TestApplicationLayerCrossUserIsolation in tests/test_routes_user.py
for why that matters).

Step 1 ("data model and routing") added conversation/message CRUD. Step 2
adds ask_in_conversation() below: real multi-turn AI synthesis, passing
this thread's prior turns into the prompt via claude.build_prompt()'s
conversation_history param, with the minhag lock enforced server-side (the
conversation row's own `minhag`, never the request body). The transcript
UI, sidebar, search, and sharing are later steps and not implemented here.
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, g

from backend.auth import require_clerk_auth
from backend import claude
from backend.cost_gates import (
    bind_cost_attribution,
    budget_exhausted_message,
    clear_cost_attribution,
    evaluate_cost_gates_sync,
)
from backend.helpers import (
    _sanitize_answer_mode,
    _canonicalize_community_name,
    _compact_ai_sources,
)

from app import (
    STRICT_SUPABASE_RLS,
    SUPABASE_CONVERSATIONS_TABLE,
    SUPABASE_MESSAGES_TABLE,
    SUPABASE_CITATIONS_TABLE,
    _get_supabase_client,
    _get_user_scoped_supabase_client,
    _capture_backend_error,
    hash_user_id,
    get_engine,
    _collect_ask_question_context,
    _dispatch_ask_ai_synthesis_call,
    _coerce_and_validate_ai_result,
    _extract_raw_ai_answer,
    _resolve_ask_web_warning_flag,
    _compose_validated_ask_answer,
    _store_user_memory_summary,
    _extract_client_ip,
)

routes_conversations = Blueprint("conversations", __name__)

_ERR_MISSING_USER_IDENTITY = "Missing user identity"
_ERR_SUPABASE_NOT_CONFIGURED = "Supabase not configured"
_ERR_NOT_FOUND = "Not found"
_VALID_ROLES = ("user", "assistant")
_VALID_STATUSES = ("streaming", "complete", "stopped", "error")
_MAX_HISTORY_MESSAGES = 20
_AUTO_TITLE_MAX_LEN = 80
_CONVERSATION_HEADER_COLUMNS = "id,title,title_is_custom,minhag,pinned_at,created_at,updated_at"
_AI_PAUSED_MESSAGE = "AI answers are paused for today. Please try again after midnight UTC."


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _derive_title(question, max_len=_AUTO_TITLE_MAX_LEN):
    """Default sidebar/header title from a thread's first question: whitespace
    collapsed, cut at a word boundary with an ellipsis when over `max_len`.
    Written with title_is_custom left False, so it never masks a user rename
    (update_conversation() is the only path that sets title_is_custom)."""
    text = " ".join(str(question or "").split())
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0] or text[:max_len]
    return cut.rstrip(" ,.;:?!") + "…"


def _require_user_id():
    """Returns the caller's verified Clerk sub, or None (caller returns 401)."""
    claims = getattr(g, "clerk_claims", {}) or {}
    return str(claims.get("sub") or "").strip() or None


def _conversations_client():
    """RLS-backed client with a service-role fallback outside strict mode,
    same shape as user_preferences()/semantic_bookmarks() in routes_user.py.
    Returns (client, error_response_or_None).
    """
    supabase = _get_user_scoped_supabase_client()
    if not supabase and not STRICT_SUPABASE_RLS:
        supabase = _get_supabase_client()
    if not supabase:
        if STRICT_SUPABASE_RLS:
            return None, (jsonify({
                "error": "Supabase authenticated session required for RLS-protected conversations."
            }), 403)
        return None, (jsonify({"error": _ERR_SUPABASE_NOT_CONFIGURED}), 503)
    return supabase, None


def _fetch_own_conversation(supabase, conversation_id, user_id):
    """Application-layer ownership check shared by every per-conversation
    route -- fetched by id AND the caller's own user_id, never id alone, so
    a guessed/foreign id 404s instead of leaking or mutating someone else's
    thread even if RLS were ever misconfigured."""
    result = (
        supabase
        .table(SUPABASE_CONVERSATIONS_TABLE)
        .select(_CONVERSATION_HEADER_COLUMNS)
        .eq("id", str(conversation_id))
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


@routes_conversations.route("/api/conversations", methods=["POST"])
@require_clerk_auth
def create_conversation():
    """Start a new conversation. `minhag` is locked in at creation time --
    the plan's minhag-lock rule means it is never changed on this thread
    again; a minhag switch means a new thread."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}
    minhag = str(payload.get("minhag") or "").strip()[:80] or None

    record = {
        "user_id": user_id,
        "title": "",
        "title_is_custom": False,
        "minhag": minhag,
    }
    try:
        result = supabase.table(SUPABASE_CONVERSATIONS_TABLE).insert(record).execute()
        rows = result.data or []
        if not rows:
            return jsonify({"error": "Failed to create conversation"}), 500
        return jsonify(rows[0]), 201
    except Exception as e:
        _capture_backend_error("conversation_create_failed", e, {"user_id_hash": hash_user_id(user_id)})
        return jsonify({"error": "Failed to create conversation"}), 500


@routes_conversations.route("/api/conversations", methods=["GET"])
@require_clerk_auth
def list_conversations():
    """Sidebar list: pinned first, then most-recently-updated, excluding
    soft-deleted threads."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    try:
        limit = max(1, min(int(request.args.get("limit", 20)), 50))
        result = (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .select("id,title,title_is_custom,minhag,pinned_at,created_at,updated_at")
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .order("pinned_at", desc=True, nullsfirst=False)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return jsonify({"items": result.data or []})
    except Exception as e:
        _capture_backend_error("conversations_list_failed", e, {"user_id_hash": hash_user_id(user_id)})
        return jsonify({"error": "Failed to load conversations"}), 500


@routes_conversations.route("/api/conversations/<conversation_id>", methods=["GET"])
@require_clerk_auth
def get_conversation(conversation_id):
    """Hydrate one thread: the conversation header plus every turn, each
    with its citations embedded (PostgREST resource embedding on the
    messages->citations foreign key, one query instead of N+1). Turns with
    a non-null `superseded_by` (a failed answer the user retried -- see
    ask_in_conversation()'s `retry_of`) are excluded, so only the live
    version of each turn is ever rendered."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    try:
        conversation = _fetch_own_conversation(supabase, conversation_id, user_id)
        if not conversation:
            return jsonify({"error": _ERR_NOT_FOUND}), 404

        messages_result = (
            supabase
            .table(SUPABASE_MESSAGES_TABLE)
            .select("id,role,content,status,superseded_by,created_at,citations(id,ordinal,source_ref,excerpt_he,excerpt_en,url)")
            .eq("conversation_id", str(conversation_id))
            .is_("superseded_by", "null")
            .order("created_at")
            .execute()
        )
        conversation["messages"] = messages_result.data or []
        return jsonify(conversation)
    except Exception as e:
        _capture_backend_error("conversation_get_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to load conversation"}), 500


def _insert_message_citations(supabase, message_id, citations):
    """Bulk-insert citation rows for a just-created message. `citations` is
    the caller-supplied list, trusted only for its content fields -- the
    message_id/ordinal are always assigned here, never taken from input."""
    if not citations:
        return []
    rows = []
    for i, c in enumerate(citations):
        if not isinstance(c, dict):
            continue
        rows.append({
            "message_id": message_id,
            "ordinal": int(c.get("ordinal") or i),
            "source_ref": str(c.get("source_ref") or "").strip()[:500] or None,
            "excerpt_he": str(c.get("excerpt_he") or "").strip()[:2000] or None,
            "excerpt_en": str(c.get("excerpt_en") or "").strip()[:2000] or None,
            "url": str(c.get("url") or "").strip()[:1000] or None,
        })
    if not rows:
        return []
    result = supabase.table(SUPABASE_CITATIONS_TABLE).insert(rows).execute()
    return result.data or []


@routes_conversations.route("/api/conversations/<conversation_id>/messages", methods=["POST"])
@require_clerk_auth
def create_message(conversation_id):
    """Append one turn to an existing conversation and bump its
    `updated_at` so the sidebar list re-sorts it to the top."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}
    role = str(payload.get("role") or "").strip()
    if role not in _VALID_ROLES:
        return jsonify({"error": f"role must be one of {_VALID_ROLES}"}), 400
    status = str(payload.get("status") or "complete").strip()
    if status not in _VALID_STATUSES:
        return jsonify({"error": f"status must be one of {_VALID_STATUSES}"}), 400
    content = str(payload.get("content") or "")
    citations = payload.get("citations") or []

    try:
        conversation = _fetch_own_conversation(supabase, conversation_id, user_id)
        if not conversation:
            return jsonify({"error": _ERR_NOT_FOUND}), 404

        message_record = {
            "conversation_id": str(conversation_id),
            "role": role,
            "content": content,
            "status": status,
        }
        result = supabase.table(SUPABASE_MESSAGES_TABLE).insert(message_record).execute()
        rows = result.data or []
        if not rows:
            return jsonify({"error": "Failed to create message"}), 500
        message = rows[0]

        message["citations"] = _insert_message_citations(
            supabase, message["id"], citations if isinstance(citations, list) else []
        )

        supabase.table(SUPABASE_CONVERSATIONS_TABLE).update(
            {"updated_at": _now_iso()}
        ).eq("id", str(conversation_id)).eq("user_id", user_id).execute()

        return jsonify(message), 201
    except Exception as e:
        _capture_backend_error("conversation_message_create_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to create message"}), 500


@routes_conversations.route("/api/conversations/<conversation_id>", methods=["PATCH"])
@require_clerk_auth
def update_conversation(conversation_id):
    """Rename and/or pin/unpin a conversation. Minhag is deliberately not
    editable here -- it is locked at creation (see create_conversation)."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}
    updates = {}
    if "title" in payload:
        updates["title"] = str(payload.get("title") or "").strip()[:200]
        updates["title_is_custom"] = True
    if "pinned" in payload:
        updates["pinned_at"] = _now_iso() if payload.get("pinned") else None
    if not updates:
        return jsonify({"error": "No updatable fields provided"}), 400

    try:
        conversation = _fetch_own_conversation(supabase, conversation_id, user_id)
        if not conversation:
            return jsonify({"error": _ERR_NOT_FOUND}), 404

        result = (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .update(updates)
            .eq("id", str(conversation_id))
            .eq("user_id", user_id)
            .execute()
        )
        rows = result.data or []
        return jsonify(rows[0] if rows else {**conversation, **updates})
    except Exception as e:
        _capture_backend_error("conversation_update_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to update conversation"}), 500


@routes_conversations.route("/api/conversations/<conversation_id>", methods=["DELETE"])
@require_clerk_auth
def delete_conversation(conversation_id):
    """Soft-delete: sets `deleted_at` rather than removing the row, so the
    frontend can offer an Undo toast (spec: ~8s) before it's really gone."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    try:
        supabase.table(SUPABASE_CONVERSATIONS_TABLE).update(
            {"deleted_at": _now_iso()}
        ).eq("id", str(conversation_id)).eq("user_id", user_id).execute()
        return jsonify({"ok": True})
    except Exception as e:
        _capture_backend_error("conversation_delete_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to delete conversation"}), 500


@routes_conversations.route("/api/conversations/<conversation_id>/restore", methods=["POST"])
@require_clerk_auth
def restore_conversation(conversation_id):
    """Undo a soft delete (the frontend's Undo toast): clears `deleted_at`
    and returns the conversation header, same shape PATCH returns.

    Deliberately does NOT go through _fetch_own_conversation(), which
    filters out soft-deleted rows -- the whole point here is to find one.
    Still owner-only: looked up by id AND the caller's verified user_id, so
    a foreign or unknown id 404s. Idempotent: restoring a thread that isn't
    deleted just returns it unchanged (a double-clicked Undo, or an Undo
    racing a DELETE that never landed, both succeed)."""
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    try:
        result = (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .select(f"{_CONVERSATION_HEADER_COLUMNS},deleted_at")
            .eq("id", str(conversation_id))
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            return jsonify({"error": _ERR_NOT_FOUND}), 404
        conversation = rows[0]
        if not conversation.get("deleted_at"):
            return jsonify(conversation)

        update_result = (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .update({"deleted_at": None})
            .eq("id", str(conversation_id))
            .eq("user_id", user_id)
            .execute()
        )
        updated_rows = update_result.data or []
        return jsonify(updated_rows[0] if updated_rows else {**conversation, "deleted_at": None})
    except Exception as e:
        _capture_backend_error("conversation_restore_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to restore conversation"}), 500


def _fetch_conversation_history(supabase, conversation_id, limit=_MAX_HISTORY_MESSAGES, before=None):
    """Prior turns for this thread, oldest first, shaped for
    claude.build_prompt()'s conversation_history param. Only `status=
    complete`, non-superseded turns are included -- a streaming/stopped/
    error message either isn't final yet or never produced real content,
    so feeding it back as context would confuse continuity rather than
    help it. Fetches a generous multiple of `limit` before the final
    slice, since the Python-side role filter can thin the set below what a
    plain row-count limit would give the model.

    Queried newest-first and reversed here, so a long thread's context is
    its MOST RECENT `limit` turns -- an ascending order + limit would
    instead pin the window to the thread's first rows forever.

    `before` (an ISO timestamp) restricts to turns strictly older than it:
    a retry passes the retried question's own created_at, so the model sees
    exactly the history the original ask saw -- not the question itself,
    and not anything after it."""
    query = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select("role,content,status,created_at")
        .eq("conversation_id", str(conversation_id))
        .eq("status", "complete")
        .is_("superseded_by", "null")
    )
    if before:
        query = query.lt("created_at", before)
    result = query.order("created_at", desc=True).limit(limit * 4).execute()
    rows = [
        {"role": r["role"], "content": r["content"]}
        for r in (result.data or [])
        if r.get("role") in _VALID_ROLES
    ]
    return list(reversed(rows[:limit]))


def _display_sources_to_citations(display_sources):
    """Map _compact_ai_sources()' UI-excerpt shape ({ref, title, lines:
    [{en, he}], url?}) onto the citations row shape (source_ref,
    excerpt_en, excerpt_he, url) _insert_message_citations() expects --
    the same shape create_message() already accepts from a client-supplied
    citations list."""
    citations = []
    for src in display_sources or []:
        if not isinstance(src, dict):
            continue
        lines = [line for line in (src.get("lines") or []) if isinstance(line, dict)]
        excerpt_en = " ".join(str(line.get("en") or "").strip() for line in lines).strip()
        excerpt_he = " ".join(str(line.get("he") or "").strip() for line in lines).strip()
        citations.append({
            "source_ref": src.get("ref") or src.get("title") or "",
            "excerpt_en": excerpt_en,
            "excerpt_he": excerpt_he,
            "url": src.get("url") or "",
        })
    return citations


def _store_error_placeholder_message(supabase, conversation_id, user_id):
    """Best-effort status=error assistant message so the thread shows a
    visible, retryable failure instead of silently dropping the turn --
    the caller already saved the user's own message, so this route
    responds 201 either way rather than 500ing after that write succeeded.
    If even this insert fails, falls back to a client-only placeholder
    (no `id`, never persisted)."""
    try:
        result = supabase.table(SUPABASE_MESSAGES_TABLE).insert({
            "conversation_id": str(conversation_id),
            "role": "assistant",
            "content": "",
            "status": "error",
        }).execute()
        rows = result.data or []
        if rows:
            message = rows[0]
            message["citations"] = []
            return message
    except Exception as placeholder_error:
        _capture_backend_error("conversation_ask_error_placeholder_failed", placeholder_error, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
    return {"role": "assistant", "content": "", "status": "error", "citations": []}


def _synthesize_and_store_assistant_reply(
    supabase, conversation_id, user_id, question, mode, canonical_lens,
    answer_language, conversation_history,
):
    """Runs AI synthesis for one conversation turn and persists the
    assistant's reply as a message row, or a status="error" placeholder on
    failure. Split out of ask_in_conversation() so its try/except doesn't
    also guard the user-message insert that happens before this is called
    -- a synthesis failure must not undo the already-saved user turn."""
    try:
        engine = get_engine()
        ctx = _collect_ask_question_context(
            question, canonical_lens, user_id, answer_language, engine)
        result = _dispatch_ask_ai_synthesis_call(
            question, mode, canonical_lens, answer_language, ctx, engine,
            conversation_history=conversation_history,
        )
        result, result_error = _coerce_and_validate_ai_result(
            result, question, mode, answer_language)

        if result_error.startswith("security_blocked"):
            answer_text = str(result.get("answer") or "").strip() or (
                "Request blocked by security policy. Please submit a direct halakhic question."
            )
            citations = []
        else:
            structured_payload, raw_ai_answer = _extract_raw_ai_answer(result, answer_language)
            needs_web_warning = _resolve_ask_web_warning_flag(result, ctx)
            answer_text = _compose_validated_ask_answer(raw_ai_answer, needs_web_warning)
            _store_user_memory_summary(user_id, question, answer_text)
            citations = _display_sources_to_citations(_compact_ai_sources(ctx["primary_sources"]))

        message_result = supabase.table(SUPABASE_MESSAGES_TABLE).insert({
            "conversation_id": str(conversation_id),
            "role": "assistant",
            "content": answer_text,
            "status": "complete",
        }).execute()
        rows = message_result.data or []
        if not rows:
            raise RuntimeError("assistant message insert returned no rows")
        message = rows[0]
        message["citations"] = _insert_message_citations(supabase, message["id"], citations)

        supabase.table(SUPABASE_CONVERSATIONS_TABLE).update(
            {"updated_at": _now_iso()}
        ).eq("id", str(conversation_id)).eq("user_id", user_id).execute()
        return message
    except Exception as e:
        _capture_backend_error("conversation_ask_synthesis_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return _store_error_placeholder_message(supabase, conversation_id, user_id)


def _seconds_until_next_utc_midnight(now=None):
    """Retry-After value for the global-breaker 503: the breaker's spend
    window is the UTC day (cost_meter's _fetch_today_usage_rows), so it
    can only un-trip at the next UTC midnight."""
    now = now or datetime.now(timezone.utc)
    next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((next_midnight - now).total_seconds()))


def _enforce_conversation_cost_gates(user_id):
    """Same two cost gates asgi.py's /ask applies (_enforce_ask_async_budget
    and _resolve_ask_async_breaker_response), for the conversation ask
    route. Returns a Flask error response to send as-is, or None to proceed.

    cost_meter's gates are coroutines and this route is a sync Flask view
    running in a WSGI worker thread, so they run through
    backend/cost_gates.py's evaluate_cost_gates_sync() -- one loop-bridge
    hop, breaker first -- rather than an event loop in this request thread.
    Unlike app.py's sync /ask, a gate-hop failure or timeout is NOT failed
    open: it propagates to _run_conversation_ask()'s 500.

    On success, binds the caller's identity and budget reservation into
    THIS request's context: _dispatch_ask_ai_synthesis_call() carries that
    context into its worker via submit_with_context, which is how
    record_llm_call() deep in backend/claude.py tags the ai_usage_log row
    with this user and settles the reservation. Without it (the sync Flask
    path never binds user_id itself), every conversation answer would be
    logged with an empty user_id and never count toward the per-user cap
    this function enforces. ask_in_conversation() clears the bindings when
    the request ends (clear_cost_attribution).

    No cached-answer fallback when the breaker is tripped (asgi.py's /ask
    serves a stale cached payload): a thread answer depends on the thread's
    own history, so a cache keyed on the question alone would be wrong."""
    client_ip = _extract_client_ip() or ""
    gates = evaluate_cost_gates_sync(user_id, client_ip)

    if gates["tripped"]:
        response = jsonify({"error": _AI_PAUSED_MESSAGE, "code": "ai_paused"})
        response.headers["Retry-After"] = str(_seconds_until_next_utc_midnight())
        return response, 503

    budget = gates["budget"]
    if not budget["allowed"]:
        return jsonify({"error": budget_exhausted_message(budget), "code": "daily_budget_exhausted"}), 402

    # user_id is always set here (signed-in only route), so the client key
    # bound alongside it is "" -- spend is keyed by user_id, not IP.
    bind_cost_attribution(user_id, client_ip, gates["reservation_id"])
    return None


def _invalid_retry_response(message):
    return jsonify({"error": message, "code": "invalid_retry"}), 400


def _parse_retry_of(payload):
    """(is_retry, canonical_retry_of_or_None). Any non-null `retry_of` makes
    this a retry; one that isn't a UUID (messages.id's type) is rejected up
    front as invalid_retry rather than reaching Postgres as a cast error."""
    raw = payload.get("retry_of")
    if raw is None:
        return False, None
    try:
        return True, str(uuid.UUID(str(raw).strip()))
    except ValueError:
        return True, None


def _resolve_retry_target(supabase, conversation_id, retry_of):
    """Validate a `retry_of` target and find the question it answered.
    Returns (retried_user_message, None), or (None, error_message) when the
    target isn't a live, failed assistant answer in THIS conversation (the
    caller has already verified the conversation is the caller's own, and
    the conversation_id filter here ties the message to it).

    The question is the nearest user turn strictly before the failed
    answer -- read server-side, never from the request body."""
    target_rows = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select("id,role,status,superseded_by,created_at")
        .eq("id", retry_of)
        .eq("conversation_id", str(conversation_id))
        .limit(1)
        .execute()
    ).data or []
    if not target_rows:
        return None, "The answer to retry was not found in this conversation."
    target = target_rows[0]
    if target.get("role") != "assistant" or target.get("status") != "error":
        return None, "Only a failed answer can be retried."
    if target.get("superseded_by"):
        return None, "This answer has already been retried."

    question_rows = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select("id,conversation_id,role,content,status,superseded_by,created_at")
        .eq("conversation_id", str(conversation_id))
        .eq("role", "user")
        .lt("created_at", target.get("created_at"))
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    ).data or []
    if not question_rows or not claude.sanitize_user_query(question_rows[0].get("content", "")):
        return None, "No question was found to retry."
    return question_rows[0], None


def _supersede_failed_answer(supabase, conversation_id, user_id, retry_of, assistant_message):
    """Point the retried error row's `superseded_by` at the new reply so
    GET /api/conversations/<id> stops returning it. Done whenever the new
    reply was actually persisted (has an id) -- including when it is itself
    a status=error placeholder, so only one error row stays visible. A
    client-only placeholder (no id) supersedes nothing: the old error row is
    still the only persisted record of that turn.

    The `superseded_by IS NULL` guard makes two racing retries of the same
    row keep the first pointer instead of overwriting it; either way the
    old row ends up hidden. Returns `retry_of` when the update ran, else
    None (the response's superseded_message_id)."""
    new_message_id = assistant_message.get("id")
    if not new_message_id:
        return None
    try:
        (
            supabase
            .table(SUPABASE_MESSAGES_TABLE)
            .update({"superseded_by": new_message_id})
            .eq("id", retry_of)
            .eq("conversation_id", str(conversation_id))
            .is_("superseded_by", "null")
            .execute()
        )
        return retry_of
    except Exception as e:
        _capture_backend_error("conversation_retry_supersede_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return None


def _insert_user_turn(supabase, conversation_id, question):
    """Persist a fresh ask's question. Returns the row, or None when the
    insert came back empty (caller 500s)."""
    result = supabase.table(SUPABASE_MESSAGES_TABLE).insert({
        "conversation_id": str(conversation_id),
        "role": "user",
        "content": question,
        "status": "complete",
    }).execute()
    rows = result.data or []
    return rows[0] if rows else None


def _run_conversation_ask(supabase, conversation_id, user_id, question, retry_of, mode, answer_language):
    """ask_in_conversation()'s body once the request itself is validated:
    ownership, retry-target resolution, cost gates, then (fresh asks only)
    the user-turn insert -- in that order, so a refused ask writes nothing
    -- followed by synthesis and, for a retry, superseding the old error."""
    try:
        conversation = _fetch_own_conversation(supabase, conversation_id, user_id)
        if not conversation:
            return jsonify({"error": _ERR_NOT_FOUND}), 404

        minhag = str(conversation.get("minhag") or "").strip()
        canonical_lens = "All" if not minhag or minhag.lower() == "all" else (
            _canonicalize_community_name(minhag) or minhag)

        user_message = None
        history_before = None
        if retry_of:
            user_message, retry_error = _resolve_retry_target(supabase, conversation_id, retry_of)
            if retry_error:
                return _invalid_retry_response(retry_error)
            question = claude.sanitize_user_query(user_message.get("content", ""))
            history_before = user_message.get("created_at")

        gate_response = _enforce_conversation_cost_gates(user_id)
        if gate_response:
            return gate_response

        conversation_history = _fetch_conversation_history(
            supabase, conversation_id, before=history_before)

        if user_message is None:
            user_message = _insert_user_turn(supabase, conversation_id, question)
            if user_message is None:
                return jsonify({"error": "Failed to save message"}), 500
        user_message["citations"] = []

        conversation_updates = {"updated_at": _now_iso()}
        if not conversation.get("title") and not conversation.get("title_is_custom"):
            conversation_updates["title"] = _derive_title(question)
        supabase.table(SUPABASE_CONVERSATIONS_TABLE).update(
            conversation_updates
        ).eq("id", str(conversation_id)).eq("user_id", user_id).execute()
    except Exception as e:
        _capture_backend_error("conversation_ask_setup_failed", e, {
            "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
        })
        return jsonify({"error": "Failed to start message"}), 500

    assistant_message = _synthesize_and_store_assistant_reply(
        supabase, conversation_id, user_id, question, mode, canonical_lens,
        answer_language, conversation_history,
    )
    # The conversation header as it stands after this turn (title may have
    # just been auto-derived above), so the client can update its header and
    # sidebar row without a second GET or re-deriving the title itself.
    body = {
        "user_message": user_message,
        "assistant_message": assistant_message,
        "conversation": {**conversation, **conversation_updates},
    }
    if retry_of:
        body["superseded_message_id"] = _supersede_failed_answer(
            supabase, conversation_id, user_id, retry_of, assistant_message)
    return jsonify(body), 201


@routes_conversations.route("/api/conversations/<conversation_id>/ask", methods=["POST"])
@require_clerk_auth
def ask_in_conversation(conversation_id):
    """Step 2: ask a follow-up question inside an existing thread, with
    real multi-turn context (this thread's prior complete turns are passed
    into the AI prompt via conversation_history).

    Reuses app.py's own /ask helpers (get_engine, _collect_ask_question_
    context, _dispatch_ask_ai_synthesis_call, ...) for retrieval and
    synthesis rather than re-implementing that orchestration a third time
    -- backend/ask_pipeline.py's module docstring documents why a prior
    attempt at a shared pipeline (run_ask_pipeline) was deleted as
    unadopted duplication; calling the live app.py helpers instead of
    copying them avoids repeating that mistake.

    minhag is read from the conversation row, never the request body --
    that's the plan's minhag lock: set once in create_conversation() and
    never changed mid-thread.

    Cost limits: the same global cost breaker (503 `ai_paused`, with a
    Retry-After to the next UTC midnight) and per-user daily budget (402
    `daily_budget_exhausted`) asgi.py's /ask applies, checked after
    auth/validation/ownership and BEFORE the user turn is written, so a
    refused ask leaves the thread untouched -- see
    _enforce_conversation_cost_gates(). backend/rate_limit.py's "llm" class
    already rate-limits this path. Still out of scope, matching the sync
    /ask route: the /ask response cache, the prayer/strict-mode
    short-circuits, ask_history, and the anonymous-only Turnstile gate
    (this route always has a signed-in caller).

    Retry: a body `retry_of` (id of a status=error assistant message in
    this thread) re-answers that turn instead of asking anew -- no new user
    turn is written, the question is the retried turn's own (the body's
    `question` is ignored and may be omitted), the history is what the
    original ask saw, and the error row is then superseded so GET stops
    returning it. The 201 body gains `superseded_message_id`.
    """
    user_id = _require_user_id()
    if not user_id:
        return jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401

    supabase, error_response = _conversations_client()
    if error_response:
        return error_response

    payload = request.get_json(silent=True) or {}
    is_retry, retry_of = _parse_retry_of(payload)
    if is_retry and not retry_of:
        return _invalid_retry_response("retry_of must be the id of a failed answer in this conversation.")
    question = "" if is_retry else claude.sanitize_user_query(payload.get("question", ""))
    if not is_retry and not question:
        return jsonify({"error": "No valid question provided"}), 400
    mode = _sanitize_answer_mode(payload.get("mode"))
    answer_language = str(payload.get("language") or "en").strip().lower()
    if answer_language not in {"en", "he"}:
        answer_language = "en"

    try:
        return _run_conversation_ask(
            supabase, conversation_id, user_id, question, retry_of, mode, answer_language)
    finally:
        clear_cost_attribution()
