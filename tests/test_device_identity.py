"""
Tests for backend/device_identity.py -- the cookie that ties a signed-out
answer (and its /answer/<id> link) to the browser that asked it.
"""

from __future__ import annotations

import asyncio

import pytest
from flask import Flask, jsonify

import asgi
import backend.device_identity as device
from backend import rag

TOKEN = "a" * 40


class TestOwnerForToken:
    def test_owner_is_a_prefixed_hash_not_the_token(self):
        owner = device.owner_for_token(TOKEN)
        assert owner.startswith("device:")
        assert TOKEN not in owner
        assert device.is_device_owner(owner)

    def test_same_token_same_owner_different_token_different_owner(self):
        assert device.owner_for_token(TOKEN) == device.owner_for_token(TOKEN)
        assert device.owner_for_token(TOKEN) != device.owner_for_token("b" * 40)

    @pytest.mark.parametrize("bad", [None, "", "short", "x" * 100, "has spaces " * 4, "semi;colon" * 5])
    def test_a_cookie_we_did_not_issue_is_no_identity(self, bad):
        assert device.owner_for_token(bad) is None

    def test_a_clerk_sub_is_never_a_device_owner(self):
        assert not device.is_device_owner("user_2abcDEF")


class TestBinding:
    def test_a_visitor_without_a_cookie_gets_a_minted_one(self):
        binding = device.bind_device({})
        try:
            assert binding.minted is True
            assert device.valid_token(binding.token)
            assert device.current_history_owner() == binding.owner
        finally:
            device.release_device(binding)
        assert device.current_history_owner() is None

    def test_a_returning_visitor_keeps_theirs(self):
        binding = device.bind_device({device.DEVICE_COOKIE: TOKEN})
        try:
            assert binding.minted is False
            assert binding.token == TOKEN
            assert binding.owner == device.owner_for_token(TOKEN)
        finally:
            device.release_device(binding)

    def test_a_forged_cookie_is_replaced_not_trusted(self):
        binding = device.bind_device({device.DEVICE_COOKIE: "not-a-token"})
        try:
            assert binding.minted is True
        finally:
            device.release_device(binding)

    def test_release_of_none_is_a_no_op(self):
        device.release_device(None)


class TestCookieSettings:
    def test_it_is_httponly_lax_and_site_wide(self):
        settings = device.cookie_settings(secure=True)
        assert settings["httponly"] is True
        assert settings["samesite"] == "lax"
        assert settings["path"] == "/"
        assert settings["secure"] is True
        assert settings["max_age"] == device.DEVICE_COOKIE_MAX_AGE

    @pytest.mark.parametrize("scheme,forwarded,expected", [
        ("http", None, False),
        ("https", None, True),
        ("http", "https", True),
        ("http", "https, http", True),
        ("https", "http", False),
    ])
    def test_secure_follows_the_request_scheme(self, monkeypatch, scheme, forwarded, expected):
        monkeypatch.setattr(device.auth, "_in_prod_runtime", False)
        assert device.request_is_secure(scheme, forwarded) is expected

    def test_secure_in_production_regardless_of_scheme(self, monkeypatch):
        monkeypatch.setattr(device.auth, "_in_prod_runtime", True)
        assert device.request_is_secure("http", None) is True


@pytest.fixture
def mini_app():
    """A bare Flask app with one view wrapped like /ask, recording the owner
    its code saw."""
    app = Flask(__name__)
    seen = []

    @app.post("/ask")
    @device.issues_device_cookie
    def ask():
        seen.append(device.current_history_owner())
        return jsonify(ok=True)

    @app.post("/refused")
    @device.issues_device_cookie
    def refused():
        return jsonify(error="no"), 400

    app.seen = seen
    return app


class TestFlaskDecorator:
    def test_first_signed_out_answer_sets_the_cookie_and_binds_the_owner(self, mini_app):
        response = mini_app.test_client().post("/ask")
        assert response.status_code == 200
        cookie = response.headers["Set-Cookie"]
        assert cookie.startswith(f"{device.DEVICE_COOKIE}=")
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        token = cookie.split("=", 1)[1].split(";", 1)[0]
        assert mini_app.seen == [device.owner_for_token(token)]
        assert device.current_history_owner() is None  # released after the request

    def test_a_returning_device_is_not_given_a_new_cookie(self, mini_app):
        client = mini_app.test_client()
        client.set_cookie(device.DEVICE_COOKIE, TOKEN, domain="localhost")
        response = client.post("/ask")
        assert "Set-Cookie" not in response.headers
        assert mini_app.seen == [device.owner_for_token(TOKEN)]

    def test_a_refusal_earns_no_cookie(self, mini_app):
        response = mini_app.test_client().post("/refused")
        assert response.status_code == 400
        assert "Set-Cookie" not in response.headers

    def test_a_signed_in_caller_is_never_bound_to_a_device(self, mini_app):
        response = mini_app.test_client().post("/ask", headers={"Authorization": "Bearer x.y.z"})
        assert "Set-Cookie" not in response.headers
        assert mini_app.seen == [None]


