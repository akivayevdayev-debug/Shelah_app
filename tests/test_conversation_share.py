"""
Tests for backend/routes_conversation_share.py -- public share links for
multi-turn conversations. Uses a small stateful in-memory Supabase fake (the
preset-result fake in test_routes_conversations can't show that a later read
sees an earlier write, which is what a snapshot and a revoke are about).
"""

from __future__ import annotations

import re

import pytest

import backend.auth as auth_module
import backend.routes_answer_share as answer_share_module
import backend.routes_conversation_share as share_module
import backend.routes_conversations as conversations_module

OWNER = "user_owner_123"
OTHER = "user_other_456"
CONV_ID = "11111111-1111-4111-8111-111111111111"
DELETED_CONV_ID = "22222222-2222-4222-8222-222222222222"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def _msg(n, role, content, **overrides):
    row = {
        "id": f"00000000-0000-4000-8000-{n:012d}",
        "conversation_id": CONV_ID,
        "role": role,
        "content": content,
        "status": "complete",
        "superseded_by": None,
        "created_at": f"2026-10-06T10:00:{n:02d}Z",
        "citations": [],
    }
    row.update(overrides)
    return row


def _conversations():
    return [
        {"id": CONV_ID, "user_id": OWNER, "title": "Shabbat candles", "minhag": "Ashkenaz",
         "created_at": "2026-10-06T10:00:00Z", "deleted_at": None,
         "share_id": None, "share_snapshot_msg_id": None, "shared_at": None},
        {"id": DELETED_CONV_ID, "user_id": OWNER, "title": "gone", "minhag": None,
         "created_at": "2026-10-06T09:00:00Z", "deleted_at": "2026-10-06T09:30:00Z",
         "share_id": "deletedTokenAAAAAAAAAAAA", "share_snapshot_msg_id": _msg(1, "user", "x")["id"],
         "shared_at": None},
    ]


