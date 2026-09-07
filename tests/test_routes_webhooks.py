"""
Tests for backend/routes_webhooks.py (plan.md §39.2).

Covers:
  - CLERK_WEBHOOK_SIGNING_SECRET unset -> 503 (fails closed, matches the
    CRON_SECRET-gated retention_enforce() pattern in routes_privacy.py)
  - missing / wrong / tampered-body Svix signature -> 401 (signature
    verification IS this endpoint's authentication -- there is no Clerk
    JWT to check once the identity may already be gone)
  - a timestamp outside the tolerance window -> 401
  - valid signature, an event type other than user.deleted -> 200, no
    table deletes attempted (acknowledged so Clerk doesn't retry forever)
  - valid signature, user.deleted -> cascades delete over every
    _USER_DATA_TABLES row for that user, exactly like delete_account()
  - a replayed event (the identical signed request sent twice) is a
    no-op the second time, not an error -- Svix delivers at-least-once
  - one table failing to delete is reported (207) without blocking the
    others
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import backend.routes_webhooks as routes_webhooks_module

from tests.test_routes_privacy import _FakeSupabaseClient, ALL_TABLES

_TEST_SECRET = "whsec_" + base64.b64encode(b"test-signing-secret-material").decode()
_FAKE_CLERK_USER_ID = "user_webhook_fake"


def _sign(secret, svix_id, timestamp, body):
    raw_secret = secret[len("whsec_"):] if secret.startswith("whsec_") else secret
    key = base64.b64decode(raw_secret)
    signed_content = f"{svix_id}.{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(key, signed_content, hashlib.sha256).digest()
    return f"v1,{base64.b64encode(digest).decode('utf-8')}"


def _user_deleted_payload(user_id=_FAKE_CLERK_USER_ID):
    return {"type": "user.deleted", "data": {"id": user_id}}


def _post_webhook(test_client, monkeypatch, payload, secret=_TEST_SECRET,
                   svix_id="msg_test_123", timestamp=None, signature=None,
                   tamper_body_after_signing=False, omit_headers=()):
    if secret is not None:
        monkeypatch.setenv("CLERK_WEBHOOK_SIGNING_SECRET", secret)
    else:
        monkeypatch.delenv("CLERK_WEBHOOK_SIGNING_SECRET", raising=False)

    body = json.dumps(payload)
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = signature if signature is not None else _sign(secret or "", svix_id, ts, body)

    if tamper_body_after_signing:
        body = body + " "

    headers = {
        "Content-Type": "application/json",
        "svix-id": svix_id,
        "svix-timestamp": ts,
        "svix-signature": sig,
    }
    for key in omit_headers:
        headers.pop(key, None)

    return test_client.post("/api/webhooks/clerk", data=body, headers=headers)


class TestSignatureVerification:
    def test_secret_not_configured_is_503(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(), secret=None,
        )
        assert response.status_code == 503

    def test_missing_signature_header_is_401(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(),
            omit_headers=("svix-signature",),
        )
        assert response.status_code == 401

    def test_missing_id_header_is_401(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(),
            omit_headers=("svix-id",),
        )
        assert response.status_code == 401

    def test_wrong_signature_is_401(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(),
            signature="v1," + base64.b64encode(b"not-the-real-signature").decode(),
        )
        assert response.status_code == 401

    def test_signature_from_wrong_secret_is_401(self, test_client, monkeypatch):
        wrong_secret = "whsec_" + base64.b64encode(b"a-different-secret").decode()
        body = json.dumps(_user_deleted_payload())
        ts = str(int(time.time()))
        wrong_sig = _sign(wrong_secret, "msg_test_123", ts, body)
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(),
            timestamp=ts, signature=wrong_sig,
        )
        assert response.status_code == 401

    def test_tampered_body_after_signing_is_401(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(),
            tamper_body_after_signing=True,
        )
        assert response.status_code == 401

    def test_stale_timestamp_is_401(self, test_client, monkeypatch):
        stale_timestamp = int(time.time()) - (routes_webhooks_module._TIMESTAMP_TOLERANCE_SECONDS + 60)
        response = _post_webhook(
            test_client, monkeypatch, _user_deleted_payload(), timestamp=stale_timestamp,
        )
        assert response.status_code == 401

    def test_valid_signature_is_accepted(self, test_client, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: client)
        response = _post_webhook(test_client, monkeypatch, _user_deleted_payload())
        assert response.status_code == 200


class TestUserDeletedCascade:
    def test_non_user_deleted_event_is_acknowledged_without_deleting(
        self, test_client, monkeypatch
    ):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: client)
        response = _post_webhook(
            test_client, monkeypatch, {"type": "user.created", "data": {"id": _FAKE_CLERK_USER_ID}},
        )
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert client.queries == {}

    def test_missing_data_id_is_400(self, test_client, monkeypatch):
        response = _post_webhook(
            test_client, monkeypatch, {"type": "user.deleted", "data": {}},
        )
        assert response.status_code == 400

    def test_no_supabase_client_is_503(self, test_client, monkeypatch):
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: None)
        response = _post_webhook(test_client, monkeypatch, _user_deleted_payload())
        assert response.status_code == 503

    def test_deletes_every_user_data_table_for_that_user(self, test_client, monkeypatch):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: client)

        response = _post_webhook(test_client, monkeypatch, _user_deleted_payload())

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert all(body["deleted_tables"].values())
        for table_name in ALL_TABLES:
            query = client.queries[table_name][0]
            assert ("delete", (), {}) in query.calls
            assert ("eq", ("user_id", _FAKE_CLERK_USER_ID), {}) in query.calls

    def test_replayed_event_is_idempotent_not_an_error(self, test_client, monkeypatch):
        """Svix delivers at-least-once -- a duplicate delivery of the exact
        same signed request must still succeed, deleting nothing new the
        second time rather than erroring."""
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: client)

        secret = _TEST_SECRET
        svix_id = "msg_replayed"
        payload = _user_deleted_payload()
        body = json.dumps(payload)
        ts = str(int(time.time()))
        sig = _sign(secret, svix_id, ts, body)

        first = _post_webhook(
            test_client, monkeypatch, payload, secret=secret, svix_id=svix_id,
            timestamp=ts, signature=sig,
        )
        second = _post_webhook(
            test_client, monkeypatch, payload, secret=secret, svix_id=svix_id,
            timestamp=ts, signature=sig,
        )
        assert first.status_code == second.status_code == 200
        assert first.get_json()["ok"] is second.get_json()["ok"] is True

    def test_one_table_failure_reports_207_without_blocking_others(
        self, test_client, monkeypatch
    ):
        prefs_table = list(ALL_TABLES)[0]
        client = _FakeSupabaseClient(table_errors={prefs_table: RuntimeError("db down")})
        monkeypatch.setattr(routes_webhooks_module, "_get_supabase_client", lambda: client)

        response = _post_webhook(test_client, monkeypatch, _user_deleted_payload())

        assert response.status_code == 207
        body = response.get_json()
        assert body["ok"] is False
        assert body["deleted_tables"][prefs_table] is False
        other_tables = [t for t in ALL_TABLES if t != prefs_table]
        assert all(body["deleted_tables"][t] for t in other_tables)
        assert prefs_table in body["table_errors"]
        assert "db down" not in body["table_errors"][prefs_table]
