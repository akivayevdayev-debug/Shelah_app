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
  - GET/PUT /api/preferences      backward-compat alias delegates to the same
    handler as /api/user/preferences
  - GET  /api/user/history        without auth → 401; with auth → happy path,
    Supabase-not-configured 503, limit clamping, exception 500
  - GET  /api/user/history/<id>   without auth → 401; with auth → happy path,
    Supabase-not-configured 503, missing entry 404, exception 500
  - DELETE /api/user/history/<id> without auth → 401; with auth → happy path,
    Supabase-not-configured 503, exception 500

Auth is enforced via `require_clerk_auth` decorator; with CLERK_ENFORCE_AUTH=false
the decorator still checks for a valid Bearer token to populate g.clerk_claims,
so requests without a token are rejected because user_id cannot be extracted.

To simulate an *authenticated* request despite CLERK_ENFORCE_AUTH=false, tests
monkeypatch `backend.auth._verify_clerk_token` (the name looked up inside the
`require_clerk_auth`/`maybe_require_clerk_auth` closures -- both decorators
are defined in `backend/auth.py` and re-imported into `app.py` as a
back-compat shim, so a function's free-variable lookups resolve against its
*defining* module's globals, not the importing module's) so any Bearer token
decodes to fake claims. `routes_user.clerk_auth_me` calls `_verify_clerk_token`
via its own direct import, so that route's tests patch
`backend.routes_user._verify_clerk_token` instead.
"""

from __future__ import annotations

import pytest

import backend.auth as auth_module
import backend.routes_user as routes_user_module
from backend.logging_setup import hash_user_id

FAKE_USER_ID = "user_test_fake_123"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}


@pytest.fixture
def authed(monkeypatch):
    """Make `require_clerk_auth`/`maybe_require_clerk_auth` accept any Bearer
    token and populate g.clerk_claims with a fake user id."""
    monkeypatch.setattr(
        auth_module,
        "_verify_clerk_token",
        lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
    )
    return FAKE_USER_ID


ENTRY_UUID = "0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f88"


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
        self.upsert_calls = []
        self.insert_calls = []
        self.eq_calls = []

    def select(self, *a, **k):
        return self

    def insert(self, *a, **k):
        self.insert_calls.append((a, k))
        return self

    def upsert(self, *a, **k):
        self.upsert_calls.append((a, k))
        return self

    def delete(self, *a, **k):
        return self

    def eq(self, *a, **k):
        self.eq_calls.append(a)
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

    def test_get_preferences_queries_by_raw_clerk_user_id(
        self, test_client, authed, monkeypatch
    ):
        """Regression (plan.md §29.6): the GET filter must use the raw Clerk
        `sub` string (e.g. "user_3DJ2PONd1x9zBnfVRlfiGhMzQPr") verbatim, never
        a value assumed to be a UUID -- the live `user_preferences.user_id`
        column must stay `text` (scripts/migrate_user_preferences_user_id_to_text.sql)
        to accept it."""
        assert not FAKE_USER_ID[:8].count("-")
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert ("user_id", FAKE_USER_ID) in fake_client._query.eq_calls

    def test_put_preferences_upserts_raw_clerk_user_id_not_uuid(
        self, test_client, authed, monkeypatch
    ):
        """Regression (plan.md §29.6): the PUT upsert payload must carry the
        raw Clerk `sub` string as `user_id`, not a UUID -- this is what a
        `uuid`-typed live column rejects with PostgREST 22P02."""
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        assert len(fake_client._query.upsert_calls) == 1
        upsert_payload = fake_client._query.upsert_calls[0][0][0]
        assert upsert_payload["user_id"] == FAKE_USER_ID

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


class TestAuthMeValidToken:
    def test_auth_me_with_valid_token_returns_authenticated(self, test_client, monkeypatch):
        monkeypatch.setattr(
            routes_user_module, "_verify_clerk_token",
            lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
        )
        response = test_client.get("/api/auth/me", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body == {"authenticated": True, "user_id": FAKE_USER_ID, "session_id": "sess_fake"}


class TestAcceptLegalAuthenticated:
    def test_accept_legal_authed_persists_to_supabase(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        body = response.get_json()
        assert body == {"success": True, "stored": "server"}

    def test_accept_legal_authed_supabase_failure_still_returns_200(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("db down"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["stored"] == "server"

    def test_accept_legal_failure_reaches_capture_backend_error(self, test_client, authed, monkeypatch):
        """Regression test (plan.md §23.2.4): this is the exact function whose
        prior on_conflict="clerk_id" bug silently dropped every legal-consent
        write with nothing but an app.logger.warning() nobody watched. A
        Supabase failure here must now be observable via
        _capture_backend_error(), not just swallowed into a log line."""
        client = _FakeSupabaseClient(error=RuntimeError("db down"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        captured = []
        monkeypatch.setattr(
            routes_user_module, "_capture_backend_error",
            lambda event, error, context: captured.append((event, error, context)),
        )

        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)

        assert response.status_code == 200
        assert len(captured) == 1
        event, error, context = captured[0]
        assert event == "accept_legal_persist_failed"
        assert isinstance(error, RuntimeError)
        assert context["user_id_hash"] == hash_user_id(FAKE_USER_ID)

    def test_accept_legal_authed_no_supabase_client_returns_server_stored(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["stored"] == "server"

    def test_accept_legal_with_age_attested_persists_it(self, test_client, authed, monkeypatch):
        """plan.md §8.B-AGE.6: age attestation is stored alongside legal consent."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post(
            "/api/accept-legal", headers=AUTH_HEADERS,
            json={"age_attested": True},
        )
        assert response.status_code == 200
        record = client._query.upsert_calls[0][0][0]
        assert record["age_attested"] is True
        assert "age_attested_at" in record

    def test_accept_legal_without_age_attested_does_not_persist_it(self, test_client, authed, monkeypatch):
        """Absence of an explicit attestation must not be silently recorded as true."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        record = client._query.upsert_calls[0][0][0]
        assert "age_attested" not in record

    def test_accept_legal_persists_client_supplied_versions(self, test_client, authed, monkeypatch):
        """plan.md §8.A.1/§8.D.2: the versions the consent modal displayed
        must be recorded verbatim, not just "accepted: true" — that's what
        makes a later material-change re-prompt distinguishable from the
        prior acceptance."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post(
            "/api/accept-legal", headers=AUTH_HEADERS,
            json={"terms_version": "3.1", "privacy_version": "3.1"},
        )
        assert response.status_code == 200
        record = client._query.upsert_calls[0][0][0]
        assert record["legal_terms_version"] == "3.1"
        assert record["legal_privacy_version"] == "3.1"

    def test_accept_legal_upserts_on_user_id_not_clerk_id(self, test_client, authed, monkeypatch):
        """Regression test (plan.md §8.D): user_preferences' real primary key
        is user_id -- there is no clerk_id column in the schema, so an
        on_conflict="clerk_id" upsert would be rejected by PostgREST on
        every real call. Consent records must key off user_id."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        args, kwargs = client._query.upsert_calls[0]
        record = args[0]
        assert record["user_id"] == FAKE_USER_ID
        assert "clerk_id" not in record
        assert kwargs.get("on_conflict") == "user_id"

    def test_accept_legal_defaults_versions_when_omitted(self, test_client, authed, monkeypatch):
        """A request with no version fields still records the server's
        current versions, rather than leaving the column null."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.post("/api/accept-legal", headers=AUTH_HEADERS)
        assert response.status_code == 200
        record = client._query.upsert_calls[0][0][0]
        assert record["legal_terms_version"] == routes_user_module.LEGAL_TERMS_VERSION
        assert record["legal_privacy_version"] == routes_user_module.LEGAL_PRIVACY_VERSION