def _messages():
    return [
        _msg(1, "user", "When do I light candles?"),
        _msg(2, "assistant", "Eighteen minutes before sunset [1].",
             citations=[{"ordinal": 0, "source_ref": "Shulchan Arukh, Orach Chayim 261:2",
                         "excerpt_he": None, "excerpt_en": "Candles", "url": None}]),
        _msg(3, "user", "And on a Friday in winter?"),
        _msg(4, "assistant", "The same rule applies."),
    ]


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._op = "select"
        self._payload = None
        self._filters = []
        self._order = None
        self._limit = None

    def select(self, *a, **k):
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def eq(self, key, value):
        self._filters.append(lambda r: r.get(key) == value)
        return self

    def is_(self, key, value):
        self._filters.append(lambda r: r.get(key) is None)
        return self

    def lte(self, key, value):
        self._filters.append(lambda r: r.get(key) <= value)
        return self

    def order(self, key, desc=False, **k):
        self._order = (key, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        matched = [r for r in self._rows if all(f(r) for f in self._filters)]
        if self._op == "update":
            for r in matched:
                r.update(self._payload)
            return _Result([dict(r) for r in matched])
        if self._order:
            key, desc = self._order
            matched.sort(key=lambda r: r[key], reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return _Result([dict(r) for r in matched])


class _Client:
    def __init__(self):
        self.tables = {"conversations": _conversations(), "messages": _messages(), "ask_history": []}

    def table(self, name):
        return _Query(self.tables[name])


@pytest.fixture
def db(monkeypatch):
    client = _Client()
    # Owner routes use the RLS-backed client, the public read the service one.
    monkeypatch.setattr(conversations_module, "_get_user_scoped_supabase_client", lambda: client)
    monkeypatch.setattr(answer_share_module, "_get_supabase_client", lambda: client)
    return client


def _as(monkeypatch, user_id):
    monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sub": user_id, "sid": "s"})


@pytest.fixture
def owner(monkeypatch):
    _as(monkeypatch, OWNER)


def _url(conversation_id=CONV_ID):
    return f"/api/conversations/{conversation_id}/share"


class TestOwnerRoutes:
    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_without_auth_is_401(self, test_client, db, method):
        assert getattr(test_client, method)(_url()).status_code == 401

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_someone_elses_conversation_is_404_and_untouched(self, test_client, db, monkeypatch, method):
        _as(monkeypatch, OTHER)
        assert getattr(test_client, method)(_url(), headers=AUTH_HEADERS).status_code == 404
        assert db.tables["conversations"][0]["share_id"] is None

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_a_deleted_conversation_is_404(self, test_client, db, owner, method):
        assert getattr(test_client, method)(_url(DELETED_CONV_ID), headers=AUTH_HEADERS).status_code == 404

    @pytest.mark.parametrize("method", ["get", "post", "delete"])
    def test_a_non_uuid_id_is_404(self, test_client, db, owner, method):
        assert getattr(test_client, method)(_url("nope"), headers=AUTH_HEADERS).status_code == 404

    def test_unshared_state(self, test_client, db, owner):
        response = test_client.get(_url(), headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"shared": False}

    def test_first_share_mints_a_token_and_pins_the_latest_turn(self, test_client, db, owner):
        response = test_client.post(_url(), headers=AUTH_HEADERS)
        assert response.status_code == 201
        body = response.get_json()
        assert body["shared"] is True
        assert TOKEN_RE.match(body["share_token"])
        assert body["path"] == f"/a/{body['share_token']}"
        row = db.tables["conversations"][0]
        assert row["share_id"] == body["share_token"]
        assert row["share_snapshot_msg_id"] == _msg(4, "assistant", "")["id"]
        assert row["shared_at"]

    def test_sharing_again_keeps_the_token_and_moves_the_snapshot(self, test_client, db, owner):
        token = test_client.post(_url(), headers=AUTH_HEADERS).get_json()["share_token"]
        db.tables["messages"].extend([_msg(5, "user", "One more?"), _msg(6, "assistant", "Yes.")])
        again = test_client.post(_url(), headers=AUTH_HEADERS)
        assert again.status_code == 200
        assert again.get_json()["share_token"] == token
        assert db.tables["conversations"][0]["share_snapshot_msg_id"] == _msg(6, "assistant", "")["id"]

    def test_a_failed_or_unanswered_turn_is_never_the_snapshot(self, test_client, db, owner):
        db.tables["messages"].extend([
            _msg(5, "user", "Pending?"),
            _msg(6, "assistant", "", status="error"),
        ])
        db.tables["messages"][-2]["status"] = "streaming"
        test_client.post(_url(), headers=AUTH_HEADERS)
        assert db.tables["conversations"][0]["share_snapshot_msg_id"] == _msg(4, "assistant", "")["id"]

    def test_an_empty_conversation_cannot_be_shared(self, test_client, db, owner):
        db.tables["messages"].clear()
        response = test_client.post(_url(), headers=AUTH_HEADERS)
        assert response.status_code == 409
        assert response.get_json()["code"] == "empty_conversation"
        assert db.tables["conversations"][0]["share_id"] is None

    def test_revoke_clears_the_token_and_the_snapshot(self, test_client, db, owner):
        test_client.post(_url(), headers=AUTH_HEADERS)
        response = test_client.delete(_url(), headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"shared": False}
        row = db.tables["conversations"][0]
        assert row["share_id"] is None
        assert row["share_snapshot_msg_id"] is None
        assert test_client.get(_url(), headers=AUTH_HEADERS).get_json() == {"shared": False}

    def test_a_database_error_is_a_500_not_a_leak(self, test_client, db, owner, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("db down")

        monkeypatch.setattr(share_module, "_own_row", boom)
        for method in ("get", "post", "delete"):
            response = getattr(test_client, method)(_url(), headers=AUTH_HEADERS)
            assert response.status_code == 500
            assert "db down" not in response.get_data(as_text=True)


class TestPublicRead:
    @pytest.fixture
    def token(self, test_client, db, owner):
        return test_client.post(_url(), headers=AUTH_HEADERS).get_json()["share_token"]

    def test_anyone_with_the_link_reads_the_chat(self, test_client, db, token):
        response = test_client.get(f"/api/public/answer/{token}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["conversation"] is True
        assert body["public"] is True
        assert body["title"] == "Shabbat candles"
        assert body["minhag"] == "Ashkenaz"
        assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user", "assistant"]
        assert body["messages"][1]["citations"][0]["source_ref"] == "Shulchan Arukh, Orach Chayim 261:2"
        assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
        assert "no-store" in response.headers["Cache-Control"]

    def test_the_payload_has_no_owner_or_row_ids(self, test_client, db, token):
        text = test_client.get(f"/api/public/answer/{token}").get_data(as_text=True)
        assert OWNER not in text
        assert CONV_ID not in text
        assert '"id"' not in text
        assert "user_id" not in text

    def test_turns_added_after_sharing_stay_private(self, test_client, db, token):
        db.tables["messages"].extend([_msg(5, "user", "Secret follow-up"), _msg(6, "assistant", "Secret answer")])
        body = test_client.get(f"/api/public/answer/{token}").get_json()
        assert [m["content"] for m in body["messages"]][-1] == "The same rule applies."
        assert "Secret" not in str(body)

    def test_superseded_and_unfinished_turns_are_left_out(self, test_client, db, token):
        db.tables["messages"][3]["superseded_by"] = _msg(9, "assistant", "")["id"]
        db.tables["messages"][2]["status"] = "error"
        body = test_client.get(f"/api/public/answer/{token}").get_json()
        assert [m["content"] for m in body["messages"]] == [
            "When do I light candles?", "Eighteen minutes before sunset [1].",
        ]

    def test_a_revoked_link_is_404_and_a_new_share_mints_a_new_token(self, test_client, db, token):
        test_client.delete(_url(), headers=AUTH_HEADERS)
        assert test_client.get(f"/api/public/answer/{token}").status_code == 404
        fresh = test_client.post(_url(), headers=AUTH_HEADERS).get_json()["share_token"]
        assert fresh != token

    def test_a_deleted_conversation_is_404(self, test_client, db):
        assert test_client.get("/api/public/answer/deletedTokenAAAAAAAAAAAA").status_code == 404

    def test_a_snapshot_message_that_is_gone_is_404(self, test_client, db, token):
        db.tables["messages"][:] = [m for m in db.tables["messages"] if m["id"] != _msg(4, "assistant", "")["id"]]
        assert test_client.get(f"/api/public/answer/{token}").status_code == 404

    def test_an_unknown_token_is_404(self, test_client, db):
        assert test_client.get("/api/public/answer/neverIssuedTokenAAAAAA").status_code == 404

    def test_a_database_error_is_a_500(self, test_client, db, token, monkeypatch):
        def boom(supabase, tok):
            raise RuntimeError("db down")

        monkeypatch.setattr(answer_share_module, "public_conversation_payload", boom)
        response = test_client.get(f"/api/public/answer/{token}")
        assert response.status_code == 500
        assert "db down" not in response.get_data(as_text=True)
