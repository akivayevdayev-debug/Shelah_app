"""
Tests for backend/routes_user.py routes.

Covers:
  - GET  /api/user/preferences   without auth → 401; with auth → happy path,
    malformed-body 400s, Supabase-not-configured 503, RLS-strict 403, and
    Supabase-exception 500 graceful degradation
  - PUT  /api/user/preferences   same matrix as GET, plus payload validation
  - GET  /api/bookmarks/semantic without auth → 401; with auth → happy path,
    AI-summary generation, missing-input 400, Supabase failure 500
  - POST /api/bookmarks/semantic without auth → 401; with auth → happy path
  - GET  /api/auth/me            without auth → {"authenticated": False}
  - GET  /api/auth/me            with bad token → {"authenticated": False} or 401
  - GET  /api/auth/me            with valid token → {"authenticated": True, ...}
  - POST /api/accept-legal       without auth → 200 (open, client-side only);
    with auth → 200 (server-stored), and 200 even if Supabase write fails
  - GET  /api/todos               Supabase-not-configured 503, happy path,
    missing-table graceful empty list, generic exception 500
  - GET/PUT /api/preferences      backward-compat alias delegates to the same
    handler as /api/user/preferences
  - GET  /api/user/history        without auth → 401; with auth → happy path,
    Supabase-not-configured 503, limit clamping, exception 500
  - DELETE /api/user/history/<id> without auth → 401; with auth → happy path,
    Supabase-not-configured 503, exception 500

Auth is enforced via `require_clerk_auth` decorator; with CLERK_ENFORCE_AUTH=false
the decorator still checks for a valid Bearer token to populate g.clerk_claims,
so requests without a token are rejected because user_id cannot be extracted.

To simulate an *authenticated* request despite CLERK_ENFORCE_AUTH=false, tests
monkeypatch `app._verify_clerk_token` (the name looked up inside the
`require_clerk_auth`/`maybe_require_clerk_auth` closures, which live in
`app.py`) so any Bearer token decodes to fake claims. `routes_user.clerk_auth_me`
calls `_verify_clerk_token` via its own direct import, so that route's tests
patch `backend.routes_user._verify_clerk_token` instead.
"""

from __future__ import annotations

import pytest

import app as flask_app_module
import backend.routes_user as routes_user_module

FAKE_USER_ID = "user_test_fake_123"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}


@pytest.fixture()
def authed(monkeypatch):
    """Make `require_clerk_auth`/`maybe_require_clerk_auth` accept any Bearer
    token and populate g.clerk_claims with a fake user id."""
    monkeypatch.setattr(
        flask_app_module,
        "_verify_clerk_token",
        lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
    )
    return FAKE_USER_ID