class TestStoredUnderTheDevice:
    @pytest.fixture
    def inserted(self, monkeypatch):
        import app as app_module

        rows = []

        class _Table:
            def insert(self, payload):
                rows.append(payload)
                return self

            def execute(self):
                return None

        class _Client:
            def table(self, name):
                return _Table()

        monkeypatch.setattr(app_module, "_get_supabase_client", lambda: _Client())
        return rows

    def test_a_signed_out_answer_is_saved_under_the_device(self, inserted):
        binding = device.bind_device({device.DEVICE_COOKIE: TOKEN})
        try:
            entry_id = rag._store_ask_history(None, "Q?", "A.")
        finally:
            device.release_device(binding)
        assert entry_id
        assert inserted[0]["user_id"] == device.owner_for_token(TOKEN)

    def test_the_account_wins_over_the_device(self, inserted):
        binding = device.bind_device({device.DEVICE_COOKIE: TOKEN})
        try:
            rag._store_ask_history("user_abc", "Q?", "A.")
        finally:
            device.release_device(binding)
        assert inserted[0]["user_id"] == "user_abc"

    def test_with_no_owner_nothing_is_saved(self, inserted):
        assert rag._store_ask_history(None, "Q?", "A.") is None
        assert inserted == []


class TestHistoryOwners:
    def test_account_then_device(self, mini_app):
        with mini_app.test_request_context("/", headers={"Cookie": f"{device.DEVICE_COOKIE}={TOKEN}"}):
            assert device.history_owners("user_abc") == ("user_abc", device.owner_for_token(TOKEN))
            assert device.history_owners(None) == (device.owner_for_token(TOKEN),)

    def test_nobody(self, mini_app):
        with mini_app.test_request_context("/"):
            assert device.history_owners(None) == ()

    def test_filter_owner_uses_eq_for_one_and_in_for_two(self):
        calls = []

        class _Q:
            def eq(self, key, value):
                calls.append(("eq", key, value))
                return self

            def in_(self, key, values):
                calls.append(("in", key, values))
                return self

        device.filter_owner(_Q(), ("a",))
        device.filter_owner(_Q(), ("a", "b"))
        assert calls == [("eq", "user_id", "a"), ("in", "user_id", ["a", "b"])]


class TestFastApiAsk:
    @pytest.fixture
    def pipeline(self, monkeypatch):
        seen = []

        async def fake_impl(request, payload, authorization):
            seen.append(device.current_history_owner())
            await asyncio.sleep(0)
            return {"answer": "Yes.", "sources": []}

        monkeypatch.setattr(asgi, "_ask_async_impl", fake_impl)
        return seen

    async def test_signed_out_ask_sets_the_device_cookie(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"}, headers={"X-Forwarded-For": "203.0.113.70"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert cookie.startswith(f"{device.DEVICE_COOKIE}=")
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        token = cookie.split("=", 1)[1].split(";", 1)[0]
        assert pipeline == [device.owner_for_token(token)]

    async def test_the_cookie_is_set_on_a_streamed_answer_too(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"},
            headers={"Accept": "application/x-ndjson", "X-Forwarded-For": "203.0.113.71"})
        assert response.status_code == 200
        assert response.headers["set-cookie"].startswith(f"{device.DEVICE_COOKIE}=")

    async def test_a_returning_device_keeps_its_cookie_and_owner(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"},
            headers={"X-Forwarded-For": "203.0.113.72", "Cookie": f"{device.DEVICE_COOKIE}={TOKEN}"})
        assert "set-cookie" not in response.headers
        assert pipeline == [device.owner_for_token(TOKEN)]

    async def test_an_authorization_header_means_no_device(self, fastapi_client, pipeline):
        response = await fastapi_client.post(
            "/ask", json={"question": "q"},
            headers={"X-Forwarded-For": "203.0.113.73", "Authorization": "Bearer a.b.c"})
        assert "set-cookie" not in response.headers
        assert pipeline == [None]
