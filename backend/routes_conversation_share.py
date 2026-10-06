"""
Public share links for multi-turn conversations.

A conversation is private to its owner (``/chat/<id>``). The owner can mint a
public link for it here: ``POST`` sets an unguessable ``conversations.share_id``
and pins ``share_snapshot_msg_id`` to the newest finished message, so anyone
holding ``/a/<token>`` reads the chat *as it was when it was shared* -- turns
added afterwards stay private until the owner shares again (the token, and so
the link they already sent, does not change). ``DELETE`` clears both, so the
old link 404s for good and sharing again mints a new token.

It is the same ``/a/<token>`` link an answer share uses
(backend/routes_answer_share.py): ``GET /api/public/answer/<token>`` answers
with the conversation when no stored answer carries the token, so one link
shape, one router key and one public viewer cover both. The columns have
existed since scripts/sql/conversations_setup.sql; no migration is needed.

The owner routes use the same RLS-backed client as the rest of
routes_conversations and always filter on the caller's verified Clerk ``sub``.
The public read goes through the service-role client and selects only what a
reader sees -- never ``user_id`` or any row id.
"""

import re
import secrets
from datetime import datetime, timezone

from flask import Blueprint, jsonify

from backend.auth import require_clerk_auth
from backend.routes_conversations import (
    _ERR_NOT_FOUND,
    _conversations_client,
    _require_user_id,
    _ERR_MISSING_USER_IDENTITY,
)

from app import (
    SUPABASE_CONVERSATIONS_TABLE,
    SUPABASE_MESSAGES_TABLE,
    _capture_backend_error,
    hash_user_id,
)

routes_conversation_share = Blueprint("conversation_share", __name__)

_CONVERSATION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_SHARE_TOKEN_BYTES = 16
# The longest a shared chat is read back; the composer's own history window
# is far smaller, so this only bounds a pathological thread.
_MAX_PUBLIC_MESSAGES = 200
_PUBLIC_CITATION_KEYS = ("ordinal", "source_ref", "excerpt_he", "excerpt_en", "url")
_PUBLIC_MESSAGE_COLUMNS = f"role,content,created_at,citations({','.join(_PUBLIC_CITATION_KEYS)})"


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _share_path(token):
    return f"/a/{token}"


def _share_state(row):
    token = str((row or {}).get("share_id") or "")
    if token:
        return {"shared": True, "share_token": token, "path": _share_path(token)}
    return {"shared": False}


def _owner_context(conversation_id):
    """(user_id, supabase, error_response) for an owner route."""
    user_id = _require_user_id()
    if not user_id:
        return None, None, (jsonify({"error": _ERR_MISSING_USER_IDENTITY}), 401)
    if not _CONVERSATION_ID_RE.match(str(conversation_id or "")):
        return None, None, (jsonify({"error": _ERR_NOT_FOUND}), 404)
    supabase, error_response = _conversations_client()
    if error_response:
        return None, None, error_response
    return user_id, supabase, None


