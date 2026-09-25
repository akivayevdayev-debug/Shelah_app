"""
Tests for backend/routes_conversations.py -- Step 1 (data model + routing)
of the multi-turn AI conversation feature. Mirrors the fake-Supabase-client
pattern in tests/test_routes_user.py, extended with a per-table map since a
single route here can legitimately touch more than one table (e.g. creating
a message also inserts citations and bumps the parent conversation's
updated_at).

Covers, for every route: without auth -> 401; happy path; Supabase-not-
configured -> 503; and application-layer cross-user isolation (a caller can
never read/mutate a conversation_id that isn't filtered by their own
verified user_id).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import backend.auth as auth_module
import backend.cost_gates as cost_gates_module
import backend.logging_setup as logging_setup_module
import backend.routes_conversations as routes_conversations_module
from backend.rate_limit import classify_route

FAKE_USER_ID = "user_test_fake_123"
OTHER_USER_ID = "user_test_attacker_target_456"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(
        auth_module,
        "_verify_clerk_token",
        lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
    )
    return FAKE_USER_ID


class _CostGateSpy:
    """Stand-ins for cost_meter's two async gates, patched onto
    backend/cost_gates.py (whose evaluate_cost_gates_sync() the route
    calls) by the autouse fixture below so no test ever reaches the real
    Supabase budget RPC / usage read. Still driven through the real
    loop-bridge hop (submit_with_context + asyncio.run on claude's bridge
    executor), so that plumbing is exercised as-is. Defaults: breaker not
    tripped, budget allowed with a bound reservation id -- mirroring what
    the real check_user_budget_and_enforce() does on success."""

    def __init__(self):
        self.tripped = False
        self.allowed = True
        self.reservation_id = "res-123"
        self.breaker_error = None
        self.budget_calls = []
        self.breaker_calls = 0

    async def breaker(self):
        self.breaker_calls += 1
        if self.breaker_error is not None:
            raise self.breaker_error
        return {"tripped": self.tripped, "total_usd": 12.5, "threshold_usd": 10.0, "configured": True}

    async def budget(self, user_id, client_ip=""):
        self.budget_calls.append((user_id, client_ip))
        if self.allowed and self.reservation_id:
            logging_setup_module.bind_budget_reservation(self.reservation_id)
        return {"allowed": self.allowed, "total_usd": 1.9876, "threshold_usd": 2.0}


@pytest.fixture(autouse=True)
def cost_gates(monkeypatch):
    spy = _CostGateSpy()
    monkeypatch.setattr(cost_gates_module, "is_global_cost_breaker_tripped", spy.breaker)
    monkeypatch.setattr(cost_gates_module, "check_user_budget_and_enforce", spy.budget)
    return spy


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable fake query builder for one table. `.execute()` returns a
    preset `_FakeResult` (or raises a preset error); every chain method
    records its call and returns self."""

    def __init__(self, data=None, error=None, update_data=None, insert_data=None, select_data=None):
        self._data = data if data is not None else []
        # Same idea as `insert_data`, for select()s: a retry reads the
        # messages table three times (the failed answer, the question it
        # answered, then the history) before writing -- each select()'s
        # execute() pops the next result LIST off the front, falling back
        # to `data` once exhausted (or when not given).
        self._select_data_queue = list(select_data) if select_data is not None else None
        # A select() (the ownership pre-fetch) and a later update() on the
        # SAME _FakeQuery instance would otherwise both return `_data` --
        # indistinguishable in a route that fetches then mutates the same
        # row. `update_data` lets a test give the update() call its own
        # (e.g. empty) result independent of the pre-fetch's.
        self._update_data = data if update_data is None else update_data
        # ask_in_conversation() insert()s into the SAME table (messages)
        # twice per request (user turn, then assistant turn) -- a plain
        # `data` fallback would echo the same row back for both. When
        # `insert_data` (a list of ROWS, one per expected insert() call) is
        # given, each insert()'s execute() pops the next row off the front;
        # once exhausted (or when not given at all) it falls back to `data`,
        # so every existing single-insert test is unaffected.
        self._insert_data_queue = list(insert_data) if insert_data is not None else None
        self._error = error
        self._op = "select"
        self.insert_calls = []
        self.update_calls = []
        self.eq_calls = []
        self.is_calls = []
        self.lt_calls = []
        self.order_calls = []

    def select(self, *a, **k):
        self._op = "select"
        return self

    def insert(self, *a, **k):
        self._op = "insert"
        self.insert_calls.append((a, k))
        return self

    def update(self, *a, **k):
        self._op = "update"
        self.update_calls.append((a, k))
        return self

    def delete(self, *a, **k):
        self._op = "delete"
        return self

    def eq(self, *a, **k):
        self.eq_calls.append(a)
        return self

    def is_(self, *a, **k):
        self.is_calls.append(a)
        return self

    def lt(self, *a, **k):
        self.lt_calls.append(a)
        return self

    def order(self, *a, **k):
        self.order_calls.append((a, k))
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        if self._op == "update":
            return _FakeResult(self._update_data)
        if self._op == "insert" and self._insert_data_queue is not None:
            row = self._insert_data_queue.pop(0) if self._insert_data_queue else None
            return _FakeResult([row] if row is not None else [])
        if self._op == "select" and self._select_data_queue:
            return _FakeResult(self._select_data_queue.pop(0))
        return _FakeResult(self._data)


class _FakeSupabaseClient:
    """Per-table fake client: `.table(name)` returns a distinct `_FakeQuery`
    (created on first access, or preset via `queries=`), so a route that
    touches conversations/messages/citations in one request can be asserted
    on independently per table."""

    def __init__(self, queries=None):
        self._queries = dict(queries or {})

    def table(self, name):
        if name not in self._queries:
            self._queries[name] = _FakeQuery()
        return self._queries[name]


CONV_TABLE = "conversations"
MSG_TABLE = "messages"
CIT_TABLE = "citations"


class TestCreateConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.post("/api/conversations", json={})
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(routes_conversations_module, "_get_supabase_client", lambda: None)
        monkeypatch.setattr(routes_conversations_module, "STRICT_SUPABASE_RLS", False)
        response = test_client.post("/api/conversations", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_happy_path_creates_conversation(self, test_client, authed, monkeypatch):
        created_row = {"id": "conv-1", "title": "", "minhag": "Ashkenaz", "user_id": FAKE_USER_ID}
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[created_row])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations", json={"minhag": "Ashkenaz"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        assert response.get_json()["id"] == "conv-1"
        insert_payload = client.table(CONV_TABLE).insert_calls[0][0][0]
        assert insert_payload["user_id"] == FAKE_USER_ID
        assert insert_payload["minhag"] == "Ashkenaz"

    def test_ignores_client_supplied_user_id_in_body(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        test_client.post(
            "/api/conversations",
            json={"minhag": "Sefard", "user_id": OTHER_USER_ID},
            headers=AUTH_HEADERS,
        )
        insert_payload = client.table(CONV_TABLE).insert_calls[0][0][0]
        assert insert_payload["user_id"] == FAKE_USER_ID

    def test_supabase_exception_returns_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(error=RuntimeError("db down"))})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 500


class TestListConversations:
    def test_without_auth_is_401(self, test_client):
        response = test_client.get("/api/conversations")
        assert response.status_code == 401

    def test_happy_path_returns_items(self, test_client, authed, monkeypatch):
        rows = [{"id": "conv-1", "title": "Kashrut question"}]
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=rows)})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.get("/api/conversations", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"items": rows}

    def test_scopes_list_to_caller_own_user_id(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        test_client.get(f"/api/conversations?user_id={OTHER_USER_ID}", headers=AUTH_HEADERS)
        query = client.table(CONV_TABLE)
        assert ("user_id", FAKE_USER_ID) in query.eq_calls
        assert ("user_id", OTHER_USER_ID) not in query.eq_calls


class TestGetConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.get("/api/conversations/conv-1")
        assert response.status_code == 401

    def test_missing_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.get("/api/conversations/does-not-exist", headers=AUTH_HEADERS)
        assert response.status_code == 404

    def test_happy_path_returns_conversation_with_messages(self, test_client, authed, monkeypatch):
        conv_row = {"id": "conv-1", "title": "Kashrut question", "minhag": "Ashkenaz"}
        msg_rows = [
            {"id": "m1", "role": "user", "content": "May I...", "citations": []},
            {"id": "m2", "role": "assistant", "content": "Yes, because...", "citations": [
                {"id": "c1", "ordinal": 1, "source_ref": "Shulchan Aruch OC 1:1"}
            ]},
        ]
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[conv_row]),
            MSG_TABLE: _FakeQuery(data=msg_rows),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.get("/api/conversations/conv-1", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == "conv-1"
        assert body["messages"] == msg_rows

    def test_cannot_fetch_another_users_conversation(self, test_client, authed, monkeypatch):
        """Even with the right id, a row owned by someone else never comes
        back -- the ownership fetch filters on id AND the caller's own
        user_id, so this always renders as 404, never as someone else's
        conversation."""
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        test_client.get("/api/conversations/someone-elses-conv", headers=AUTH_HEADERS)
        query = client.table(CONV_TABLE)
        assert ("id", "someone-elses-conv") in query.eq_calls
        assert ("user_id", FAKE_USER_ID) in query.eq_calls


class TestCreateMessage:
    def test_without_auth_is_401(self, test_client):
        response = test_client.post("/api/conversations/conv-1/messages", json={"role": "user", "content": "hi"})
        assert response.status_code == 401

    def test_invalid_role_is_400(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "system", "content": "hi"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 400

    def test_missing_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/does-not-exist/messages",
            json={"role": "user", "content": "hi"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 404

    def test_happy_path_inserts_message_and_citations_and_bumps_conversation(
        self, test_client, authed, monkeypatch
    ):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[{"id": "m1", "role": "assistant", "content": "Yes."}]),
            CIT_TABLE: _FakeQuery(data=[{"id": "c1", "ordinal": 0, "source_ref": "OC 1:1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={
                "role": "assistant",
                "content": "Yes.",
                "citations": [{"source_ref": "OC 1:1"}],
            },
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["id"] == "m1"
        assert body["citations"] == [{"id": "c1", "ordinal": 0, "source_ref": "OC 1:1"}]

        msg_insert = client.table(MSG_TABLE).insert_calls[0][0][0]
        assert msg_insert["conversation_id"] == "conv-1"
        assert msg_insert["role"] == "assistant"

        cit_insert = client.table(CIT_TABLE).insert_calls[0][0][0]
        assert cit_insert[0]["message_id"] == "m1"

        assert client.table(CONV_TABLE).update_calls, "conversation.updated_at must be bumped"

    def test_cannot_post_message_to_another_users_conversation(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/someone-elses-conv/messages",
            json={"role": "user", "content": "hi"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 404


class TestUpdateConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.patch("/api/conversations/conv-1", json={"title": "New title"})
        assert response.status_code == 401

    def test_no_fields_is_400(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.patch("/api/conversations/conv-1", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 400

    def test_rename_marks_title_as_custom(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "title": "old"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.patch(
            "/api/conversations/conv-1", json={"title": "My renamed thread"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 200
        update_payload = client.table(CONV_TABLE).update_calls[0][0][0]
        assert update_payload["title"] == "My renamed thread"
        assert update_payload["title_is_custom"] is True

    def test_pin_sets_pinned_at_unpin_clears_it(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)

        test_client.patch("/api/conversations/conv-1", json={"pinned": True}, headers=AUTH_HEADERS)
        assert client.table(CONV_TABLE).update_calls[-1][0][0]["pinned_at"] is not None

        test_client.patch("/api/conversations/conv-1", json={"pinned": False}, headers=AUTH_HEADERS)
        assert client.table(CONV_TABLE).update_calls[-1][0][0]["pinned_at"] is None

    def test_missing_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.patch(
            "/api/conversations/does-not-exist", json={"title": "x"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 404


class TestDeleteConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.delete("/api/conversations/conv-1")
        assert response.status_code == 401

    def test_happy_path_soft_deletes(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.delete("/api/conversations/conv-1", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        update_payload = client.table(CONV_TABLE).update_calls[0][0][0]
        assert "deleted_at" in update_payload

    def test_scopes_delete_to_caller_own_user_id(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        test_client.delete("/api/conversations/conv-1", headers=AUTH_HEADERS)
        query = client.table(CONV_TABLE)
        assert ("user_id", FAKE_USER_ID) in query.eq_calls

    def test_supabase_exception_returns_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(error=RuntimeError("db down"))})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.delete("/api/conversations/conv-1", headers=AUTH_HEADERS)
        assert response.status_code == 500


class TestNoSubClaimIsUnauthorized:
    """`require_clerk_auth` lets a request through once the bearer token
    decodes at all; each route still separately checks for a `sub` claim
    before doing anything, since a token can decode to claims without one."""

    @pytest.mark.parametrize("method, path, body", [
        ("post", "/api/conversations", {}),
        ("get", "/api/conversations", None),
        ("get", "/api/conversations/conv-1", None),
        ("post", "/api/conversations/conv-1/messages", {"role": "user", "content": "hi"}),
        ("patch", "/api/conversations/conv-1", {"title": "x"}),
        ("delete", "/api/conversations/conv-1", None),
        ("post", "/api/conversations/conv-1/ask", {"question": "hi"}),
        ("post", "/api/conversations/conv-1/restore", None),
    ])
    def test_returns_401(self, test_client, monkeypatch, method, path, body):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = getattr(test_client, method)(path, json=body, headers=AUTH_HEADERS)
        assert response.status_code == 401


class TestStrictRlsWithoutSession:
    def test_returns_403(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(routes_conversations_module, "STRICT_SUPABASE_RLS", True)
        response = test_client.post("/api/conversations", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 403


class TestExceptionPaths:
    @pytest.mark.parametrize("method, path, body", [
        ("get", "/api/conversations", None),
        ("get", "/api/conversations/conv-1", None),
        ("post", "/api/conversations/conv-1/messages", {"role": "user", "content": "hi"}),
        ("patch", "/api/conversations/conv-1", {"title": "x"}),
        ("post", "/api/conversations/conv-1/restore", None),
    ])
    def test_returns_500(self, test_client, authed, monkeypatch, method, path, body):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(error=RuntimeError("db down"))})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = getattr(test_client, method)(path, json=body, headers=AUTH_HEADERS)
        assert response.status_code == 500


class TestInsertReturnedNoRows:
    """Postgrest can return an empty `.data` on an insert Supabase itself
    accepted (e.g. a `select=minimal` misconfiguration) -- both create
    routes treat that as a 500 rather than crashing on `rows[0]`."""

    def test_create_conversation_with_empty_insert_result_is_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations", json={}, headers=AUTH_HEADERS)
        assert response.status_code == 500

    def test_create_message_with_empty_insert_result_is_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "user", "content": "hi"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 500


class TestUpdateConversationFallback:
    def test_returns_merged_row_when_update_result_is_empty(self, test_client, authed, monkeypatch):
        """Some postgrest configs return no representation on update; the
        route falls back to the pre-fetched row merged with the requested
        changes instead of returning an empty body."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "title": "old"}], update_data=[]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.patch(
            "/api/conversations/conv-1", json={"title": "new title"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 200
        assert response.get_json()["title"] == "new title"


class TestInsertMessageCitations:
    def test_skips_non_dict_entries_and_inserts_valid_ones(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[{"id": "m1"}]),
            CIT_TABLE: _FakeQuery(data=[{"id": "c1", "source_ref": "OC 1:1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "assistant", "content": "x", "citations": [123, {"source_ref": "OC 1:1"}]},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        cit_insert_rows = client.table(CIT_TABLE).insert_calls[0][0][0]
        assert len(cit_insert_rows) == 1

    def test_all_non_dict_entries_skips_citations_insert_entirely(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[{"id": "m1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "assistant", "content": "x", "citations": [123, "not-a-dict"]},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        assert response.get_json()["citations"] == []
        assert CIT_TABLE not in client._queries

    def test_no_citations_key_skips_citation_insert_entirely(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[{"id": "m1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "assistant", "content": "x"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        assert response.get_json()["citations"] == []
        assert CIT_TABLE not in client._queries


class TestCreateMessageInvalidStatus:
    def test_invalid_status_is_400(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/messages",
            json={"role": "user", "content": "hi", "status": "bogus"},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 400


class TestConversationsClientErrorAcrossRoutes:
    """`if error_response: return error_response` is duplicated once per
    route (each early-returns from _conversations_client()); TestCreate
    Conversation's 503 test only exercises create_conversation's copy of
    that line, so every other route needs its own hit to be covered."""

    @pytest.mark.parametrize("method, path, body", [
        ("get", "/api/conversations", None),
        ("get", "/api/conversations/conv-1", None),
        ("post", "/api/conversations/conv-1/messages", {"role": "user", "content": "hi"}),
        ("patch", "/api/conversations/conv-1", {"title": "x"}),
        ("delete", "/api/conversations/conv-1", None),
        ("post", "/api/conversations/conv-1/ask", {"question": "hi"}),
        ("post", "/api/conversations/conv-1/restore", None),
    ])
    def test_returns_503_when_supabase_not_configured(
        self, test_client, authed, monkeypatch, method, path, body
    ):
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(routes_conversations_module, "_get_supabase_client", lambda: None)
        monkeypatch.setattr(routes_conversations_module, "STRICT_SUPABASE_RLS", False)
        response = getattr(test_client, method)(path, json=body, headers=AUTH_HEADERS)
        assert response.status_code == 503


def _patch_ai_pipeline(
    monkeypatch, *, security_blocked=False, raise_exc=None,
    answer_text="The answer.", primary_sources=None,
):
    """Patches every app.py AI-synthesis helper ask_in_conversation() calls,
    so these tests exercise only routes_conversations.py's own wiring (turn
    persistence, minhag lock, history assembly) -- the retrieval/synthesis
    orchestration itself is app.py's responsibility and already covered by
    tests/test_ask.py and tests/test_cov_app_py.py. Returns a dict the
    caller can inspect for the kwargs _dispatch_ask_ai_synthesis_call() was
    given (only populated when raise_exc is None)."""
    monkeypatch.setattr(routes_conversations_module, "get_engine", lambda: object())
    monkeypatch.setattr(
        routes_conversations_module, "_collect_ask_question_context",
        lambda question, canonical_lens, user_id, answer_language, engine: {
            "primary_sources": primary_sources or [],
        },
    )

    if raise_exc is not None:
        def _dispatch(*a, **k):
            raise raise_exc
        monkeypatch.setattr(routes_conversations_module, "_dispatch_ask_ai_synthesis_call", _dispatch)
        return {}

    seen_dispatch_kwargs = {}

    def _dispatch(question, mode, canonical_lens, answer_language, ctx, engine, conversation_history=None):
        seen_dispatch_kwargs.update({
            "question": question,
            "mode": mode,
            "canonical_lens": canonical_lens,
            "answer_language": answer_language,
            "conversation_history": conversation_history,
        })
        return {"raw": True}

    monkeypatch.setattr(routes_conversations_module, "_dispatch_ask_ai_synthesis_call", _dispatch)

    if security_blocked:
        monkeypatch.setattr(
            routes_conversations_module, "_coerce_and_validate_ai_result",
            lambda result, question, mode, answer_language: ({"answer": "Blocked."}, "security_blocked_input"),
        )
    else:
        monkeypatch.setattr(
            routes_conversations_module, "_coerce_and_validate_ai_result",
            lambda result, question, mode, answer_language: (result, ""),
        )
        monkeypatch.setattr(
            routes_conversations_module, "_extract_raw_ai_answer",
            lambda result, answer_language: (None, "raw answer"),
        )
        monkeypatch.setattr(
            routes_conversations_module, "_resolve_ask_web_warning_flag",
            lambda result, ctx: False,
        )
        monkeypatch.setattr(
            routes_conversations_module, "_compose_validated_ask_answer",
            lambda raw, needs_web_warning: answer_text,
        )
        monkeypatch.setattr(
            routes_conversations_module, "_store_user_memory_summary",
            lambda *a, **k: None,
        )

    return seen_dispatch_kwargs


class TestAskInConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.post("/api/conversations/conv-1/ask", json={"question": "hi"})
        assert response.status_code == 401

    def test_blank_question_is_400(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "   "}, headers=AUTH_HEADERS
        )
        assert response.status_code == 400

    def test_missing_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/does-not-exist/ask", json={"question": "hi"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 404

    def test_ownership_fetch_filters_by_id_and_the_authenticated_user(
        self, test_client, authed, monkeypatch
    ):
        """Same isolation guarantee as every other per-conversation route
        (see TestGetConversation.test_cannot_fetch_another_users_conversation):
        the ownership fetch filters on id AND the caller's own verified
        user_id, never a request-supplied one, so a guessed/foreign id 404s
        instead of ever touching someone else's thread."""
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/someone-elses-conv/ask",
            json={"question": "hi", "user_id": OTHER_USER_ID},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 404
        query = client.table(CONV_TABLE)
        assert ("id", "someone-elses-conv") in query.eq_calls
        assert ("user_id", FAKE_USER_ID) in query.eq_calls

    def test_happy_path_stores_user_and_assistant_messages(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": "Ashkenaz"}]),
            MSG_TABLE: _FakeQuery(
                data=[{"role": "user", "content": "Earlier turn", "status": "complete"}],
                insert_data=[
                    {"id": "msg-user-1", "role": "user", "content": "New question", "status": "complete"},
                    {"id": "msg-assistant-1", "role": "assistant", "content": "The answer.", "status": "complete"},
                ],
            ),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch, answer_text="The answer.")

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "New question"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["user_message"]["id"] == "msg-user-1"
        assert body["user_message"]["citations"] == []
        assert body["assistant_message"]["id"] == "msg-assistant-1"
        assert body["assistant_message"]["content"] == "The answer."
        assert body["assistant_message"]["citations"] == []

        # minhag is read from the conversation row, not the request body.
        assert seen["canonical_lens"] == "Ashkenaz"
        assert seen["conversation_history"] == [{"role": "user", "content": "Earlier turn"}]

        assert client.table(CONV_TABLE).update_calls, "conversation.updated_at must be bumped"

    def test_minhag_lock_ignores_community_in_request_body(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": "Sefardic"}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask",
            json={"question": "q", "community": "Ashkenaz", "minhag": "Ashkenaz"},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        assert seen["canonical_lens"] == "Sefardic"

    def test_no_minhag_defaults_to_all(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert seen["canonical_lens"] == "All"

    def test_history_query_filters_to_this_conversations_complete_turns(
        self, test_client, authed, monkeypatch
    ):
        """`status="complete"` filtering happens server-side (Postgres),
        which the fake query builder doesn't simulate -- so this asserts on
        the query construction itself, the same way TestGetConversation's
        cross-user isolation test asserts on eq_calls rather than on
        (unenforced) fake-side filtering."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        eq_calls = client.table(MSG_TABLE).eq_calls
        assert ("conversation_id", "conv-1") in eq_calls
        assert ("status", "complete") in eq_calls

    def test_conversation_history_drops_rows_with_an_invalid_role(self, test_client, authed, monkeypatch):
        """Unlike the status filter above, the role check in
        _fetch_conversation_history() runs in Python, not Postgres -- so
        it's directly exercisable through the fake."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(
                data=[
                    {"role": "system", "content": "not a real turn", "status": "complete"},
                    {"role": "user", "content": "Earlier turn", "status": "complete"},
                ],
                insert_data=[
                    {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                    {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
                ],
            ),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert seen["conversation_history"] == [{"role": "user", "content": "Earlier turn"}]

    def test_security_blocked_result_still_returns_201(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "Blocked.", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, security_blocked=True)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert response.get_json()["assistant_message"]["content"] == "Blocked."

    def test_synthesis_exception_stores_error_placeholder_message(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-error-1", "role": "assistant", "content": "", "status": "error"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, raise_exc=RuntimeError("AI request failed"))

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["user_message"]["id"] == "msg-user-1"
        assert body["assistant_message"]["status"] == "error"

    def test_synthesis_exception_and_placeholder_insert_failure_returns_client_only_placeholder(
        self, test_client, authed, monkeypatch
    ):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(
                insert_data=[{"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"}],
                error=None,
            ),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, raise_exc=RuntimeError("AI request failed"))

        # After the user-message insert consumes the one queued row, the
        # placeholder's own insert() call finds the queue exhausted and gets
        # back an empty result -- exercising the "even the placeholder
        # insert produced no rows" branch without needing a second error.
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["assistant_message"] == {
            "role": "assistant", "content": "", "status": "error", "citations": [],
        }

    def test_user_message_insert_returning_no_rows_is_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 500

    def test_invalid_language_falls_back_to_english(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask",
            json={"question": "q", "language": "fr"},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        assert seen["answer_language"] == "en"

    def test_assistant_message_insert_returning_no_rows_falls_back_to_error_placeholder(
        self, test_client, authed, monkeypatch
    ):
        """_synthesize_and_store_assistant_reply()'s own insert can also
        come back empty (e.g. an RLS policy silently drops the row) --
        that's treated the same as a synthesis exception: caught, and a
        status=error placeholder is stored instead."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                # Second (assistant) insert() call gets no row back; third
                # (the error placeholder's own insert()) gets one.
                None,
                {"id": "msg-error-1", "role": "assistant", "content": "", "status": "error"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert response.get_json()["assistant_message"]["id"] == "msg-error-1"

    def test_error_placeholder_insert_itself_raising_is_swallowed(self, test_client, authed, monkeypatch):
        """If even the status=error placeholder's own insert() raises, that
        exception is caught too -- the route still responds 201 with a
        client-only (unpersisted) placeholder rather than 500ing after the
        user's own message was already saved."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(
                insert_data=[{"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"}],
            ),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, raise_exc=RuntimeError("AI request failed"))

        real_insert = client.table(MSG_TABLE).insert
        call_count = {"n": 0}

        def _insert_second_call_raises(*a, **k):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise RuntimeError("insert failed")
            return real_insert(*a, **k)

        monkeypatch.setattr(client.table(MSG_TABLE), "insert", _insert_second_call_raises)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert response.get_json()["assistant_message"] == {
            "role": "assistant", "content": "", "status": "error", "citations": [],
        }

    def test_setup_exception_before_ai_dispatch_is_500(self, test_client, authed, monkeypatch):
        boom = RuntimeError("db down")
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(error=boom)})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 500

    def test_citations_are_stored_from_primary_sources(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
            CIT_TABLE: _FakeQuery(data=[{"id": "c1", "source_ref": "OC 1:1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, primary_sources=[
            {"ref": "OC 1:1", "title": "Orach Chaim 1:1", "lines": [{"en": "Some text.", "he": "טקסט"}], "url": "https://example.com"},
        ])

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert response.get_json()["assistant_message"]["citations"] == [{"id": "c1", "source_ref": "OC 1:1"}]
        cit_insert_rows = client.table(CIT_TABLE).insert_calls[0][0][0]
        assert cit_insert_rows[0]["source_ref"] == "OC 1:1"
        assert cit_insert_rows[0]["excerpt_en"] == "Some text."
        assert cit_insert_rows[0]["excerpt_he"] == "טקסט"
        assert cit_insert_rows[0]["url"] == "https://example.com"

    def test_non_dict_primary_source_is_skipped_when_building_citations(
        self, test_client, authed, monkeypatch
    ):
        """_compact_ai_sources() already drops non-dict entries before
        _display_sources_to_citations() sees them in the real /ask flow --
        this exercises that function's own defensive re-check directly, by
        handing ctx["primary_sources"] a stray non-dict entry alongside a
        valid one."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "minhag": None}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
            CIT_TABLE: _FakeQuery(data=[{"id": "c1", "source_ref": "OC 1:1"}]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        monkeypatch.setattr(
            routes_conversations_module, "_compact_ai_sources",
            lambda sources: ["not-a-dict", {"ref": "OC 1:1", "title": "OC 1:1", "lines": []}],
        )
        _patch_ai_pipeline(monkeypatch, primary_sources=["irrelevant -- _compact_ai_sources is mocked above"])

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        cit_insert_rows = client.table(CIT_TABLE).insert_calls[0][0][0]
        assert len(cit_insert_rows) == 1
        assert cit_insert_rows[0]["source_ref"] == "OC 1:1"


class TestAskAutoTitle:
    """create_conversation() stores title="" and nothing else ever filled it,
    so every thread showed up untitled in the sidebar. ask_in_conversation()
    now derives a default title from the question -- only while the title is
    still empty and never user-set (title_is_custom), so a rename is never
    overwritten and later turns never retitle the thread."""

    def _ask(self, test_client, monkeypatch, conversation_row, question="What bracha on a new fruit?"):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[conversation_row]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": question, "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": question}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        # The first update() is the post-user-message bump (the second is the
        # post-assistant-message bump in _synthesize_and_store_assistant_reply).
        return client.table(CONV_TABLE).update_calls[0][0][0]

    def test_first_question_sets_the_title(self, test_client, authed, monkeypatch):
        updates = self._ask(test_client, monkeypatch, {"id": "conv-1", "title": "", "title_is_custom": False})
        assert updates["title"] == "What bracha on a new fruit?"
        assert "title_is_custom" not in updates
        assert "updated_at" in updates

    def test_response_carries_the_updated_conversation_header(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1", "title": "", "title_is_custom": False, "minhag": "Sefardic"}]),
            MSG_TABLE: _FakeQuery(insert_data=[
                {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
                {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
            ]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        body = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "Dishwasher for meat?"}, headers=AUTH_HEADERS
        ).get_json()
        assert body["conversation"]["id"] == "conv-1"
        assert body["conversation"]["title"] == "Dishwasher for meat?"
        assert body["conversation"]["minhag"] == "Sefardic"
        assert body["conversation"]["title_is_custom"] is False

    def test_existing_title_is_not_overwritten(self, test_client, authed, monkeypatch):
        updates = self._ask(test_client, monkeypatch, {"id": "conv-1", "title": "Brachot", "title_is_custom": False})
        assert "title" not in updates

    def test_custom_title_is_never_overwritten_even_if_blank(self, test_client, authed, monkeypatch):
        """A user who deliberately renamed the thread to "" keeps it blank."""
        updates = self._ask(test_client, monkeypatch, {"id": "conv-1", "title": "", "title_is_custom": True})
        assert "title" not in updates


class TestDeriveTitle:
    def test_short_question_is_kept_whole_with_whitespace_collapsed(self):
        assert routes_conversations_module._derive_title("  Can I\n use  one dishwasher? ") == \
            "Can I use one dishwasher?"

    def test_long_question_is_cut_at_a_word_boundary_with_ellipsis(self):
        question = "Can I use one dishwasher for both meat and dairy dishes if I run them in separate hot loads every day?"
        title = routes_conversations_module._derive_title(question, max_len=40)
        assert title.endswith("…")
        assert len(title) <= 41
        assert title == "Can I use one dishwasher for both meat…"

    def test_long_single_word_is_hard_cut(self):
        title = routes_conversations_module._derive_title("א" * 100, max_len=10)
        assert title == "א" * 10 + "…"


class TestGetConversationHidesSupersededTurns:
    def test_messages_query_excludes_superseded_rows(self, test_client, authed, monkeypatch):
        """A retried error row points at its replacement via superseded_by;
        filtering happens in Postgres, so assert the query construction."""
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[{"id": "conv-1"}]),
            MSG_TABLE: _FakeQuery(data=[]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.get("/api/conversations/conv-1", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert ("superseded_by", "null") in client.table(MSG_TABLE).is_calls


class TestRestoreConversation:
    def test_without_auth_is_401(self, test_client):
        response = test_client.post("/api/conversations/conv-1/restore")
        assert response.status_code == 401

    def test_missing_or_foreign_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations/someone-elses-conv/restore", headers=AUTH_HEADERS)
        assert response.status_code == 404
        assert response.get_json() == {"error": "Not found"}
        query = client.table(CONV_TABLE)
        assert ("id", "someone-elses-conv") in query.eq_calls
        assert ("user_id", FAKE_USER_ID) in query.eq_calls
        assert query.update_calls == []

    def test_lookup_includes_soft_deleted_rows(self, test_client, authed, monkeypatch):
        """Unlike _fetch_own_conversation(), the lookup must NOT filter
        deleted_at IS NULL -- a soft-deleted row is exactly what it's for."""
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        test_client.post("/api/conversations/conv-1/restore", headers=AUTH_HEADERS)
        assert ("deleted_at", "null") not in client.table(CONV_TABLE).is_calls

    def test_soft_deleted_conversation_is_restored(self, test_client, authed, monkeypatch):
        deleted_row = {"id": "conv-1", "title": "Brachot", "deleted_at": "2026-09-23T10:00:00Z"}
        restored_row = {"id": "conv-1", "title": "Brachot", "deleted_at": None, "user_id": FAKE_USER_ID}
        client = _FakeSupabaseClient({
            CONV_TABLE: _FakeQuery(data=[deleted_row], update_data=[restored_row]),
        })
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations/conv-1/restore", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == restored_row
        query = client.table(CONV_TABLE)
        assert query.update_calls[0][0][0] == {"deleted_at": None}
        assert query.eq_calls.count(("user_id", FAKE_USER_ID)) == 2

    def test_empty_update_result_falls_back_to_merged_row(self, test_client, authed, monkeypatch):
        deleted_row = {"id": "conv-1", "title": "Brachot", "deleted_at": "2026-09-23T10:00:00Z"}
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[deleted_row], update_data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations/conv-1/restore", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"id": "conv-1", "title": "Brachot", "deleted_at": None}

    def test_not_deleted_conversation_is_returned_unchanged(self, test_client, authed, monkeypatch):
        live_row = {"id": "conv-1", "title": "Brachot", "deleted_at": None}
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[live_row])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post("/api/conversations/conv-1/restore", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == live_row
        assert client.table(CONV_TABLE).update_calls == []

    def test_restore_is_rate_limited_as_cheap_not_llm(self):
        """backend/rate_limit.py's `^/api/conversations/[^/]+/ask/?$` pattern
        must not swallow /restore into the metered llm class."""
        assert classify_route("/api/conversations/x/restore") == "cheap"
        assert classify_route("/api/conversations/x/ask") == "llm"


def _ask_client(conversation_row=None, **msg_query_kwargs):
    msg_query_kwargs.setdefault("insert_data", [
        {"id": "msg-user-1", "role": "user", "content": "q", "status": "complete"},
        {"id": "msg-assistant-1", "role": "assistant", "content": "a", "status": "complete"},
    ])
    return _FakeSupabaseClient({
        CONV_TABLE: _FakeQuery(data=[conversation_row or {"id": "conv-1", "title": "t", "minhag": None}]),
        MSG_TABLE: _FakeQuery(**msg_query_kwargs),
    })


class TestAskCostGates:
    def test_tripped_global_breaker_is_503_and_writes_nothing(
        self, test_client, authed, monkeypatch, cost_gates
    ):
        cost_gates.tripped = True
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 503
        assert response.get_json() == {
            "error": "AI answers are paused for today. Please try again after midnight UTC.",
            "code": "ai_paused",
        }
        assert 1 <= int(response.headers["Retry-After"]) <= 86400
        assert client.table(MSG_TABLE).insert_calls == []
        assert client.table(CONV_TABLE).update_calls == []
        # Breaker first: no per-user reservation is taken for a refused ask.
        assert cost_gates.budget_calls == []

    def test_exhausted_user_budget_is_402_and_writes_nothing(
        self, test_client, authed, monkeypatch, cost_gates
    ):
        cost_gates.allowed = False
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        response = test_client.post(
            "/api/conversations/conv-1/ask",
            json={"question": "q"},
            headers={**AUTH_HEADERS, "X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )

        assert response.status_code == 402
        assert response.get_json() == {
            "error": (
                "Daily AI usage limit reached for this account ($1.99 of $2.00). "
                "Please try again after midnight UTC."
            ),
            "code": "daily_budget_exhausted",
        }
        assert cost_gates.budget_calls == [(FAKE_USER_ID, "203.0.113.9")]
        assert client.table(MSG_TABLE).insert_calls == []
        assert client.table(CONV_TABLE).update_calls == []

    def test_missing_client_ip_is_passed_as_empty_string(self, test_client, authed, monkeypatch, cost_gates):
        monkeypatch.setattr(routes_conversations_module, "_extract_client_ip", lambda: None)
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        assert cost_gates.budget_calls == [(FAKE_USER_ID, "")]

    def test_allowed_ask_attributes_spend_to_the_user_then_clears_it(
        self, test_client, authed, monkeypatch, cost_gates
    ):
        """record_llm_call() reads user_id + the budget reservation from
        contextvars; the route must carry both (the reservation is bound on
        the bridge thread, so it has to be handed back explicitly) into the
        synthesis call, and unbind them once the request ends."""
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        seen = {}

        def _dispatch(question, mode, canonical_lens, answer_language, ctx, engine, conversation_history=None):
            seen["user_id"] = logging_setup_module.get_user_id()
            seen["client_key"] = logging_setup_module.get_client_key()
            seen["reservation"] = logging_setup_module.get_budget_reservation()
            return {"raw": True}

        monkeypatch.setattr(routes_conversations_module, "_dispatch_ask_ai_synthesis_call", _dispatch)
        logging_setup_module.bind_budget_reservation("stale-from-an-earlier-request")

        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )

        assert response.status_code == 201
        assert seen == {"user_id": FAKE_USER_ID, "client_key": "", "reservation": "res-123"}
        assert logging_setup_module.get_user_id() == ""
        assert logging_setup_module.get_budget_reservation() == ""

    def test_gate_failure_is_500_and_writes_nothing(self, test_client, authed, monkeypatch, cost_gates):
        cost_gates.breaker_error = RuntimeError("bridge exploded")
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 500
        assert client.table(MSG_TABLE).insert_calls == []

    def test_gates_run_after_ownership_lookup(self, test_client, authed, monkeypatch, cost_gates):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q"}, headers=AUTH_HEADERS
        )
        assert response.status_code == 404
        assert cost_gates.breaker_calls == 0


class TestSecondsUntilNextUtcMidnight:
    @pytest.mark.parametrize("now, expected", [
        (datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc), 43200),
        (datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc), 86400),
        (datetime(2026, 9, 23, 23, 59, 59, 500000, tzinfo=timezone.utc), 1),
        (datetime(2026, 12, 31, 23, 0, 0, tzinfo=timezone.utc), 3600),
    ])
    def test_values(self, now, expected):
        assert routes_conversations_module._seconds_until_next_utc_midnight(now) == expected

    def test_defaults_to_now(self):
        assert 1 <= routes_conversations_module._seconds_until_next_utc_midnight() <= 86400


class TestFetchConversationHistory:
    def test_most_recent_turns_are_kept_and_returned_oldest_first(self):
        # The fake returns rows as given; the real query is newest-first.
        rows_newest_first = [
            {"role": "assistant", "content": "a3", "status": "complete"},
            {"role": "user", "content": "q3", "status": "complete"},
            {"role": "assistant", "content": "a2", "status": "complete"},
            {"role": "user", "content": "q2", "status": "complete"},
        ]
        client = _FakeSupabaseClient({MSG_TABLE: _FakeQuery(data=rows_newest_first)})
        history = routes_conversations_module._fetch_conversation_history(client, "conv-1", limit=2)
        assert history == [
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]
        query = client.table(MSG_TABLE)
        assert (("created_at",), {"desc": True}) in query.order_calls
        assert ("superseded_by", "null") in query.is_calls
        assert query.lt_calls == []

    def test_before_bounds_the_window(self):
        client = _FakeSupabaseClient({MSG_TABLE: _FakeQuery(data=[])})
        routes_conversations_module._fetch_conversation_history(
            client, "conv-1", before="2026-09-23T10:00:00Z")
        assert client.table(MSG_TABLE).lt_calls == [("created_at", "2026-09-23T10:00:00Z")]


RETRY_OF = "0b9f2c1e-6a53-4f0e-9d7c-3c2f1a8b7e10"
NEW_ASSISTANT_ID = "5d1c7e22-8b4a-4c39-a0f1-9e6d2b3c4a55"
QUESTION_ROW = {
    "id": "msg-user-1", "conversation_id": "conv-1", "role": "user",
    "content": "Original question?", "status": "complete", "superseded_by": None,
    "created_at": "2026-09-23T10:00:00Z",
}
ERROR_ROW = {
    "id": RETRY_OF, "role": "assistant", "status": "error", "superseded_by": None,
    "created_at": "2026-09-23T10:00:05Z",
}


def _retry_client(error_row=ERROR_ROW, question_rows=None, history_rows=None, insert_data=None,
                  conversation_row=None):
    select_data = [[error_row] if error_row else []]
    if error_row:
        select_data.append([QUESTION_ROW] if question_rows is None else question_rows)
        select_data.append(history_rows or [])
    return _FakeSupabaseClient({
        CONV_TABLE: _FakeQuery(data=[conversation_row or {"id": "conv-1", "title": "t", "minhag": None}]),
        MSG_TABLE: _FakeQuery(
            select_data=select_data,
            insert_data=insert_data if insert_data is not None else [
                {"id": NEW_ASSISTANT_ID, "role": "assistant", "content": "a", "status": "complete"},
            ],
        ),
    })


class TestAskRetry:
    def test_retry_reanswers_without_a_new_user_turn_and_supersedes_the_error(
        self, test_client, authed, monkeypatch
    ):
        history_newest_first = [
            {"role": "assistant", "content": "Earlier answer", "status": "complete"},
            {"role": "user", "content": "Earlier question", "status": "complete"},
        ]
        client = _retry_client(history_rows=history_newest_first)
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        seen = _patch_ai_pipeline(monkeypatch, answer_text="a")

        response = test_client.post(
            "/api/conversations/conv-1/ask",
            json={"retry_of": RETRY_OF, "question": "ignored body question"},
            headers=AUTH_HEADERS,
        )

        assert response.status_code == 201
        body = response.get_json()
        assert body["user_message"] == {**QUESTION_ROW, "citations": []}
        assert body["assistant_message"]["id"] == NEW_ASSISTANT_ID
        assert body["superseded_message_id"] == RETRY_OF
        assert body["conversation"]["id"] == "conv-1"

        # The question comes from the stored turn, never the body.
        assert seen["question"] == "Original question?"
        # History is what the original ask saw: strictly before the question.
        assert seen["conversation_history"] == [
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
        ]

        msg_query = client.table(MSG_TABLE)
        assert [call[0][0]["role"] for call in msg_query.insert_calls] == ["assistant"]
        assert ("id", RETRY_OF) in msg_query.eq_calls
        assert ("conversation_id", "conv-1") in msg_query.eq_calls
        assert ("role", "user") in msg_query.eq_calls
        assert ("created_at", ERROR_ROW["created_at"]) in msg_query.lt_calls
        assert ("created_at", QUESTION_ROW["created_at"]) in msg_query.lt_calls
        assert msg_query.update_calls[0][0][0] == {"superseded_by": NEW_ASSISTANT_ID}
        assert ("superseded_by", "null") in msg_query.is_calls
        assert client.table(CONV_TABLE).update_calls, "updated_at must still bump"

    def test_retry_of_is_canonicalized(self, test_client, authed, monkeypatch):
        client = _retry_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask",
            json={"retry_of": f"  {RETRY_OF.upper()} "},
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        assert response.get_json()["superseded_message_id"] == RETRY_OF

    def test_retry_derives_title_when_thread_is_still_untitled(self, test_client, authed, monkeypatch):
        client = _retry_client(conversation_row={"id": "conv-1", "title": "", "title_is_custom": False})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        assert client.table(CONV_TABLE).update_calls[0][0][0]["title"] == "Original question?"

    @pytest.mark.parametrize("retry_of", ["not-a-uuid", "", 123, True, {"id": RETRY_OF}])
    def test_malformed_retry_of_is_400_before_any_db_access(self, test_client, authed, monkeypatch, retry_of):
        client = _retry_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": retry_of}, headers=AUTH_HEADERS
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_retry"
        assert client.table(CONV_TABLE).eq_calls == []

    @pytest.mark.parametrize("error_row, question_rows", [
        (None, None),  # not found in this conversation
        ({**ERROR_ROW, "role": "user"}, None),
        ({**ERROR_ROW, "status": "complete"}, None),
        ({**ERROR_ROW, "superseded_by": NEW_ASSISTANT_ID}, None),  # already retried
        (ERROR_ROW, []),  # no preceding question
        (ERROR_ROW, [{**QUESTION_ROW, "content": "   "}]),  # blank question
    ])
    def test_invalid_retry_target_is_400_and_writes_nothing(
        self, test_client, authed, monkeypatch, cost_gates, error_row, question_rows
    ):
        client = _retry_client(error_row=error_row, question_rows=question_rows)
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body["code"] == "invalid_retry"
        assert body["error"]
        msg_query = client.table(MSG_TABLE)
        assert msg_query.insert_calls == []
        assert msg_query.update_calls == []
        assert cost_gates.breaker_calls == 0

    def test_retry_in_a_foreign_conversation_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient({CONV_TABLE: _FakeQuery(data=[])})
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 404
        assert MSG_TABLE not in client._queries

    def test_retry_is_cost_gated_and_refusal_supersedes_nothing(
        self, test_client, authed, monkeypatch, cost_gates
    ):
        cost_gates.allowed = False
        client = _retry_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 402
        msg_query = client.table(MSG_TABLE)
        assert msg_query.insert_calls == []
        assert msg_query.update_calls == []

    def test_failed_retry_still_supersedes_so_one_error_row_stays_visible(
        self, test_client, authed, monkeypatch
    ):
        client = _retry_client(insert_data=[
            {"id": NEW_ASSISTANT_ID, "role": "assistant", "content": "", "status": "error"},
        ])
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, raise_exc=RuntimeError("AI request failed"))
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["assistant_message"]["status"] == "error"
        assert body["superseded_message_id"] == RETRY_OF
        assert client.table(MSG_TABLE).update_calls[0][0][0] == {"superseded_by": NEW_ASSISTANT_ID}

    def test_unpersisted_placeholder_supersedes_nothing(self, test_client, authed, monkeypatch):
        client = _retry_client(insert_data=[])
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch, raise_exc=RuntimeError("AI request failed"))
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        body = response.get_json()
        assert "id" not in body["assistant_message"]
        assert body["superseded_message_id"] is None
        assert client.table(MSG_TABLE).update_calls == []

    def test_supersede_update_failure_still_returns_201(self, test_client, authed, monkeypatch):
        client = _retry_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)

        def _update_raises(*a, **k):
            raise RuntimeError("update failed")

        monkeypatch.setattr(client.table(MSG_TABLE), "update", _update_raises)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"retry_of": RETRY_OF}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        body = response.get_json()
        assert body["assistant_message"]["id"] == NEW_ASSISTANT_ID
        assert body["superseded_message_id"] is None

    def test_fresh_ask_with_null_retry_of_is_not_a_retry(self, test_client, authed, monkeypatch):
        client = _ask_client()
        monkeypatch.setattr(routes_conversations_module, "_get_user_scoped_supabase_client", lambda: client)
        _patch_ai_pipeline(monkeypatch)
        response = test_client.post(
            "/api/conversations/conv-1/ask", json={"question": "q", "retry_of": None}, headers=AUTH_HEADERS
        )
        assert response.status_code == 201
        assert "superseded_message_id" not in response.get_json()
        assert len(client.table(MSG_TABLE).insert_calls) == 2