class TestUserPreferencesMissingIdentity:
    def test_get_preferences_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.get("/api/user/preferences", headers=AUTH_HEADERS)
        assert response.status_code == 401
        assert response.get_json()["error"] == "Missing user identity"


class TestSemanticBookmarksMissingIdentityAndRls:
    def test_get_bookmarks_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.get("/api/bookmarks/semantic", headers=AUTH_HEADERS)
        assert response.status_code == 401

    def test_get_bookmarks_authed_strict_rls_no_client_is_403(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_user_module, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(routes_user_module, "_get_user_scoped_supabase_client", lambda: None)
        response = test_client.get("/api/bookmarks/semantic", headers=AUTH_HEADERS)
        assert response.status_code == 403


class TestGetAskHistory:
    def test_without_auth_is_401(self, test_client):
        response = test_client.get("/api/user/history")
        assert response.status_code == 401

    def test_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.get("/api/user/history", headers=AUTH_HEADERS)
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        response = test_client.get("/api/user/history", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_happy_path_returns_items(self, test_client, authed, monkeypatch):
        rows = [{"id": "1", "question": "Is this permitted?", "answer": "Yes."}]
        client = _FakeSupabaseClient(data=rows)
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get("/api/user/history", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["items"] == rows

    def test_limit_is_clamped_between_1_and_50(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get("/api/user/history?limit=9999", headers=AUTH_HEADERS)
        assert response.status_code == 200

    def test_supabase_exception_returns_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("db down"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get("/api/user/history", headers=AUTH_HEADERS)
        assert response.status_code == 500


class TestGetAskHistoryEntry:
    def test_a_non_uuid_id_is_404_without_a_query(self, test_client, authed, monkeypatch):
        """ids are uuids: a malformed one can't exist, and sending it to
        Postgres would only raise 22P02 (a 500)."""
        client = _FakeSupabaseClient(error=AssertionError("must not query"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        for bad in ("entry-1", "not-a-uuid", "0b6a3f58-2f5e-4c1d-9a7e-3d2b1c0a9f8"):
            response = test_client.get(f"/api/user/history/{bad}", headers=AUTH_HEADERS)
            assert response.status_code == 404, bad

    def test_without_auth_is_401(self, test_client):
        response = test_client.get(f"/api/user/history/{ENTRY_UUID}")
        assert response.status_code == 401

    def test_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.get(f"/api/user/history/{ENTRY_UUID}", headers=AUTH_HEADERS)
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        response = test_client.get(f"/api/user/history/{ENTRY_UUID}", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_happy_path_returns_entry(self, test_client, authed, monkeypatch):
        row = {"id": ENTRY_UUID, "question": "Is this permitted?", "answer": "Yes."}
        client = _FakeSupabaseClient(data=[row])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get(f"/api/user/history/{ENTRY_UUID}", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json() == row

    def test_missing_entry_is_404(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get("/api/user/history/0b6a3f58-0000-4c1d-9a7e-3d2b1c0a9f88", headers=AUTH_HEADERS)
        assert response.status_code == 404

    def test_supabase_exception_returns_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("db down"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.get(f"/api/user/history/{ENTRY_UUID}", headers=AUTH_HEADERS)
        assert response.status_code == 500

    def test_scopes_lookup_to_caller_own_user_id(self, test_client, authed, monkeypatch):
        """Even if an attacker guesses another user's entry_id, the query still
        carries `.eq("user_id", <caller's own id>)` as a second filter -- a row
        matching the id but owned by someone else matches zero rows here, RLS
        or not, and comes back as 404 rather than leaking the other user's
        answer."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        other = "9f9f9f9f-2f5e-4c1d-9a7e-3d2b1c0a9f88"
        response = test_client.get(f"/api/user/history/{other}", headers=AUTH_HEADERS)
        assert response.status_code == 404
        assert ("id", other) in client._query.eq_calls
        assert ("user_id", FAKE_USER_ID) in client._query.eq_calls


class TestDeleteAskHistoryEntry:
    def test_without_auth_is_401(self, test_client):
        response = test_client.delete("/api/user/history/entry-1")
        assert response.status_code == 401

    def test_authed_but_no_sub_claim_is_401(self, test_client, monkeypatch):
        monkeypatch.setattr(auth_module, "_verify_clerk_token", lambda token: {"sid": "sess"})
        response = test_client.delete("/api/user/history/entry-1", headers=AUTH_HEADERS)
        assert response.status_code == 401

    def test_no_supabase_client_is_503(self, test_client, authed, monkeypatch):
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: None)
        response = test_client.delete("/api/user/history/entry-1", headers=AUTH_HEADERS)
        assert response.status_code == 503

    def test_happy_path_deletes_entry(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.delete("/api/user/history/entry-1", headers=AUTH_HEADERS)
        assert response.status_code == 200
        assert response.get_json()["ok"] is True

    def test_supabase_exception_returns_500(self, test_client, authed, monkeypatch):
        client = _FakeSupabaseClient(error=RuntimeError("db down"))
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        response = test_client.delete("/api/user/history/entry-1", headers=AUTH_HEADERS)
        assert response.status_code == 500


class TestApplicationLayerCrossUserIsolation:
    """plan.md §21.2.4/§21.3 exit criterion: an application-layer test
    asserting cross-user isolation on every Supabase-backed route in this
    file, independent of whether RLS is live. RLS is defense-in-depth here,
    not the only thing standing between users' data -- every route below
    also hard-filters to `user_id` sourced from the CALLER'S OWN verified
    Clerk claims (`g.clerk_claims["sub"]`), never from request body/query
    input. These tests prove a client cannot smuggle a different user_id
    through any of those inputs to read or write another user's row, and
    would fail loudly if a future change ever read `user_id` from the
    request instead of the verified claims.
    """

    OTHER_USER_ID = "user_test_attacker_target_456"

    def test_get_preferences_ignores_client_supplied_user_id_query_param(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.get(
            f"/api/user/preferences?user_id={self.OTHER_USER_ID}", headers=AUTH_HEADERS
        )
        assert ("user_id", FAKE_USER_ID) in fake_client._query.eq_calls
        assert ("user_id", self.OTHER_USER_ID) not in fake_client._query.eq_calls

    def test_put_preferences_ignores_client_supplied_user_id_in_body(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.put(
            "/api/user/preferences",
            json={"prefs": {"theme": "dark"}, "user_id": self.OTHER_USER_ID},
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        upsert_payload = fake_client._query.upsert_calls[0][0][0]
        assert upsert_payload["user_id"] == FAKE_USER_ID

    def test_get_bookmarks_ignores_client_supplied_user_id_query_param(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.get(
            f"/api/bookmarks/semantic?user_id={self.OTHER_USER_ID}", headers=AUTH_HEADERS
        )
        assert ("user_id", FAKE_USER_ID) in fake_client._query.eq_calls
        assert ("user_id", self.OTHER_USER_ID) not in fake_client._query.eq_calls

    def test_post_bookmark_ignores_client_supplied_user_id_in_body(
        self, test_client, authed, monkeypatch
    ):
        fake_client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(
            routes_user_module, "_get_user_scoped_supabase_client", lambda: fake_client
        )
        test_client.post(
            "/api/bookmarks/semantic",
            json={
                "ref": "Genesis 1:1",
                "ai_summary": "test",
                "user_id": self.OTHER_USER_ID,
            },
            headers=AUTH_HEADERS,
            content_type="application/json",
        )
        inserted = fake_client._query.insert_calls[0][0][0]
        assert inserted["user_id"] == FAKE_USER_ID

    def test_get_history_ignores_client_supplied_user_id_query_param(
        self, test_client, authed, monkeypatch
    ):
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        test_client.get(
            f"/api/user/history?user_id={self.OTHER_USER_ID}", headers=AUTH_HEADERS
        )
        assert ("user_id", FAKE_USER_ID) in client._query.eq_calls
        assert ("user_id", self.OTHER_USER_ID) not in client._query.eq_calls

    def test_delete_history_entry_always_scopes_delete_to_caller_own_user_id(
        self, test_client, authed, monkeypatch
    ):
        """Even if an attacker guesses another user's entry_id, the delete
        query still carries `.eq("user_id", <caller's own id>)` as a second
        filter -- a delete matching the id but not owned by the caller
        matches zero rows at the database layer, RLS or not."""
        client = _FakeSupabaseClient(data=[])
        monkeypatch.setattr(routes_user_module, "_get_supabase_client", lambda: client)
        test_client.delete(
            "/api/user/history/some-other-users-entry-id", headers=AUTH_HEADERS
        )
        assert ("id", "some-other-users-entry-id") in client._query.eq_calls
        assert ("user_id", FAKE_USER_ID) in client._query.eq_calls
