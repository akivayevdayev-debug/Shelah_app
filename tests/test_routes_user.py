"""
Tests for backend/routes_user.py routes.

Covers:
  - GET  /api/user/preferences   without auth → 401
  - PUT  /api/user/preferences   without auth → 401
  - GET  /api/bookmarks/semantic without auth → 401
  - POST /api/bookmarks/semantic without auth → 401
  - GET  /api/auth/me            without auth → {"authenticated": False}
  - GET  /api/auth/me            with bad token → {"authenticated": False} or 401
  - POST /api/accept-legal       without auth → 200 (open, client-side only)

Auth is enforced via `require_clerk_auth` decorator; with CLERK_ENFORCE_AUTH=false
the decorator still checks for a valid Bearer token to populate g.clerk_claims,
so requests without a token are rejected because user_id cannot be extracted.
"""

from __future__ import annotations

import pytest


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
