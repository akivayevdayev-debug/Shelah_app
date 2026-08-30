"""
Tests for backend/routes_devtools.py routes.

Covers:
  - GET /api/devtools/reliability → auth-gated, 200 with stats JSON once authed
  - GET /api/devtools/heartbeat   → 200 (public), checks dict with no config booleans
  - GET /api/stack/health         → auth-gated, 200 with component health dict once authed
  - GET /api/health               → auth-gated alias for /api/stack/health
  - GET /api/devtools/rls-audit   → auth-gated, 200 with RLS posture dict once authed
  - POST /api/client-errors       → 200 {"ok": True}, rate-limited

Security audit P2: /api/stack/health, /api/devtools/reliability,
/api/devtools/rls-audit, and the /api/health alias now require Clerk auth —
none of them are called by any frontend feature, so gating is zero-regression.
/api/devtools/heartbeat stays public (it backs the real, if hidden,
devtools-inspector UI panel any site visitor can trigger) but no longer
returns Clerk/Supabase configuration-presence booleans.
"""

from __future__ import annotations

import pytest

import backend.auth as auth_module
from backend import cost_meter

FAKE_USER_ID = "user_test_fake_devtools"
AUTH_HEADERS = {"Authorization": "Bearer faketoken.faketoken.faketoken"}


@pytest.fixture
def authed(monkeypatch):
    """Make require_clerk_auth accept any Bearer token."""
    monkeypatch.setattr(
        auth_module, "_verify_clerk_token",
        lambda token: {"sub": FAKE_USER_ID, "sid": "sess_fake"},
    )
    return FAKE_USER_ID


