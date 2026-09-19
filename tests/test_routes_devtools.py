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

    def test_stack_health_cost_breaker_unconfigured_when_unset(self, test_client, authed, monkeypatch):
        monkeypatch.delenv(cost_meter._DAILY_BUDGET_ENV, raising=False)
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert body["security"]["cost_breaker"]["configured"] is False
        assert "note" in body["security"]["cost_breaker"]

    def test_stack_health_cost_breaker_configured_when_set(self, test_client, authed, monkeypatch):
        monkeypatch.setenv(cost_meter._DAILY_BUDGET_ENV, "10.0")
        response = test_client.get("/api/stack/health", headers=AUTH_HEADERS)
        body = response.get_json()
        assert body["security"]["cost_breaker"] == {"configured": True}


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
        # plan.md §8.C.2 security-audit pass: ask_history was missing from
        # this endpoint's reported posture -- regression guard against
        # dropping it. Its own RLS policy was later dropped (plan.md §21
        # STEP 6a, 2026-08-31; it's service-role-only by design, never
        # queried through a user-scoped client) -- still listed here for
        # completeness, not as an RLS-coverage gap.
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        body = response.get_json()
        assert "ask_history" in body["tables"]

    def test_rls_audit_has_observed_key_covering_rls_tables(self, test_client, authed):
        # plan.md §21.2.2 STEP 5: the observed-query comparison (user-scoped
        # vs. service-role row count) must cover exactly the three tables an
        # RLS policy still governs -- ask_history is deliberately excluded
        # (STEP 6a: no policy left to observe).
        response = test_client.get("/api/devtools/rls-audit", headers=AUTH_HEADERS)
        body = response.get_json()
        assert set(body["observed"].keys()) == {
            "user_preferences", "user_memories", "study_bookmarks"}


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


class _FakeQuery:
    """Chainable stand-in for a supabase-py query builder: records every call
    and returns `result` from execute()."""

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "execute":
                if self.error is not None:
                    raise self.error
                return self.result
            return self

        return _record

    def table(self, name):
        self.calls.append(("table", (name,), {}))
        return self


class _Result:
    def __init__(self, data=None, count=None):
        self.data = data
        self.count = count


class TestFeedbackDigest:
    """GET /api/devtools/feedback-digest — plan.md §12.4.3: recent
    answer-feedback rows, newest first, auth-gated because comments are
    reader-supplied free text."""

    URL = "/api/devtools/feedback-digest"

    @pytest.fixture
    def devtools_module(self):
        import backend.routes_devtools as module

        return module

    def _use_client(self, monkeypatch, devtools_module, client):
        monkeypatch.setattr(devtools_module, "_get_supabase_client", lambda: client)

    def test_requires_auth(self, test_client):
        assert test_client.get(self.URL).status_code == 401

    def test_returns_503_when_supabase_is_not_configured(
        self, test_client, authed, monkeypatch, devtools_module,
    ):
        self._use_client(monkeypatch, devtools_module, None)

        response = test_client.get(self.URL, headers=AUTH_HEADERS)

        assert response.status_code == 503
        assert response.get_json() == {"error": "Supabase not configured"}

    def test_counts_verdicts_and_returns_rows_newest_first(
        self, test_client, authed, monkeypatch, devtools_module,
    ):
        rows = [
            {"verdict": "helpful", "comment": "a"},
            {"verdict": "not_helpful", "comment": "b"},
            {"verdict": "helpful", "comment": "c"},
            {"verdict": "something_else", "comment": "d"},
        ]
        client = _FakeQuery(result=_Result(data=rows))
        self._use_client(monkeypatch, devtools_module, client)

        response = test_client.get(self.URL, headers=AUTH_HEADERS)

        assert response.status_code == 200
        assert response.get_json() == {"count": 4, "helpful": 2, "not_helpful": 1, "rows": rows}
        assert ("table", (devtools_module.SUPABASE_ANSWER_FEEDBACK_TABLE,), {}) in client.calls
        assert ("order", ("created_at",), {"desc": True}) in client.calls

    def test_null_data_is_an_empty_digest(self, test_client, authed, monkeypatch, devtools_module):
        self._use_client(monkeypatch, devtools_module, _FakeQuery(result=_Result(data=None)))

        response = test_client.get(self.URL, headers=AUTH_HEADERS)

        assert response.get_json() == {"count": 0, "helpful": 0, "not_helpful": 0, "rows": []}

    @pytest.mark.parametrize(
        "query_string, expected_limit",
        [
            ("", 50), ("?limit=", 50), ("?limit=7", 7), ("?limit=200", 200), ("?limit=100000", 200),
            # A bad query string used to raise ValueError -> HTML 500 (or, for
            # a negative value, be forwarded to the query).
            ("?limit=abc", 50), ("?limit=1.5", 50), ("?limit=0", 50), ("?limit=-5", 50),
        ],
    )
    def test_limit_defaults_to_50_and_is_capped_at_200(
        self, test_client, authed, monkeypatch, devtools_module, query_string, expected_limit,
    ):
        client = _FakeQuery(result=_Result(data=[]))
        self._use_client(monkeypatch, devtools_module, client)

        assert test_client.get(self.URL + query_string, headers=AUTH_HEADERS).status_code == 200

        assert ("limit", (expected_limit,), {}) in client.calls

    def test_query_failure_returns_500_and_is_reported(
        self, test_client, authed, monkeypatch, devtools_module,
    ):
        captured = []
        monkeypatch.setattr(
            devtools_module, "_capture_backend_error",
            lambda event, exc, context: captured.append((event, str(exc), context)),
        )
        self._use_client(monkeypatch, devtools_module, _FakeQuery(error=RuntimeError("db down")))

        response = test_client.get(self.URL, headers=AUTH_HEADERS)

        assert response.status_code == 500
        assert response.get_json() == {"error": "Could not load feedback"}
        assert captured == [("feedback_digest_query_failed", "db down", {})]


