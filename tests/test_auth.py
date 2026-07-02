"""
Tests for backend/auth.py — Clerk JWT verification and route-auth decorators.

Security-sensitive module with no prior dedicated test file (baseline
coverage came only indirectly through route tests that monkeypatch
_verify_clerk_token wholesale). This file unit-tests the actual
verification/extraction/enforcement logic directly, including the
missing-issuer, missing-token, and invalid/expired-token rejection paths
that a route-level test wouldn't otherwise reach deterministically.

jwt.PyJWKClient / jwt.decode are mocked at the boundary — this tests our
orchestration logic (issuer/audience handling, error mapping, decorator
enforcement), not the `jwt` library's own crypto correctness.
"""

from __future__ import annotations

import pytest
from flask import Flask, g

import backend.auth as auth


@pytest.fixture(autouse=True)
def _reset_jwks_client_cache():
    auth._clerk_jwks_client = None
    yield
    auth._clerk_jwks_client = None


# ─────────────────────────── _extract_bearer_token ──────────────────────────

class TestExtractBearerToken:
    def test_extracts_token_from_bearer_header(self):
        app = Flask(__name__)
        with app.test_request_context(headers={"Authorization": "Bearer abc123"}):
            assert auth._extract_bearer_token() == "abc123"

    def test_case_insensitive_bearer_prefix(self):
        app = Flask(__name__)
        with app.test_request_context(headers={"Authorization": "bearer abc123"}):
            assert auth._extract_bearer_token() == "abc123"

    def test_missing_header_returns_none(self):
        app = Flask(__name__)
        with app.test_request_context():
            assert auth._extract_bearer_token() is None

    def test_non_bearer_scheme_returns_none(self):
        app = Flask(__name__)
        with app.test_request_context(headers={"Authorization": "Basic abc123"}):
            assert auth._extract_bearer_token() is None

    def test_bearer_with_empty_token_returns_none(self):
        app = Flask(__name__)
        with app.test_request_context(headers={"Authorization": "Bearer "}):
            assert auth._extract_bearer_token() is None


# ─────────────────────────── _get_clerk_jwks_client ─────────────────────────

