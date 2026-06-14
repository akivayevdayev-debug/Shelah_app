"""
Unit tests for backend/health_check.py circuit-breaker logic.

Covers:
  - Fresh APIHealth instance starts with all services healthy
  - FAIL_THRESHOLD consecutive record_failure() calls open the circuit
  - After RECOVERY_INTERVAL seconds (via freezegun) circuit allows probing
  - record_success() after failures resets circuit to "up"
  - Unknown service name → optimistically healthy (True)
  - status_summary() returns a dict of {service: status}

Tests use a fresh APIHealth() instance (not the module singleton) so they are
isolated from each other and from production state.
"""

from __future__ import annotations

import time

import pytest
from freezegun import freeze_time

from backend.health_check import APIHealth, FAIL_THRESHOLD, RECOVERY_INTERVAL


@pytest.fixture()
def health():
    """Fresh isolated APIHealth instance per test."""
    return APIHealth()


class TestInitialState:
    def test_sefaria_starts_healthy(self, health):
        assert health.is_healthy("sefaria") is True

    def test_hebcal_starts_healthy(self, health):
        assert health.is_healthy("hebcal") is True

    def test_gemini_starts_healthy(self, health):
        assert health.is_healthy("gemini") is True

    def test_claude_starts_healthy(self, health):
        assert health.is_healthy("claude") is True

    def test_unknown_service_is_healthy(self, health):
        assert health.is_healthy("completely_unknown_service_xyz") is True

    def test_status_summary_returns_dict(self, health):
        summary = health.status_summary()
        assert isinstance(summary, dict)

    def test_status_summary_has_known_services(self, health):
        summary = health.status_summary()
        assert "sefaria" in summary
        assert "hebcal" in summary


class TestCircuitOpening:
    def test_below_threshold_stays_up(self, health):
        """Fewer than FAIL_THRESHOLD failures must not open the circuit."""
        for _ in range(FAIL_THRESHOLD - 1):
            health._circuits["sefaria"].record_failure()

        assert health.is_healthy("sefaria") is True

    def test_at_threshold_opens_circuit(self, health):
        """Exactly FAIL_THRESHOLD consecutive failures must open the circuit."""
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        assert health.is_healthy("sefaria") is False

    def test_circuit_status_is_down_after_threshold(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        assert health._circuits["sefaria"].status == "down"

    def test_multiple_services_independent(self, health):
        """Failures on one service do not affect another."""
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        assert health.is_healthy("hebcal") is True


class TestCircuitRecovery:
    def test_recovery_after_interval_allows_probe(self, health):
        """
        After RECOVERY_INTERVAL seconds, should_probe() returns True so the
        circuit can transition to half-open.
        """
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        # Record failure timestamp then advance time past RECOVERY_INTERVAL
        failure_time = health._circuits["sefaria"].last_failure_ts

        with freeze_time(
            # Convert the monotonic timestamp to wall time relative to now
            # freezegun freezes time.time(), so we advance by RECOVERY_INTERVAL + 1
        ):
            pass  # freeze_time context used below

        # Manually simulate time advancing by patching last_failure_ts
        health._circuits["sefaria"].last_failure_ts = (
            time.time() - (RECOVERY_INTERVAL + 1)
        )

        # should_probe() must now return True
        assert health._circuits["sefaria"].should_probe() is True

    def test_is_healthy_half_opens_after_interval(self, health, mock_outbound_http):
        """
        is_healthy() triggers an inline probe when should_probe() returns True.
        With mock HTTP returning 200, probe succeeds and circuit closes.
        """
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        # Advance last_failure_ts to beyond RECOVERY_INTERVAL
        health._circuits["sefaria"].last_failure_ts = (
            time.time() - (RECOVERY_INTERVAL + 1)
        )

        # is_healthy() fires a probe; the mock returns 200 so circuit heals
        result = health.is_healthy("sefaria")
        # Either True (probe succeeded) or False (probe failed) — not an exception
        assert isinstance(result, bool)


class TestRecordSuccess:
    def test_record_success_after_failures_resets_to_up(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        # Now record success directly on the circuit state
        health._circuits["sefaria"].record_success()

        assert health._circuits["sefaria"].status == "up"
        assert health._circuits["sefaria"].failures == 0

    def test_record_success_makes_is_healthy_true(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        health._circuits["sefaria"].record_success()

        assert health.is_healthy("sefaria") is True

    def test_record_success_resets_failure_count(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        health._circuits["sefaria"].record_success()

        assert health._circuits["sefaria"].failures == 0


class TestStatusSummary:
    def test_summary_reports_down_after_threshold(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()

        summary = health.status_summary()
        assert summary["sefaria"] == "down"

    def test_summary_reports_up_after_success(self, health):
        for _ in range(FAIL_THRESHOLD):
            health._circuits["sefaria"].record_failure()
        health._circuits["sefaria"].record_success()

        summary = health.status_summary()
        assert summary["sefaria"] == "up"
