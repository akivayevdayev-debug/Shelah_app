"""
Tests for backend/routes_privacy.py routes (plan.md §8.D privacy operations).

Covers:
  - GET  /api/user/data-export      without auth → 401; with auth → returns
    exactly this user's own rows across every user-scoped table (each query
    filtered by user_id), Supabase-not-configured → 503, one table's
    exception surfaces as a partial_errors entry without failing the rest
  - POST /api/user/delete-account   without auth → 401; missing/wrong
    confirmation phrase → 400; happy path deletes every table + the Clerk
    identity and is idempotent; a table failure still attempts every other
    table and reports 207; Clerk failure/not-configured is reported but
    doesn't block the Supabase-side deletion from succeeding
  - _delete_clerk_user              404 (already gone) treated as success;
    non-2xx/404 treated as failure; missing CLERK_SECRET_KEY short-circuits
  - GET  /api/devtools/retention-enforce  CRON_SECRET-gated like
    budget_check(); deletes only rows older than each table's retention
    window (asserted via the `.lt("created_at", cutoff)` filter actually
    issued, mirroring how the rest of this suite verifies Supabase query
    construction without a real Postgres instance)
"""

from __future__ import annotations

import httpx
import pytest

import backend.auth as auth_module
import backend.routes_privacy as routes_privacy_module

FAKE_USER_ID = "user_test_fake_privacy"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}
CRON_HEADERS = {"Authorization": "Bearer top-secret-cron-value"}