class TestGetClerkJwksClient:
    def test_no_issuer_configured_returns_none(self, monkeypatch):
        monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
        assert auth._get_clerk_jwks_client() is None

    def test_creates_and_caches_client(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")
        created = []

        class FakeJWKClient:
            def __init__(self, url):
                created.append(url)

        monkeypatch.setattr(auth.jwt, "PyJWKClient", FakeJWKClient)
        client1 = auth._get_clerk_jwks_client()
        client2 = auth._get_clerk_jwks_client()
        assert client1 is client2
        assert len(created) == 1
        assert created[0] == "https://clerk.example.com/.well-known/jwks.json"

    def test_issuer_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com/")
        created = []

        class FakeJWKClient:
            def __init__(self, url):
                created.append(url)

        monkeypatch.setattr(auth.jwt, "PyJWKClient", FakeJWKClient)
        auth._get_clerk_jwks_client()
        assert created[0] == "https://clerk.example.com/.well-known/jwks.json"


# ─────────────────────────── _verify_clerk_token ────────────────────────────

class TestVerifyClerkToken:
    def test_missing_token_raises_value_error(self):
        with pytest.raises(ValueError, match="Missing bearer token"):
            auth._verify_clerk_token(None)
        with pytest.raises(ValueError, match="Missing bearer token"):
            auth._verify_clerk_token("")

    def test_missing_issuer_raises_value_error(self, monkeypatch):
        monkeypatch.delenv("CLERK_JWT_ISSUER", raising=False)
        with pytest.raises(ValueError, match="CLERK_JWT_ISSUER"):
            auth._verify_clerk_token("some-token")

    def test_jwks_client_unavailable_raises(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")
        monkeypatch.setattr(auth, "_get_clerk_jwks_client", lambda: None)
        with pytest.raises(ValueError, match="JWKS client unavailable"):
            auth._verify_clerk_token("some-token")

    def test_valid_token_decoded_with_audience(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")
        monkeypatch.setenv("CLERK_AUDIENCE", "my-audience")

        class FakeSigningKey:
            key = "fake-signing-key"

        class FakeJwksClient:
            def get_signing_key_from_jwt(self, token):
                return FakeSigningKey()

        monkeypatch.setattr(auth, "_get_clerk_jwks_client", lambda: FakeJwksClient())

        decode_calls = {}

        def fake_decode(token, key, **kwargs):
            decode_calls["token"] = token
            decode_calls["key"] = key
            decode_calls.update(kwargs)
            return {"sub": "user-123"}

        monkeypatch.setattr(auth.jwt, "decode", fake_decode)
        result = auth._verify_clerk_token("valid-token")
        assert result == {"sub": "user-123"}
        assert decode_calls["issuer"] == "https://clerk.example.com"
        assert decode_calls["audience"] == "my-audience"
        assert "options" not in decode_calls

    def test_no_audience_configured_disables_aud_verification(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")
        monkeypatch.delenv("CLERK_AUDIENCE", raising=False)

        class FakeSigningKey:
            key = "fake-signing-key"

        class FakeJwksClient:
            def get_signing_key_from_jwt(self, token):
                return FakeSigningKey()

        monkeypatch.setattr(auth, "_get_clerk_jwks_client", lambda: FakeJwksClient())

        decode_calls = {}

        def fake_decode(token, key, **kwargs):
            decode_calls.update(kwargs)
            return {"sub": "user-123"}

        monkeypatch.setattr(auth.jwt, "decode", fake_decode)
        auth._verify_clerk_token("valid-token")
        assert decode_calls["options"] == {"verify_aud": False}
        assert "audience" not in decode_calls

    def test_expired_or_invalid_token_propagates_exception(self, monkeypatch):
        monkeypatch.setenv("CLERK_JWT_ISSUER", "https://clerk.example.com")

        class FakeSigningKey:
            key = "fake-signing-key"

        class FakeJwksClient:
            def get_signing_key_from_jwt(self, token):
                return FakeSigningKey()

        monkeypatch.setattr(auth, "_get_clerk_jwks_client", lambda: FakeJwksClient())

        def fake_decode(token, key, **kwargs):
            raise auth.jwt.ExpiredSignatureError("token expired")

        monkeypatch.setattr(auth.jwt, "decode", fake_decode)
        with pytest.raises(auth.jwt.ExpiredSignatureError):
            auth._verify_clerk_token("expired-token")


# ─────────────────────────── maybe_require_clerk_auth ───────────────────────

class TestMaybeRequireClerkAuth:
    def _make_app(self):
        app = Flask(__name__)

        @app.route("/protected")
        @auth.maybe_require_clerk_auth
        def protected():
            claims = getattr(g, "clerk_claims", None)
            return {"ok": True, "authenticated": claims is not None}

        return app

    def test_no_token_and_enforce_off_allows_through(self, monkeypatch):
        monkeypatch.setattr(auth, "CLERK_ENFORCE_AUTH", False)
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/protected")
        assert resp.status_code == 200
        assert resp.get_json()["authenticated"] is False

    def test_no_token_and_enforce_on_returns_401(self, monkeypatch):
        monkeypatch.setattr(auth, "CLERK_ENFORCE_AUTH", True)
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/protected")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Authentication required"

    def test_valid_token_sets_clerk_claims(self, monkeypatch):
        monkeypatch.setattr(auth, "CLERK_ENFORCE_AUTH", False)
        monkeypatch.setattr(auth, "_verify_clerk_token", lambda token: {"sub": "user-1"})
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/protected", headers={"Authorization": "Bearer valid"})
        assert resp.status_code == 200
        assert resp.get_json()["authenticated"] is True

    def test_invalid_token_returns_401_regardless_of_enforcement(self, monkeypatch):
        monkeypatch.setattr(auth, "CLERK_ENFORCE_AUTH", False)

        def _raise(token):
            raise ValueError("bad token")

        monkeypatch.setattr(auth, "_verify_clerk_token", _raise)
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/protected", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Invalid or expired Clerk token"


# ─────────────────────────── require_clerk_auth ─────────────────────────────

class TestRequireClerkAuth:
    def _make_app(self):
        app = Flask(__name__)

        @app.route("/strict")
        @auth.require_clerk_auth
        def strict():
            return {"ok": True, "user": g.clerk_claims.get("sub")}

        return app

    def test_no_token_always_returns_401(self):
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/strict")
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Authentication required"

    def test_valid_token_grants_access(self, monkeypatch):
        monkeypatch.setattr(auth, "_verify_clerk_token", lambda token: {"sub": "user-42"})
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/strict", headers={"Authorization": "Bearer valid"})
        assert resp.status_code == 200
        assert resp.get_json()["user"] == "user-42"

    def test_invalid_token_returns_401(self, monkeypatch):
        def _raise(token):
            raise ValueError("expired")

        monkeypatch.setattr(auth, "_verify_clerk_token", _raise)
        app = self._make_app()
        client = app.test_client()
        resp = client.get("/strict", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
        assert resp.get_json()["error"] == "Invalid or expired Clerk token"