class TestDevtoolsBudgetCheck:
    """GET /api/devtools/budget-check — daily AI-spend guardrail (§8.E.1),
    intended to be triggered by Vercel Cron. CRON_SECRET-gated, and fails
    closed (503) rather than open when CRON_SECRET isn't configured at all
    (security audit P3)."""

    def test_returns_200_and_delegates_to_cost_meter(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")

        async def _fake_check():
            return {
                "configured": True, "total_usd": 1.23, "threshold_usd": 5.0,
                "call_count": 3, "exceeded": False,
            }
        monkeypatch.setattr(cost_meter, "check_daily_budget_and_alert", _fake_check)

        response = test_client.get(
            "/api/devtools/budget-check",
            headers={"Authorization": "Bearer top-secret-cron-value"},
        )

        assert response.status_code == 200
        body = response.get_json()
        assert body["total_usd"] == 1.23
        assert body["exceeded"] is False

    def test_fails_closed_when_cron_secret_unset(self, test_client, monkeypatch):
        monkeypatch.delenv("CRON_SECRET", raising=False)
        response = test_client.get("/api/devtools/budget-check")
        assert response.status_code == 503

    def test_rejects_missing_auth_header_when_cron_secret_set(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        response = test_client.get("/api/devtools/budget-check")
        assert response.status_code == 401

    def test_rejects_wrong_bearer_token(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        response = test_client.get(
            "/api/devtools/budget-check",
            headers={"Authorization": "Bearer wrong-value"},
        )
        assert response.status_code == 401

    def test_accepts_correct_bearer_token(self, test_client, monkeypatch):
        monkeypatch.setenv("CRON_SECRET", "top-secret-cron-value")
        response = test_client.get(
            "/api/devtools/budget-check",
            headers={"Authorization": "Bearer top-secret-cron-value"},
        )
        assert response.status_code == 200


class TestDevtoolsReliability:
    def test_reliability_requires_auth(self, test_client):
        response = test_client.get("/api/devtools/reliability")
        assert response.status_code == 401

    def test_reliability_returns_200_when_authed(self, test_client, authed):
        response = test_client.get("/api/devtools/reliability", headers=AUTH_HEADERS)
        assert response.status_code == 200

    def test_reliability_body_is_dict(self, test_client, authed):
        response = test_client.get("/api/devtools/reliability", headers=AUTH_HEADERS)
        body = response.get_json()
        assert isinstance(body, dict)

    def test_reliability_has_stats_key(self, test_client, authed):
        response = test_client.get("/api/devtools/reliability", headers=AUTH_HEADERS)
        body = response.get_json()
        assert "stats" in body

    def test_reliability_stats_has_counters(self, test_client, authed):
        response = test_client.get("/api/devtools/reliability", headers=AUTH_HEADERS)
        body = response.get_json()
        stats = body.get("stats", {})
        assert "answers_total" in stats
        assert "fallback_answers" in stats


class TestDevtoolsHeartbeat:
    """Public/unauthenticated by design — backs templates/index.html's
    devtools-inspector panel, reachable by any site visitor."""

    def test_heartbeat_returns_200_without_auth(self, test_client):
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

    def test_heartbeat_checks_omit_configuration_booleans(self, test_client):
        """Security audit P2: Clerk/Supabase configuration-presence booleans
        must not leak to this unauthenticated, publicly-reachable route."""
        response = test_client.get("/api/devtools/heartbeat")
        checks = response.get_json()["checks"]
        assert "clerk_configured" not in checks
        assert "supabase_service_ready" not in checks
        assert "supabase_publishable_ready" not in checks
        assert "library_popular_ready" in checks


class TestStackHealth:
    def test_stack_health_requires_auth(self, test_client):
        response = test_client.get("/api/stack/health")
        assert response.status_code == 401

    def test_stack_health_returns_200_when_authed(self, test_client, authed):
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        assert response.status_code == 200

    def test_stack_health_body_is_dict(self, test_client, authed):
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert isinstance(body, dict)

    def test_stack_health_has_flask_key(self, test_client, authed):
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert "flask" in body

    def test_stack_health_flask_is_true(self, test_client, authed):
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert body.get("flask") is True


class TestApiHealthAlias:
    def test_api_health_alias_requires_auth(self, test_client):
        response = test_client.get("/api/health")
        assert response.status_code == 401

    def test_api_health_alias_returns_200_when_authed(self, test_client, authed):
        response = test_client.get("/api/health", headers=AUTH_HEADERS)
        assert response.status_code == 200

    def test_api_health_alias_body_is_dict(self, test_client, authed):
        response = test_client.get("/api/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert isinstance(body, dict)


class TestRlsAudit:
    def test_rls_audit_requires_auth(self, test_client):
        response = test_client.get("/api/devtools/rls-audit")
        assert response.status_code == 401

    def test_rls_audit_returns_200_when_authed(self, test_client, authed):
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        assert response.status_code == 200

    def test_rls_audit_has_strict_rls_key(self, test_client, authed):
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        body = response.get_json()
        assert "strict_rls" in body

    def test_rls_audit_strict_rls_is_enforced_by_default(self, test_client, authed):
        # Regression guard (plan.md §8.C.2): strict RLS must be the enforced
        # default, not opt-in -- this test fails loudly if that literal is
        # ever flipped to an env-toggle or to False.
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        body = response.get_json()
        assert body["strict_rls"] is True

    def test_rls_audit_reports_ask_history_table(self, test_client, authed):
        # plan.md §8.C.2 security-audit pass: ask_history has its own RLS
        # policy (scripts/migrate_ask_history.sql) but was missing from this
        # endpoint's reported posture -- regression guard against dropping it.
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        body = response.get_json()
        assert "ask_history" in body["tables"]


class TestClientErrors:
    """POST /api/client-errors — plan.md §16.2/§16.4 hardening: same-origin
    required, stack capped well below the old 8000-char ceiling, and the
    spoofable client IP is never forwarded into the Sentry context."""

    def test_client_errors_post_returns_200_with_matching_origin(self, test_client):
        response = test_client.post(
            "/api/client-errors",
            json={"message": "Test error", "url": "https://test.example.com"},
            content_type="application/json",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("ok") is True

    def test_client_errors_post_returns_200_with_matching_referer(self, test_client):
        """Origin is absent on some legitimate same-origin requests; Referer
        is the documented fallback."""
        response = test_client.post(
            "/api/client-errors",
            json={"message": "Test error"},
            content_type="application/json",
            headers={"Referer": "http://localhost/some/page"},
        )
        assert response.status_code == 200

    def test_client_errors_rejects_cross_origin_request(self, test_client):
        response = test_client.post(
            "/api/client-errors",
            json={"message": "Test error"},
            content_type="application/json",
            headers={"Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403

    def test_client_errors_rejects_request_with_no_origin_or_referer(self, test_client):
        response = test_client.post(
            "/api/client-errors",
            json={"message": "Test error"},
            content_type="application/json",
        )
        assert response.status_code == 403

    def test_client_errors_caps_stack_well_below_old_8000_char_ceiling(self, test_client, monkeypatch):
        from backend import routes_devtools

        captured = []
        monkeypatch.setattr(
            routes_devtools, "_capture_backend_error",
            lambda event, message, context: captured.append(context),
        )
        response = test_client.post(
            "/api/client-errors",
            json={"message": "boom", "stack": "x" * 8000},
            content_type="application/json",
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200
        assert len(captured[0]["stack"]) == routes_devtools._CLIENT_ERROR_STACK_MAX_CHARS
        assert routes_devtools._CLIENT_ERROR_STACK_MAX_CHARS < 8000

    def test_client_errors_never_forwards_client_ip_to_sentry_context(self, test_client, monkeypatch):
        from backend import routes_devtools

        captured = []
        monkeypatch.setattr(
            routes_devtools, "_capture_backend_error",
            lambda event, message, context: captured.append(context),
        )
        response = test_client.post(
            "/api/client-errors",
            json={"message": "boom"},
            content_type="application/json",
            headers={"Origin": "http://localhost", "X-Forwarded-For": "203.0.113.5"},
        )
        assert response.status_code == 200
        assert "ip" not in captured[0]


class TestSegmentReport:
    """POST /api/devtools/segment-report — plan.md §8.C.5 security-audit
    pass: non-string JSON field values used to short-circuit the `or`
    fallback and crash `.strip()` with an unhandled AttributeError -> 500."""

    def test_string_fields_returns_200(self, test_client):
        response = test_client.post(
            "/api/devtools/segment-report",
            json={"kind": "reader", "message": "gap found", "segment": "Genesis 1"},
        )
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "logged": True}

    def test_missing_body_returns_200(self, test_client):
        response = test_client.post(
            "/api/devtools/segment-report", content_type="application/json")
        assert response.status_code == 200

    def test_non_string_field_values_do_not_crash(self, test_client):
        response = test_client.post(
            "/api/devtools/segment-report",
            json={
                "kind": 1,
                "message": ["not", "a", "string"],
                "segment": {"nested": "object"},
                "ref": True,
                "view_type": None,
                "view_value": 3.14,
            },
        )
        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "logged": True}