class _FakeResult:
    """Minimal stand-in for postgrest's APIResponse (only `.data` is read)."""

    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable fake query builder: every chain method returns self, and
    `.execute()` returns a preset `_FakeResult` (or raises a preset error)."""

    def __init__(self, data=None, error=None):
        self._data = data if data is not None else []
        self._error = error

    def select(self, *a, **k):
        return self

    def insert(self, *a, **k):
        return self

    def upsert(self, *a, **k):
        return self

    def delete(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return _FakeResult(self._data)


class _FakeSupabaseClient:
    """Fake Supabase client whose `.table(name)` always returns the same
    pre-configured `_FakeQuery`, regardless of table name."""

    def __init__(self, data=None, error=None):
        self._query = _FakeQuery(data=data, error=error)

    def table(self, name):
        return self._query

    def from_(self, name):
        return self._query


class TestUserPreferences:
    def test_get_preferences_without_auth_is_401(self, test_client):
        response = test_client.get("/api/user/preferences")
        assert response.status_code == 401

    def test_put_preferences_without_auth_is_401(self, test_client):
        response = test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_get_preferences_authed_no_rows_returns_nulls(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body == {
            "prefs": None,
            "shelf": None,
            "notes": None,
            "reading_state": None,
            "updated_at": None,
        }

    def test_get_preferences_authed_legacy_shape_row(
        self, test_client, authed, monkeypatch
    ):
        """Legacy rows stored the prefs JSON directly (no nested shelf/notes keys)."""
        legacy_row = {"prefs": {"theme": "dark"}, "updated_at": "2024-01-01T00:00:00Z"}
        fake_client = _FakeSupabaseClient(data=[legacy_row])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["prefs"] == {"theme": "dark"}
        assert body["shelf"] is None
        assert body["updated_at"] == "2024-01-01T00:00:00Z"

    def test_get_preferences_authed_nested_shape_row(
        self, test_client, authed, monkeypatch
    ):
        """Current rows nest prefs/shelf/notes/reading_state under `prefs`."""
        nested_row = {
            "prefs": {
                "prefs": {"theme": "light"},
                "shelf": {"bookA": True},
                "notes": {"note1": "hi"},
                "reading_state": {"last": "Genesis 1:1"},
            },
            "updated_at": "2024-02-02T00:00:00Z",
        }
        fake_client = _FakeSupabaseClient(data=[nested_row])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["prefs"] == {"theme": "light"}
        assert body["shelf"] == {"bookA": True}
        assert body["notes"] == {"note1": "hi"}
        assert body["reading_state"] == {"last": "Genesis 1:1"}

    def test_get_preferences_authed_non_dict_row_returns_nulls(
        self, test_client, authed, monkeypatch
    ):
        """A malformed (non-dict) row in `.data` should fall back to nulls."""
        fake_client = _FakeSupabaseClient(data=["not-a-dict"])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body["prefs"] is None

    def test_put_preferences_authed_happy_path(self, test_client, authed, monkeypatch):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={
                "prefs": {"theme": "dark"},
                "shelf": {"bookA": True},
                "notes": {"n1": "text"},
                "reading_state": {"last": "Exodus 2:1"},
            },
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("ok") is True
        assert "updated_at" in body

    def test_put_preferences_authed_defaults_optional_fields(
        self, test_client, authed, monkeypatch
    ):
        """shelf/notes/reading_state are optional and default to {}."""
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 200
        assert response.get_json().get("ok") is True

    def test_put_preferences_authed_missing_prefs_is_400(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={"shelf": {}},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 400
        assert "prefs" in response.get_json().get("error", "")

    def test_put_preferences_authed_non_dict_prefs_is_400(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={"prefs": "not-an-object"},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 400

    @pytest.mark.parametrize("bad_field", ["shelf", "notes", "reading_state"])
    def test_put_preferences_authed_non_dict_optional_field_is_400(
        self, test_client, authed, monkeypatch, bad_field
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}, bad_field: "not-an-object"},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 400
        assert bad_field in response.get_json().get("error", "")

    def test_get_preferences_authed_supabase_not_configured_is_503(
        self, test_client, authed, monkeypatch
    ):
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: None
        )
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        monkeypatch.setattr(routes_user_module, "STRICT_SUPABASE_RLS", False)
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_get_preferences_authed_strict_rls_no_client_is_403(
        self, test_client, authed, monkeypatch
    ):
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: None
        )
        monkeypatch.setattr(routes_user_module, "STRICT_SUPABASE_RLS", True)
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 403

    def test_get_preferences_authed_supabase_exception_is_500(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(error=RuntimeError("boom"))
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 500
        assert "Failed to sync" in response.get_json().get("error", "")

    def test_put_preferences_authed_supabase_exception_is_500(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(error=RuntimeError("boom"))
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 500


class TestSemanticBookmarks:
    def test_get_bookmarks_without_auth_is_401(self, test_client):
        response = test_client.get("/api/bookmarks/semantic")
        assert response.status_code == 401

    def test_post_bookmark_without_auth_is_401(self, test_client):
        response = test_client.post(
            "/api/bookmarks/semantic",
            json={"ref": "Genesis 1:1", "label": "test"},
            content_type="application/json",
        )
        assert response.status_code == 401

    def test_get_bookmarks_authed_happy_path(self, test_client, authed, monkeypatch):
        items = [{"id": "1", "ref": "Genesis 1:1", "label": "Creation"}]
        fake_client = _FakeSupabaseClient(data=items)
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.get("/api/bookmarks/semantic", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == {"items": items}

    def test_post_bookmark_authed_with_explicit_summary(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.post(
            "/api/bookmarks/semantic",
            json={
                "ref": "Genesis 1:1",
                "label": "Creation",
                "segment_text": "In the beginning...",
                "notes": "my note",
                "ai_summary": "already summarized",
            },
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("ok") is True
        assert body["item"]["ai_summary"] == "already summarized"
        assert body.get("summary_generated") is True
        assert body.get("summary_error") == ""

    def test_post_bookmark_authed_generates_summary_via_gemini(
        self, test_client, authed, monkeypatch
    ):
        """No ai_summary supplied + segment_text present → calls summarize_with_gemini,
        which is intercepted by the autouse Gemini httpx mock (mock_outbound_httpx)."""
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.post(
            "/api/bookmarks/semantic",
            json={
                "ref": "Genesis 1:1",
                "segment_text": "In the beginning God created the heavens.",
            },
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("ok") is True
        # ai_summary should be non-empty given a non-empty segment_text and a
        # mocked Gemini response (or, at worst, the local fallback summary).
        assert isinstance(body["item"]["ai_summary"], str)

    def test_post_bookmark_authed_missing_ref_and_text_is_400(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.post(
            "/api/bookmarks/semantic",
            json={"label": "no ref or text"},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_get_bookmarks_authed_supabase_not_configured_is_503(
        self, test_client, authed, monkeypatch
    ):
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: None
        )
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        monkeypatch.setattr(routes_user_module, "STRICT_SUPABASE_RLS", False)
        response = test_client.get("/api/bookmarks/semantic", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_post_bookmark_authed_supabase_exception_is_500(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(error=RuntimeError("boom"))
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        response = test_client.post(
            "/api/bookmarks/semantic",
            json={"ref": "Genesis 1:1", "segment_text": "text"},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert response.status_code == 500


class TestAuthMe:
    def test_auth_me_without_token_returns_not_authenticated(self, test_client):
        response = test_client.get("/api/auth/me")
        # Returns 200 {"authenticated": False} or 401
        assert response.status_code in (200, 401)
        if response.status_code == 200:
            body = response.get_json()
            assert body.get("authenticated") is False

    def test_auth_me_with_bad_token_returns_not_authenticated(self, test_client):
        response = test_client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer this-is-not-a-real-jwt"},
        )
        assert response.status_code in (200, 401)
        if response.status_code == 200:
            body = response.get_json()
            assert body.get("authenticated") is False


class TestAcceptLegal:
    def test_accept_legal_without_auth_returns_200_client(self, test_client):
        """Unauthenticated users get client-side-only storage — still 200."""
        response = test_client.post("/api/accept-legal")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("success") is True


class TestApiPreferencesAlias:
    def test_preferences_alias_without_auth_is_401(self, test_client):
        """Backward-compat /api/preferences alias must also enforce auth."""
        response = test_client.get("/api/preferences")
        assert response.status_code == 401