def _own_row(supabase, conversation_id, user_id):
    result = (
        supabase
        .table(SUPABASE_CONVERSATIONS_TABLE)
        .select("id,share_id")
        .eq("id", str(conversation_id))
        .eq("user_id", user_id)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def _latest_message_id(supabase, conversation_id):
    """The newest finished turn: what a share pins itself to."""
    result = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select("id")
        .eq("conversation_id", str(conversation_id))
        .eq("status", "complete")
        .is_("superseded_by", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0]["id"] if rows else None


def _owner_error(event, exc, user_id, conversation_id, message):
    _capture_backend_error(event, exc, {
        "user_id_hash": hash_user_id(user_id), "conversation_id": conversation_id,
    })
    return jsonify({"error": message}), 500


@routes_conversation_share.route("/api/conversations/<conversation_id>/share", methods=["GET"])
@require_clerk_auth
def get_conversation_share(conversation_id):
    """The owner's share state for one conversation."""
    user_id, supabase, error = _owner_context(conversation_id)
    if error:
        return error
    try:
        row = _own_row(supabase, conversation_id, user_id)
    except Exception as e:
        return _owner_error("conversation_share_get_failed", e, user_id, conversation_id, "Failed to load share state")
    if row is None:
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    return jsonify(_share_state(row))


@routes_conversation_share.route("/api/conversations/<conversation_id>/share", methods=["POST"])
@require_clerk_auth
def create_conversation_share(conversation_id):
    """Make a conversation public as it stands now. Sharing again keeps the
    token (200) and moves the snapshot forward to the newest turn, so a link
    already sent shows the chat's later turns once the owner chooses to; a
    first share mints the token (201)."""
    user_id, supabase, error = _owner_context(conversation_id)
    if error:
        return error
    try:
        row = _own_row(supabase, conversation_id, user_id)
        if row is None:
            return jsonify({"error": _ERR_NOT_FOUND}), 404
        snapshot = _latest_message_id(supabase, conversation_id)
        if not snapshot:
            return jsonify({
                "error": "There is nothing to share yet.",
                "code": "empty_conversation",
            }), 409

        existing = str(row.get("share_id") or "")
        token = existing or secrets.token_urlsafe(_SHARE_TOKEN_BYTES)
        update = {"share_snapshot_msg_id": snapshot}
        if not existing:
            update.update({"share_id": token, "shared_at": _now_iso()})
        query = (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .update(update)
            .eq("id", str(conversation_id))
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
        )
        if not existing:
            # Compare-and-set: a concurrent first share wins, and the re-read
            # below returns its token instead of overwriting a link that may
            # already have been copied.
            query = query.is_("share_id", "null")
        updated = query.execute()
        if updated.data:
            return jsonify(_share_state({"share_id": token})), (200 if existing else 201)
        current = _own_row(supabase, conversation_id, user_id)
        if current and current.get("share_id"):
            return jsonify(_share_state(current))
        return jsonify({"error": _ERR_NOT_FOUND}), 404
    except Exception as e:
        return _owner_error("conversation_share_create_failed", e, user_id, conversation_id, "Failed to share conversation")


@routes_conversation_share.route("/api/conversations/<conversation_id>/share", methods=["DELETE"])
@require_clerk_auth
def revoke_conversation_share(conversation_id):
    """Stop sharing: the public link 404s from now on. The owner's own
    /chat/<id> is untouched."""
    user_id, supabase, error = _owner_context(conversation_id)
    if error:
        return error
    try:
        if _own_row(supabase, conversation_id, user_id) is None:
            return jsonify({"error": _ERR_NOT_FOUND}), 404
        (
            supabase
            .table(SUPABASE_CONVERSATIONS_TABLE)
            .update({"share_id": None, "share_snapshot_msg_id": None, "shared_at": None})
            .eq("id", str(conversation_id))
            .eq("user_id", user_id)
            .execute()
        )
        return jsonify({"shared": False})
    except Exception as e:
        return _owner_error("conversation_share_revoke_failed", e, user_id, conversation_id, "Failed to stop sharing")


def _public_turn(row):
    """One turn as a reader sees it: only these fields, whatever the query
    returned (no message or conversation id, no status)."""
    return {
        "role": row.get("role"),
        "content": row.get("content") or "",
        "created_at": row.get("created_at"),
        "citations": [
            {key: citation.get(key) for key in _PUBLIC_CITATION_KEYS}
            for citation in row.get("citations") or []
            if isinstance(citation, dict)
        ],
    }


def public_conversation_payload(supabase, token):
    """A shared conversation as anyone holding its link may read it, or None
    when no live conversation carries ``token``. Called by
    routes_answer_share.get_public_answer with a service-role client.

    The turns are the ones up to and including the pinned snapshot message,
    finished and not superseded, oldest first. Raises on a database error.
    """
    result = (
        supabase
        .table(SUPABASE_CONVERSATIONS_TABLE)
        .select("id,title,minhag,created_at,shared_at,share_snapshot_msg_id")
        .eq("share_id", token)
        .is_("deleted_at", "null")
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return None
    conversation = rows[0]
    snapshot_id = conversation.get("share_snapshot_msg_id")
    if not snapshot_id:
        return None

    pinned = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select("created_at")
        .eq("id", str(snapshot_id))
        .eq("conversation_id", str(conversation["id"]))
        .limit(1)
        .execute()
    )
    pinned_rows = pinned.data or []
    if not pinned_rows:
        return None

    messages = (
        supabase
        .table(SUPABASE_MESSAGES_TABLE)
        .select(_PUBLIC_MESSAGE_COLUMNS)
        .eq("conversation_id", str(conversation["id"]))
        .eq("status", "complete")
        .is_("superseded_by", "null")
        .lte("created_at", pinned_rows[0]["created_at"])
        .order("created_at")
        .limit(_MAX_PUBLIC_MESSAGES)
        .execute()
    )
    turns = [_public_turn(row) for row in messages.data or []]
    if not turns:
        return None
    return {
        "conversation": True,
        "title": conversation.get("title") or "",
        "minhag": conversation.get("minhag"),
        "created_at": conversation.get("created_at"),
        "shared_at": conversation.get("shared_at"),
        "messages": turns,
        "public": True,
    }
