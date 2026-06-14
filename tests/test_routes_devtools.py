"""
Tests for backend/routes_devtools.py routes.

Covers:
  - GET /api/devtools/reliability → 200 with stats JSON
  - GET /api/devtools/heartbeat   → 200 with checks dict
  - GET /api/stack/health         → 200 with component health dict
  - GET /api/health               → 200 (backward-compat alias for /api/stack/health)
  - GET /api/devtools/rls-audit   → 200 with RLS posture dict
  - POST /api/client-errors       → 200 {"ok": True}

Devtools routes do not require auth (they surface runtime diagnostics).
"""

from __future__ import annotations

import pytest


class TestDevtoolsReliability:
    def test_reliability_returns_200(self, test_client):
        response = test_client.get("/api/devtools/reliability")
        assert response.status_code == 200

    def test_reliability_body_is_dict(self, test_client):
        response = test_client.get("/api/devtools/reliability")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_reliability_has_stats_key(self, test_client):
        response = test_client.get("/api/devtools/reliability")
        body = response.get_json()
        assert "stats" in body

    def test_reliability_stats_has_counters(self, test_client):
        response = test_client.get("/api/devtools/reliability")
        body = response.get_json()
        stats = body.get("stats", {})
        assert "answers_total" in stats
        assert "fallback_answers" in stats


class TestDevtoolsHeartbeat:
    def test_heartbeat_returns_200(self, test_client):
        response = test_client.get("/api/devtools/heartbeat")
        assert response.status_code == 200

    def test_heartbeat_body_has_ok_key(self, test_client):
        response = test_client.get("/api/devtools/heartbeat")
        body = response.get_json()
        assert isinstance(body, dict)
        assert "ok" in body

    def test_heartbeat_has_checks(self, test_client):
        response = test_client.get("/api/devtools/heartbeat")
        body = response.get_json()
        assert "checks" in body
        assert isinstance(body["checks"], dict)


class TestStackHealth:
    def test_stack_health_returns_200(self, test_client):
        response = test_client.get("/api/stack/health")
        assert response.status_code == 200

    def test_stack_health_body_is_dict(self, test_client):
        response = test_client.get("/api/stack/health")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_stack_health_has_flask_key(self, test_client):
        response = test_client.get("/api/stack/health")
        body = response.get_json()
        assert "flask" in body

    def test_stack_health_flask_is_true(self, test_client):
        response = test_client.get("/api/stack/health")
        body = response.get_json()
        assert body.get("flask") is True


class TestApiHealthAlias:
    def test_api_health_alias_returns_200(self, test_client):
        response = test_client.get("/api/health")
        assert response.status_code == 200

    def test_api_health_alias_body_is_dict(self, test_client):
        response = test_client.get("/api/health")
        body = response.get_json()
        assert isinstance(body, dict)


class TestRlsAudit:
    def test_rls_audit_returns_200(self, test_client):
        response = test_client.get("/api/devtools/rls-audit")
        assert response.status_code == 200

    def test_rls_audit_has_strict_rls_key(self, test_client):
        response = test_client.get("/api/devtools/rls-audit")
        body = response.get_json()
        assert "strict_rls" in body


class TestClientErrors:
    def test_client_errors_post_returns_200(self, test_client):
        response = test_client.post(
            "/api/client-errors",
            json={"message": "Test error", "url": "https://test.example.com"},
            content_type="application/json",
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("ok") is True
