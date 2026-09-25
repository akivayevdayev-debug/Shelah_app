"""
Tests for backend/routes_answer_share.py -- public share links for stored AI
answers (deep-link Phase 4).

The fake Supabase client here is stateful (an in-memory ask_history table
that honours .eq() filters on select and update), unlike the preset-result
_FakeQuery in tests/test_routes_user.py: idempotent sharing and revoke are
only meaningful if a later request sees what an earlier one wrote.
"""

from __future__ import annotations

import re

import pytest
from postgrest.exceptions import APIError

import backend.auth as auth_module
import backend.routes_answer_share as share_module
import backend.routes_user as routes_user_module
from backend.rate_limit import classify_route

OWNER_ID = "user_owner_123"
OTHER_ID = "user_other_456"
ENTRY_ID = "0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88"
OTHER_ENTRY_ID = "7f1e2d3c-4b5a-4968-8776-655443322110"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _row(**overrides):
    row = {
        "id": ENTRY_ID,
        "user_id": OWNER_ID,
        "question": "May I carry on Shabbat inside an eruv?",
        "answer": "Within a valid eruv, carrying is permitted.",
        "sources": [{"ref": "Shulchan Arukh, Orach Chayim 345:1"}],
        "ai_cited_sources": ["Shulchan Arukh, Orach Chayim 345:1"],
        "community": "ashkenazi",
        "mode": "balanced",
        "language": "en",
        "safety_class": "ok",
        "created_at": "2026-09-24T10:00:00Z",
        "share_token": None,
        "is_public": False,
        "shared_at": None,
        "share_revoked_at": None,
    }
    row.update(overrides)
    return row


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, table):
        self._table = table
        self._op = "select"
        self._payload = None
        self._filters = []

    def select(self, columns):
        self._table.select_calls.append(columns)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, key, value):
        self._filters.append((key, value))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        table = self._table
        if table.error is not None and self._op in table.fail_ops:
            raise table.error
        if self._op == "update" and table.before_update:
            table.before_update(table)
        matched = [r for r in table.rows if all(r.get(k) == v for k, v in self._filters)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
            table.update_calls.append((self._payload, list(self._filters)))
        # Every column comes back, whatever was selected -- so the tests
        # below prove the route's own whitelist keeps user_id/id out.
        return _Result([dict(r) for r in matched])


class _FakeTable:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [_row()]
        self.error = None
        self.fail_ops = ("select", "update")
        self.before_update = None
        self.select_calls = []
        self.update_calls = []


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def table(self, name):
        return _Query(self._table)


@pytest.fixture
def table():
    return _FakeTable()


@pytest.fixture
def db(monkeypatch, table):
    client = _FakeClient(table)
    monkeypatch.setattr(share_module, "_get_supabase_client", lambda: client)
    monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
    return table


def _as(monkeypatch, user_id):
    monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sub": user_id, "sid": "sess"})


@pytest.fixture
def owner(monkeypatch):
    _as(monkeypatch, OWNER_ID)
    return OWNER_ID


def _share_url(entry_id=ENTRY_ID):
    return f"/api/user/history/{entry_id}/share"


def _schema_missing():
    return APIError({"code": "42703", "message": "column ask_history.share_token does not exist", "details": None, "hint": None})


class TestOwnerAuth:
    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_without_auth_is_401(self, test_client, db, method):
        response = getattr(test_client, method)(_share_url())
        assert response.status_code == 401

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_authed_but_no_sub_claim_is_401(self, test_client, db, monkeypatch, method):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = getattr(test_client, method)(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 401

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_non_owner_gets_404_and_row_is_untouched(self, test_client, db, monkeypatch, method):
        _as(monkeypatch, OTHER_ID)
        response = getattr(test_client, method)(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 404
        assert db.rows[0]["share_token"] is None
        assert db.rows[0]["is_public"] is False

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_unknown_entry_is_404(self, test_client, db, owner, method):
        response = getattr(test_client, method)(_share_url(OTHER_ENTRY_ID), headers=AUTH_HEADERS)
        assert response.status_code == 404

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_non_uuid_entry_is_404_without_a_query(self, test_client, db, owner, method):
        response = getattr(test_client, method)(_share_url("not-a-uuid"), headers=AUTH_HEADERS)
        assert response.status_code == 404
        assert db.select_calls == []

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_no_supabase_client_is_503(self, test_client, owner, monkeypatch, method):
        monkeypatch.setattr(share_module, "_get_supabase_client", lambda: None)
        response = getattr(test_client, method)(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 503


class TestShareLifecycle:
    def test_unshared_state(self, test_client, db, owner):
        response = test_client.get(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"shared": False}
        assert "no-store" in response.headers["Cache-Control"]

    def test_post_mints_a_token_and_is_idempotent(self, test_client, db, owner):
        first = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert first.status_code == 201
        body = first.get_json()
        assert body["shared"] is True
        assert TOKEN_RE.match(body["share_token"])
        assert body["path"] == f"/a/{body['share_token']}"
        assert db.rows[0]["is_public"] is True
        assert db.rows[0]["shared_at"]

        second = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert second.status_code == 200
        assert second.get_json()["share_token"] == body["share_token"]
        assert len(db.update_calls) == 1

        state = test_client.get(_share_url(), headers=AUTH_HEADERS).get_json()
        assert state == body

    def test_share_update_is_scoped_to_owner_and_unshared_rows(self, test_client, db, owner):
        test_client.post(_share_url(), headers=AUTH_HEADERS)
        _, filters = db.update_calls[0]
        assert ("id", ENTRY_ID) in filters
        assert ("user_id", OWNER_ID) in filters
        assert ("is_public", False) in filters

    def test_concurrent_share_keeps_the_winning_token(self, test_client, db, owner):
        def other_request_wins(table):
            table.rows[0].update({"share_token": "winnerTokenAAAAAAAAAAA", "is_public": True})

        db.before_update = other_request_wins
        response = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["share_token"] == "winnerTokenAAAAAAAAAAA"
        assert db.rows[0]["share_token"] == "winnerTokenAAAAAAAAAAA"

    def test_row_deleted_mid_share_is_404(self, test_client, db, owner):
        db.before_update = lambda table: table.rows.clear()
        response = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 404

    def test_revoke_kills_the_public_link_but_not_the_owner_link(self, test_client, db, owner):
        token = test_client.post(_share_url(), headers=AUTH_HEADERS).get_json()["share_token"]
        assert test_client.get(f"/api/public/answer/{token}").status_code == 200

        revoked = test_client.delete(_share_url(), headers=AUTH_HEADERS)
        assert revoked.status_code == 200
        assert revoked.get_json() == {"shared": False}
        assert db.rows[0]["share_token"] is None
        assert db.rows[0]["is_public"] is False
        assert db.rows[0]["share_revoked_at"]

        assert test_client.get(f"/api/public/answer/{token}").status_code == 404
        owner_view = test_client.get(f"/api/user/history/{ENTRY_ID}", headers=AUTH_HEADERS)
        assert owner_view.status_code == 200
        assert owner_view.get_json()["answer"] == db.rows[0]["answer"]

    def test_resharing_after_revoke_mints_a_new_token(self, test_client, db, owner):
        first = test_client.post(_share_url(), headers=AUTH_HEADERS).get_json()["share_token"]
        test_client.delete(_share_url(), headers=AUTH_HEADERS)
        second = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert second.status_code == 201
        assert second.get_json()["share_token"] != first


class TestOwnerErrors:
    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_schema_missing_is_503_share_unavailable(self, test_client, db, owner, method):
        db.error = _schema_missing()
        response = getattr(test_client, method)(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 503
        assert response.get_json()["code"] == "share_unavailable"

    def test_schema_missing_on_update_is_503(self, test_client, db, owner):
        db.error = APIError({"code": "PGRST204", "message": "Could not find the 'is_public' column"})
        db.fail_ops = ("update",)
        response = test_client.post(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 503
        assert response.get_json()["code"] == "share_unavailable"

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_other_failures_are_500_and_reported(self, test_client, db, owner, monkeypatch, method):
        captured = []
        monkeypatch.setattr(share_module, "_capture_backend_error", lambda *a, **k: captured.append(a))
        db.error = RuntimeError("db down")
        response = getattr(test_client, method)(_share_url(), headers=AUTH_HEADERS)
        assert response.status_code == 500
        assert captured and OWNER_ID not in repr(captured)


class TestPublicAnswer:
    def _shared(self, db, **overrides):
        db.rows[0].update({"share_token": "sharedTokenBBBBBBBBBBB", "is_public": True, **overrides})
        return "sharedTokenBBBBBBBBBBB"

    def test_returns_the_answer_without_pii(self, test_client, db):
        token = self._shared(db)
        response = test_client.get(f"/api/public/answer/{token}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["question"] == db.rows[0]["question"]
        assert body["answer"] == db.rows[0]["answer"]
        assert body["sources"] == db.rows[0]["sources"]
        assert body["ai_cited_sources"] == db.rows[0]["ai_cited_sources"]
        assert body["meta"]["safety_class"] == "ok"
        assert body["public"] is True
        for key in ("user_id", "id", "share_token", "is_public", "shared_at", "share_revoked_at", "safety_class"):
            assert key not in body
        assert OWNER_ID not in response.get_data(as_text=True)
        assert ENTRY_ID not in response.get_data(as_text=True)
        assert "user_id" not in db.select_calls[-1].split(",")
        assert "id" not in db.select_calls[-1].split(",")

    def test_keeps_a_referral_safety_class(self, test_client, db):
        token = self._shared(db, safety_class="mental_health_or_self_harm")
        body = test_client.get(f"/api/public/answer/{token}").get_json()
        assert body["meta"]["safety_class"] == "mental_health_or_self_harm"

    def test_missing_safety_class_defaults_to_ok(self, test_client, db):
        token = self._shared(db, safety_class=None)
        body = test_client.get(f"/api/public/answer/{token}").get_json()
        assert body["meta"]["safety_class"] == "ok"

    def test_no_store_and_noindex_headers(self, test_client, db):
        token = self._shared(db)
        response = test_client.get(f"/api/public/answer/{token}")
        assert "no-store" in response.headers["Cache-Control"]
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"

    def test_works_signed_out_even_with_a_bad_bearer(self, test_client, db):
        token = self._shared(db)
        response = test_client.get(f"/api/public/answer/{token}", headers={"Authorization": "Bearer junk"})
        assert response.status_code == 200

    def test_unknown_token_is_404(self, test_client, db):
        self._shared(db)
        response = test_client.get("/api/public/answer/unknownTokenCCCCCCCCCC")
        assert response.status_code == 404
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"

    def test_private_row_with_a_token_is_404(self, test_client, db):
        token = self._shared(db, is_public=False)
        assert test_client.get(f"/api/public/answer/{token}").status_code == 404

    @pytest.mark.parametrize("token", ["short", "has space in it aaaaaa", "dots.are.not.allowed.x", "a" * 65, "%27%20OR%201%3D1--aaaa"])
    def test_malformed_token_is_404_without_a_query(self, test_client, db, token):
        response = test_client.get(f"/api/public/answer/{token}")
        assert response.status_code == 404
        assert "no-store" in response.headers["Cache-Control"]
        assert db.select_calls == []

    def test_no_supabase_client_is_503(self, test_client, monkeypatch):
        monkeypatch.setattr(share_module, "_get_supabase_client", lambda: None)
        response = test_client.get("/api/public/answer/sharedTokenBBBBBBBBBBB")
        assert response.status_code == 503

    def test_schema_missing_is_503_share_unavailable(self, test_client, db):
        db.error = _schema_missing()
        response = test_client.get("/api/public/answer/sharedTokenBBBBBBBBBBB")
        assert response.status_code == 503
        assert response.get_json()["code"] == "share_unavailable"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"

    def test_other_failures_are_500(self, test_client, db, monkeypatch):
        monkeypatch.setattr(share_module, "_capture_backend_error", lambda *a, **k: None)
        db.error = RuntimeError("db down")
        response = test_client.get("/api/public/answer/sharedTokenBBBBBBBBBBB")
        assert response.status_code == 500


def test_public_answer_route_is_rate_limited_as_fanout():
    assert classify_route("/api/public/answer/sharedTokenBBBBBBBBBBB") == "fanout"