class TestObserveRlsRowCounts:
    """_observe_rls_row_counts compares a user-scoped row count with the
    service-role ground truth (plan.md §21.2.2 STEP 5)."""

    TABLE_KEYS = {"user_preferences", "user_memories", "study_bookmarks"}

    @pytest.fixture
    def devtools_module(self):
        import backend.routes_devtools as module

        return module

    def _clients(self, monkeypatch, module, service, scoped):
        monkeypatch.setattr(module, "_get_supabase_client", lambda: service)
        monkeypatch.setattr(module, "_get_request_supabase_client", lambda: scoped)

    @pytest.mark.parametrize("missing", ["service", "scoped", "both"])
    def test_missing_client_marks_every_table_unobserved(self, monkeypatch, devtools_module, missing):
        service = None if missing in ("service", "both") else _FakeQuery(result=_Result(count=1))
        scoped = None if missing in ("scoped", "both") else _FakeQuery(result=_Result(count=1))
        self._clients(monkeypatch, devtools_module, service, scoped)

        observed = devtools_module._observe_rls_row_counts("user_1")

        assert set(observed) == self.TABLE_KEYS
        assert all(entry == {"observed": False, "reason": "client_unavailable"} for entry in observed.values())

    def test_matching_counts_are_reported_as_matching(self, monkeypatch, devtools_module):
        self._clients(
            monkeypatch, devtools_module,
            _FakeQuery(result=_Result(count=3)), _FakeQuery(result=_Result(count=3)),
        )

        observed = devtools_module._observe_rls_row_counts("user_1")

        assert observed["user_memories"] == {
            "observed": True, "service_role_count": 3, "user_scoped_count": 3, "matches": True,
        }

    def test_scoped_zero_versus_service_rows_is_the_silent_rls_failure(self, monkeypatch, devtools_module):
        # plan.md §21.1: when auth.uid() does not resolve, the scoped client
        # silently returns zero rows for a user who has data.
        self._clients(
            monkeypatch, devtools_module,
            _FakeQuery(result=_Result(count=4)), _FakeQuery(result=_Result(count=0)),
        )

        observed = devtools_module._observe_rls_row_counts("user_1")

        assert observed["user_preferences"]["matches"] is False
        assert observed["user_preferences"]["service_role_count"] == 4
        assert observed["user_preferences"]["user_scoped_count"] == 0

    def test_null_counts_are_treated_as_zero(self, monkeypatch, devtools_module):
        self._clients(
            monkeypatch, devtools_module,
            _FakeQuery(result=_Result(count=None)), _FakeQuery(result=_Result(count=None)),
        )

        entry = devtools_module._observe_rls_row_counts("user_1")["study_bookmarks"]

        assert entry["service_role_count"] == 0 and entry["user_scoped_count"] == 0
        assert entry["matches"] is True

    def test_query_failure_is_reported_per_table_without_aborting(self, monkeypatch, devtools_module):
        captured = []
        monkeypatch.setattr(
            devtools_module, "_capture_backend_error",
            lambda event, exc, context: captured.append((event, context)),
        )
        self._clients(
            monkeypatch, devtools_module,
            _FakeQuery(error=RuntimeError("boom")), _FakeQuery(result=_Result(count=0)),
        )

        observed = devtools_module._observe_rls_row_counts("user_1")

        assert set(observed) == self.TABLE_KEYS
        assert all(entry == {"observed": False, "reason": "query_failed"} for entry in observed.values())
        assert sorted(context["table"] for _, context in captured) == sorted(
            [
                devtools_module.SUPABASE_PREFS_TABLE,
                devtools_module.SUPABASE_USER_MEMORIES_TABLE,
                devtools_module.SUPABASE_STUDY_BOOKMARKS_TABLE,
            ]
        )
        assert {event for event, _ in captured} == {"rls_audit_observed_query_failed"}


class TestSegmentReportIdentity:
    def test_authenticated_report_logs_the_clerk_subject(self, test_client, authed, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            response = test_client.post(
                "/api/devtools/segment-report",
                json={"kind": "reader", "segment": "Genesis 1"},
                headers=AUTH_HEADERS,
            )

        assert response.status_code == 200
        assert any(
            "SEGMENT_REPORT" in record.getMessage() and f'"user_id": "{FAKE_USER_ID}"' in record.getMessage()
            for record in caplog.records
        )

    def test_anonymous_report_has_no_user_id(self, test_client, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            test_client.post("/api/devtools/segment-report", json={"kind": "reader"})

        messages = [r.getMessage() for r in caplog.records if "SEGMENT_REPORT" in r.getMessage()]
        assert messages and all('"user_id"' not in message for message in messages)
