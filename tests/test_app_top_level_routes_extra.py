"""
Supplementary coverage for app.py/asgi.py top-level routes not measured by
--cov=backend (pytest.ini scopes coverage there since app.py is a documented
migration candidate — see plan.md). This file ensures every one of the 12
top-level HTTP endpoints has a real request/response test: app.py's 10
(/, /settings, /profile share one view function; /terms, /privacy,
/accessibility, /manifest.webmanifest, /favicon.ico, /service-worker.js,
/ask are covered by test_routes_core.py + test_ask.py) plus asgi.py's 2
(/ask covered by test_ask.py; /api/async/health had zero coverage anywhere).
"""

from __future__ import annotations



class TestSettingsAndProfileRoutesResolve:
    def test_settings_route_returns_200(self, test_client):
        response = test_client.get("/settings")
        assert response.status_code == 200

    def test_profile_route_returns_200(self, test_client):
        response = test_client.get("/profile")
        assert response.status_code == 200

    def test_settings_and_profile_render_same_view_as_index(self, test_client):
        index_body = test_client.get("/").data
        settings_body = test_client.get("/settings").data
        profile_body = test_client.get("/profile").data
        assert index_body == settings_body == profile_body


class TestCustom404Page:
    def test_unknown_path_returns_404_status(self, test_client):
        response = test_client.get("/this-path-does-not-exist")
        assert response.status_code == 404

    def test_unknown_path_renders_branded_template(self, test_client):
        response = test_client.get("/this-path-does-not-exist")
        assert b"Page Not Found" in response.data
        assert b"Back to Sh'elah" in response.data


class TestAsyncHealthEndpoint:
    async def test_async_health_returns_200(self, fastapi_client):
        response = await fastapi_client.get("/api/async/health")
        assert response.status_code == 200

    async def test_async_health_body_shape(self, fastapi_client):
        response = await fastapi_client.get("/api/async/health")
        body = response.json()
        assert body["ok"] is True
        assert body["runtime"] == "fastapi"
        assert body["flask_mounted"] is True
        assert "ts" in body