@pytest.fixture
def authed(monkeypatch):
    monkeypatch.setattr(
        auth_module, "_verify_clerk_token",
        lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
    )
    return FAKE_USER_ID


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable fake query builder that records every filter method call
    (so tests can assert *which* column/value a route filtered on) and
    returns a preset result (or raises a preset error) from `.execute()`."""

    def __init__(self, data=None, error=None):
        self._data = data if data is not None else []
        self._error = error
        self.calls = []

    def select(self, *a, **k):
        self.calls.append(("select", a, k))
        return self

    def delete(self, *a, **k):
        self.calls.append(("delete", a, k))
        return self

    def eq(self, *a, **k):
        self.calls.append(("eq", a, k))
        return self

    def lt(self, *a, **k):
        self.calls.append(("lt", a, k))
        return self

    def range(self, *a, **k):
        self.calls.append(("range", a, k))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return _FakeResult(self._data)


class _FakeSupabaseClient:
    """Fake Supabase client that hands out a fresh, independently-tracked
    `_FakeQuery` per `.table(name)` call, keyed by table name -- unlike
    test_routes_user.py's single-shared-query fake, this lets a test seed
    different data/errors per table (needed to prove export/delete only
    touch the tables they claim to, and that one table's failure doesn't
    sink the others)."""

    def __init__(self, table_data=None, table_errors=None, table_data_pages=None):
        self._table_data = table_data or {}
        self._table_errors = table_errors or {}
        # table_name -> list[list[row]], one entry consumed per successive
        # `.table(name)` call -- lets a test simulate export_user_data()'s
        # per-page `.range()` loop, which calls `.table()` fresh each page
        # (mirroring backend/cost_meter.py's pagination pattern).
        self._table_data_pages = {k: list(v) for k, v in (table_data_pages or {}).items()}
        self.queries = {}

    def table(self, name):
        if name in self._table_data_pages and self._table_data_pages[name]:
            data = self._table_data_pages[name].pop(0)
        else:
            data = self._table_data.get(name, [])
        query = _FakeQuery(
            data=data,
            error=self._table_errors.get(name),
        )
        self.queries.setdefault(name, []).append(query)
        return query


ALL_TABLES = [t for _key, t in routes_privacy_module._USER_DATA_TABLES]


class TestDataExport:
    def test_without_auth_is_401(self, test_client):
        response = test_client.get("/api/user/data-export")
        assert response.status_code == 401

    def test_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.get("/api/user/data-export", headers=AUTH_HEADERS)
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: None)
        response = test_client.get("/api/user/data-export", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_happy_path_returns_only_this_users_rows_per_table(
        self, test_client, authed, monkeypatch
    ):
        seeded = {
            routes_privacy_module.SUPABASE_PREFS_TABLE: [{"user_id": FAKE_USER_ID, "prefs": {"theme": "dark"}}],
            routes_privacy_module.SUPABASE_STUDY_BOOKMARKS_TABLE: [{"id": "b1", "ref": "Genesis 1:1"}],
            routes_privacy_module.SUPABASE_ASK_HISTORY_TABLE: [{"id": "h1", "question": "Is X permitted?"}],
            routes_privacy_module.SUPABASE_USER_MEMORIES_TABLE: [{"id": "m1", "summary": "..."}],
            routes_privacy_module._AI_USAGE_LOG_TABLE: [{"id": "u1", "model": "claude-sonnet-4-6"}],
        }
        client = _FakeSupabaseClient(table_data=seeded)
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        response = test_client.get("/api/user/data-export", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["user_id"] == FAKE_USER_ID
        assert "exported_at" in body
        assert "partial_errors" not in body
        assert body["data"]["preferences"] == seeded[routes_privacy_module.SUPABASE_PREFS_TABLE]
        assert body["data"]["bookmarks"] == seeded[routes_privacy_module.SUPABASE_STUDY_BOOKMARKS_TABLE]
        assert body["data"]["ask_history"] == seeded[routes_privacy_module.SUPABASE_ASK_HISTORY_TABLE]
        assert body["data"]["memories"] == seeded[routes_privacy_module.SUPABASE_USER_MEMORIES_TABLE]
        assert body["data"]["ai_usage_log"] == seeded[routes_privacy_module._AI_USAGE_LOG_TABLE]

        # RLS-equivalent guarantee at the query-construction level: every
        # table query must have been filtered to this caller's user_id.
        for table_name in ALL_TABLES:
            query = client.queries[table_name][0]
            assert ("eq", ("user_id", FAKE_USER_ID), {}) in query.calls

    def test_one_table_exception_reports_partial_error_without_failing_others(
        self, test_client, authed, monkeypatch
    ):
        prefs_table = routes_privacy_module.SUPABASE_PREFS_TABLE
        bookmarks_table = routes_privacy_module.SUPABASE_STUDY_BOOKMARKS_TABLE
        client = _FakeSupabaseClient(
            table_data={bookmarks_table: [{"id": "b1"}]},
            table_errors={prefs_table: RuntimeError("table missing")},
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        response = test_client.get("/api/user/data-export", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["data"]["preferences"] == []
        assert body["data"]["bookmarks"] == [{"id": "b1"}]
        assert "preferences" in body["partial_errors"]
        # Regression: the raw exception text must not be echoed back to the
        # client -- only a generic message (full detail goes to
        # _capture_backend_error server-side instead).
        assert "table missing" not in body["partial_errors"]["preferences"]

    def test_high_volume_table_is_paginated_not_truncated(
        self, test_client, authed, monkeypatch
    ):
        """Regression test for the finding that an unpaginated `.select("*")`
        would silently truncate an export at PostgREST's default per-request
        row cap. Seeds a table with more rows than one page and asserts every
        row comes back, fetched across multiple `.range()` pages."""
        ask_history_table = routes_privacy_module.SUPABASE_ASK_HISTORY_TABLE
        page_size = routes_privacy_module._EXPORT_PAGE_SIZE
        full_page = [{"id": f"h{i}"} for i in range(page_size)]
        last_page = [{"id": "h_last"}]
        client = _FakeSupabaseClient(
            table_data_pages={ask_history_table: [full_page, last_page]},
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        response = test_client.get("/api/user/data-export", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert len(body["data"]["ask_history"]) == page_size + 1
        assert body["data"]["ask_history"][-1] == {"id": "h_last"}
        range_calls = [c for c in client.queries[ask_history_table][0].calls if c[0] == "range"]
        assert range_calls == [("range", (0, page_size - 1), {})]
        range_calls_page2 = [c for c in client.queries[ask_history_table][1].calls if c[0] == "range"]
        assert range_calls_page2 == [("range", (page_size, 2 * page_size - 1), {})]


class TestDeleteAccount:
    def test_without_auth_is_401(self, test_client):
        response = test_client.post("/api/user/delete-account")
        assert response.status_code == 401

    def test_missing_confirmation_is_400(self, test_client, authed):
        response = test_client.post("/api/user/delete-account", headers=AUTH_HEADERS)
        assert response.status_code == 400

    def test_wrong_confirmation_is_400(self, test_client, authed):
        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "delete"},
        )
        assert response.status_code == 400

    def test_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: None)
        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 503

    def test_happy_path_deletes_every_table_and_clerk_identity(
        self, test_client, authed, monkeypatch
    ):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(
            routes_privacy_module, "_delete_clerk_user", lambda user_id: (True, None)
        )

        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["clerk_deleted"] is True
        assert all(body["deleted_tables"].values())

        for table_name in ALL_TABLES:
            query = client.queries[table_name][0]
            assert ("delete", (), {}) in query.calls
            assert ("eq", ("user_id", FAKE_USER_ID), {}) in query.calls

    def test_is_idempotent_across_repeated_calls(self, test_client, authed, monkeypatch):
        """Deleting an already-empty set of rows must not error -- a user
        retrying the request (e.g. after a network blip) gets the same
        success response both times."""
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(
            routes_privacy_module, "_delete_clerk_user", lambda user_id: (True, None)
        )

        first = test_client.post(
            "/api/user/delete-account", headers=AUTH_HEADERS, json={"confirmation": "DELETE"},
        )
        second = test_client.post(
            "/api/user/delete-account", headers=AUTH_HEADERS, json={"confirmation": "DELETE"},
        )
        assert first.status_code == second.status_code == 200
        assert first.get_json()["ok"] is second.get_json()["ok"] is True

    def test_clerk_not_configured_still_deletes_data_but_reports_incomplete(
        self, test_client, authed, monkeypatch
    ):
        """Supabase rows are gone, but since the Clerk identity is not (the
        overall DSR isn't complete until both are), the response must not
        claim full success -- 207/ok:False, not 200/ok:True."""
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)

        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 207
        body = response.get_json()
        assert body["ok"] is False
        assert all(body["deleted_tables"].values())
        assert body["clerk_deleted"] is False
        assert "CLERK_SECRET_KEY" in body["clerk_error"]

    def test_clerk_delete_failure_still_deletes_data_but_reports_incomplete(
        self, test_client, authed, monkeypatch
    ):
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(
            routes_privacy_module, "_delete_clerk_user",
            lambda user_id: (False, "Clerk delete-user returned 500: boom"),
        )

        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 207
        body = response.get_json()
        assert body["ok"] is False
        assert all(body["deleted_tables"].values())
        assert body["clerk_deleted"] is False
        assert "clerk_error" in body

    def test_one_table_failure_skips_clerk_deletion_entirely(
        self, test_client, authed, monkeypatch
    ):
        """Regression test for the finding that _delete_clerk_user() was
        called unconditionally even when a Supabase table delete failed,
        permanently orphaning that user's data since the Clerk identity --
        their only way to sign in and retry -- would already be gone.
        _delete_clerk_user must not be invoked at all in this case."""
        prefs_table = routes_privacy_module.SUPABASE_PREFS_TABLE
        client = _FakeSupabaseClient(
            table_errors={prefs_table: RuntimeError("db down")},
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        clerk_delete_calls = []

        def _tracked_delete_clerk_user(user_id):
            clerk_delete_calls.append(user_id)
            return True, None

        monkeypatch.setattr(
            routes_privacy_module, "_delete_clerk_user", _tracked_delete_clerk_user
        )

        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 207
        body = response.get_json()
        assert body["ok"] is False
        assert body["deleted_tables"][prefs_table] is False
        # every other table was still attempted despite the failure above
        other_tables = [t for t in ALL_TABLES if t != prefs_table]
        assert all(body["deleted_tables"][t] for t in other_tables)
        assert prefs_table in body["table_errors"]
        # Regression: raw exception text must not be echoed to the client.
        assert "db down" not in body["table_errors"][prefs_table]
        assert body["clerk_deleted"] is False
        assert "clerk_skipped" in body
        # the crux of the fix: the Clerk identity delete must never fire
        assert clerk_delete_calls == []

    def test_retry_after_fixed_table_failure_completes_clerk_deletion(
        self, test_client, authed, monkeypatch
    ):
        """After a partial failure leaves the Clerk account intact, a
        retried request (once the underlying table issue is resolved) must
        be able to reach full success -- proves the skip in the finding
        above doesn't strand the user."""
        client = _FakeSupabaseClient()
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)
        monkeypatch.setattr(
            routes_privacy_module, "_delete_clerk_user", lambda user_id: (True, None)
        )

        response = test_client.post(
            "/api/user/delete-account",
            headers=AUTH_HEADERS,
            json={"confirmation": "DELETE"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["clerk_deleted"] is True


class TestDeleteClerkUser:
    """Exercises _delete_clerk_user directly (below the Flask route) to
    verify the actual Clerk Backend API call construction and its
    success/idempotent/failure classification."""

    def test_missing_secret_key_short_circuits(self, monkeypatch):
        monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
        deleted, error = routes_privacy_module._delete_clerk_user("user_x")
        assert deleted is False
        assert "CLERK_SECRET_KEY" in error

    def test_missing_secret_key_reaches_capture_backend_error(self, monkeypatch):
        """Regression test: an unset CLERK_SECRET_KEY must not be a silent
        partial failure -- it has to surface via _capture_backend_error
        (Sentry/structured logs) exactly like the genuine Clerk-API-failure
        branch does, since the `clerk_error` HTTP response field alone isn't
        monitored in production."""
        monkeypatch.delenv("CLERK_SECRET_KEY", raising=False)
        captured = []
        monkeypatch.setattr(
            routes_privacy_module, "_capture_backend_error",
            lambda event, error, context: captured.append((event, error, context)),
        )

        deleted, error = routes_privacy_module._delete_clerk_user("user_x")

        assert deleted is False
        assert "CLERK_SECRET_KEY" in error
        assert len(captured) == 1
        event, captured_error, context = captured[0]
        assert event == "clerk_account_delete_skipped_no_secret_key"
        assert isinstance(captured_error, RuntimeError)
        assert context == {"user_id": "user_x"}

    def test_200_is_success(self, monkeypatch, mock_outbound_httpx):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
        mock_outbound_httpx.delete(
            url__regex=r"https://api\.clerk\.com/v1/users/.*"
        ).mock(return_value=httpx.Response(200, json={"deleted": True}))

        deleted, error = routes_privacy_module._delete_clerk_user("user_x")
        assert deleted is True
        assert error is None

    def test_404_is_treated_as_already_deleted_success(self, monkeypatch, mock_outbound_httpx):
        """A retried delete-account call for a user Clerk already deleted
        must not be reported as a failure."""
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
        mock_outbound_httpx.delete(
            url__regex=r"https://api\.clerk\.com/v1/users/.*"
        ).mock(return_value=httpx.Response(404, json={"error": "not found"}))

        deleted, error = routes_privacy_module._delete_clerk_user("user_x")
        assert deleted is True
        assert error is None

    def test_server_error_is_failure(self, monkeypatch, mock_outbound_httpx):
        monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_fake")
        mock_outbound_httpx.delete(
            url__regex=r"https://api\.clerk\.com/v1/users/.*"
        ).mock(return_value=httpx.Response(500, text="internal error"))

        deleted, error = routes_privacy_module._delete_clerk_user("user_x")
        assert deleted is False
        assert "500" in error


class TestRetentionEnforce:
    def test_fails_closed_when_cron_secret_unset(self, test_client, monkeypatch):
        monkeypatch.delenv("CRON_SECRET", raising=False)
        response = test_client.get("/api/devtools/retention-enforce")
        assert response.status_code == 503

    def test_rejects_missing_auth_header(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        response = test_client.get("/api/devtools/retention-enforce")
        assert response.status_code == 401

    def test_rejects_wrong_bearer_token(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        response = test_client.get(
            "/api/devtools/retention-enforce",
            headers={"Authorization": "Bearer wrong-value"},
        )
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: None)
        response = test_client.get("/api/devtools/retention-enforce", headers=CRON_HEADERS)
        assert response.status_code == 503

    def test_deletes_only_rows_older_than_each_windows_cutoff(
        self, test_client, monkeypatch
    ):
        import datetime as dt_module

        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        ask_history_table = routes_privacy_module.SUPABASE_ASK_HISTORY_TABLE
        usage_log_table = routes_privacy_module._AI_USAGE_LOG_TABLE
        client = _FakeSupabaseClient(
            table_data={
                ask_history_table: [{"id": "old1"}, {"id": "old2"}],
                usage_log_table: [{"id": "old3"}],
            },
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        before = dt_module.datetime.now(dt_module.timezone.utc)
        response = test_client.get("/api/devtools/retention-enforce", headers=CRON_HEADERS)
        after = dt_module.datetime.now(dt_module.timezone.utc)

        assert response.status_code == 200
        body = response.get_json()
        assert body["ok"] is True
        assert body["ask_history"]["deleted"] == 2
        assert body["ai_usage_log"]["deleted"] == 1

        for table_name, days in (
            (ask_history_table, routes_privacy_module._ASK_HISTORY_RETENTION_DAYS),
            (usage_log_table, routes_privacy_module._AI_USAGE_LOG_RETENTION_DAYS),
        ):
            query = client.queries[table_name][0]
            lt_calls = [c for c in query.calls if c[0] == "lt"]
            assert len(lt_calls) == 1
            column, cutoff_value = lt_calls[0][1]
            assert column == "created_at"
            cutoff_dt = dt_module.datetime.fromisoformat(cutoff_value)
            expected_earliest = before - dt_module.timedelta(days=days)
            expected_latest = after - dt_module.timedelta(days=days)
            assert expected_earliest <= cutoff_dt <= expected_latest

    def test_one_table_failure_still_reports_the_other_and_returns_500(
        self, test_client, monkeypatch
    ):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        ask_history_table = routes_privacy_module.SUPABASE_ASK_HISTORY_TABLE
        client = _FakeSupabaseClient(
            table_errors={ask_history_table: RuntimeError("db down")},
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        response = test_client.get("/api/devtools/retention-enforce", headers=CRON_HEADERS)
        assert response.status_code == 500
        body = response.get_json()
        assert body["ok"] is False
        assert "error" in body["ask_history"]
        assert "deleted" in body["ai_usage_log"]

    def test_ai_usage_log_failure_alone_is_isolated_from_ask_history(
        self, test_client, monkeypatch
    ):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        usage_log_table = routes_privacy_module._AI_USAGE_LOG_TABLE
        client = _FakeSupabaseClient(
            table_errors={usage_log_table: RuntimeError("db down")},
        )
        monkeypatch.setattr(routes_privacy_module, "_get_supabase_client", lambda: client)

        response = test_client.get("/api/devtools/retention-enforce", headers=CRON_HEADERS)
        assert response.status_code == 500
        body = response.get_json()
        assert body["ok"] is False
        assert "deleted" in body["ask_history"]
        assert "error" in body["ai_usage_log"]
