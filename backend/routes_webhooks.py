"""
Clerk webhook blueprint (plan.md §39.2).

Completeness backstop for account deletion: delete_account() (backend/
routes_privacy.py) is the only code path in this repo that deletes
Supabase rows for a user, but a Clerk identity can be deleted through
doors this app does not control -- Clerk's own hosted <UserProfile />
self-service "delete account" UI, or an operator removing a user directly
from the Clerk Dashboard (a normal, expected admin action for handling
abuse/takedowns). Neither path ever calls this app's own endpoint, so
neither triggers any cleanup without this listener -- every _USER_DATA_
TABLES row for that user would otherwise be orphaned forever, with no
Clerk JWT left to authenticate a retry.

This does NOT replace delete_account(): that endpoint remains the one
that gives a signed-in user an immediate, synchronous "ok": true
confirmation. This webhook is the backstop for the doors delete_account()
never sees fire.

Clerk delivers webhooks signed via Svix (https://www.svix.com/). Signature
verification IS this endpoint's authentication -- there is no Clerk JWT to
check here, since by the time this fires the identity itself may already
be gone. Verified manually against the documented Svix scheme (HMAC-SHA256
over "{id}.{timestamp}.{body}", base64-encoded secret/signature) rather
than adding the `svix` package as a new dependency for one header check.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

from flask import Blueprint, jsonify, request

from app import _get_supabase_client, _capture_backend_error
from backend.routes_privacy import _USER_DATA_TABLES, _delete_table_rows

routes_webhooks = Blueprint("webhooks", __name__)

# Svix's own library rejects a timestamp further than this from "now" (in
# either direction) to bound the window a captured request could be
# replayed in -- mirrored here since verification is hand-rolled.
_TIMESTAMP_TOLERANCE_SECONDS = 300

_CLERK_USER_DELETED_EVENT = "user.deleted"


def _verify_svix_signature(secret, svix_id, svix_timestamp, body, svix_signature):
    """Returns True iff `svix_signature` (one or more space-delimited
    "v1,<base64>" values -- Svix sends multiple during secret rotation)
    matches an HMAC-SHA256 computed over the raw body with `secret`, and
    `svix_timestamp` is within tolerance of now. Never raises -- any
    malformed header or secret is simply an invalid signature."""
    if not (secret and svix_id and svix_timestamp and svix_signature):
        return False

    try:
        timestamp = int(svix_timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - timestamp) > _TIMESTAMP_TOLERANCE_SECONDS:
        return False

    secret_prefix = "whsec_"
    raw_secret = secret[len(secret_prefix):] if secret.startswith(secret_prefix) else secret
    try:
        key = base64.b64decode(raw_secret)
    except Exception:
        return False

    signed_content = f"{svix_id}.{svix_timestamp}.{body}".encode("utf-8")
    expected_sig = base64.b64encode(
        hmac.new(key, signed_content, hashlib.sha256).digest()
    ).decode("utf-8")

    for candidate in svix_signature.split():
        _version, _sep, candidate_sig = candidate.partition(",")
        if candidate_sig and hmac.compare_digest(candidate_sig, expected_sig):
            return True
    return False


@routes_webhooks.route("/api/webhooks/clerk", methods=["POST"])
def clerk_webhook():
    """On a `user.deleted` event, cascade-delete every _USER_DATA_TABLES
    row for that user -- the same tables delete_account() already wipes
    (backend/routes_privacy.py), keyed on the event payload's `data.id`
    instead of a live Clerk JWT (which no longer exists once the identity
    is gone).

    Idempotent by construction, not by an explicit dedup store: Svix
    delivers at-least-once, and _delete_table_rows() already treats
    deleting zero remaining rows as success (routes_privacy.py's existing
    idempotency note) -- a replayed event just deletes nothing the second
    time and still returns 200.
    """
    secret = (os.environ.get("CLERK_WEBHOOK_SIGNING_SECRET") or "").strip()
    if not secret:
        return jsonify({"error": "CLERK_WEBHOOK_SIGNING_SECRET is not configured"}), 503

    body = request.get_data(as_text=True)
    svix_id = request.headers.get("svix-id", "")
    svix_timestamp = request.headers.get("svix-timestamp", "")
    svix_signature = request.headers.get("svix-signature", "")

    if not _verify_svix_signature(secret, svix_id, svix_timestamp, body, svix_signature):
        return jsonify({"error": "invalid signature"}), 401

    payload = request.get_json(silent=True) or {}
    event_type = str(payload.get("type") or "")
    if event_type != _CLERK_USER_DELETED_EVENT:
        # Not an error: this endpoint only acts on one event type, and
        # Clerk sends other event types (and a test payload when the
        # webhook is first configured in the Dashboard) to the same URL.
        # Acknowledge with 200 so Clerk doesn't retry forever.
        return jsonify({"ok": True, "skipped": event_type or "unrecognized_payload"}), 200

    user_id = str((payload.get("data") or {}).get("id") or "").strip()
    if not user_id:
        return jsonify({"error": "missing data.id"}), 400

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

    response = {"ok": not table_errors, "deleted_tables": deleted}
    if table_errors:
        _capture_backend_error(
            "clerk_webhook_delete_partial_failure", RuntimeError(str(table_errors)),
            {"table_errors": table_errors},
        )
        response["table_errors"] = table_errors
        return jsonify(response), 207
    return jsonify(response), 200
