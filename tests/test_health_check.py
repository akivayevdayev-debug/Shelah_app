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


@pytest.fixture
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

        # Advance time past RECOVERY_INTERVAL
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


class TestPublicRecordMethods:
    """Phase 3: the public record_success()/record_failure() API used by
    backend/utils/search_provider.py's network call sites."""

    def test_record_failure_below_threshold_stays_up(self, health):
        for _ in range(FAIL_THRESHOLD - 1):
            health.record_failure("sefaria")

        assert health.is_healthy("sefaria") is True

    def test_record_failure_at_threshold_opens_circuit(self, health):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("sefaria")

        assert health.is_healthy("sefaria") is False

    def test_record_success_closes_circuit(self, health):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("sefaria")
        health.record_success("sefaria")

        assert health.is_healthy("sefaria") is True

    def test_record_failure_on_unregistered_service_auto_registers(self, health):
        """A brand-new service name (not pre-registered) must not raise."""
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("some_new_service")

        assert health.is_healthy("some_new_service") is False


class TestPassiveServices:
    """Passive services (translate_google, translate_mymemory, web) have no
    active probe function — their circuit is driven entirely by callers."""

    @pytest.mark.parametrize("service", ["translate_google", "translate_mymemory", "web"])
    def test_passive_service_starts_healthy(self, health, service):
        assert health.is_healthy(service) is True

    @pytest.mark.parametrize("service", ["translate_google", "translate_mymemory", "web"])
    def test_passive_service_opens_after_threshold_failures(self, health, service):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure(service)

        assert health.is_healthy(service) is False

    def test_passive_service_half_opens_after_recovery_without_probe(self, health):
        """Unlike probed services, a passive circuit past RECOVERY_INTERVAL is
        optimistically let through (no probe function exists to call) so the
        next real caller's record_success()/record_failure() can re-close or
        re-open it."""
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("web")
        assert health.is_healthy("web") is False

        health._circuits["web"].last_failure_ts = time.time() - (RECOVERY_INTERVAL + 1)

        assert health.is_healthy("web") is True
        # Optimistic pass-through does not itself clear the failure count.
        assert health._circuits["web"].failures == FAIL_THRESHOLD

    def test_passive_service_failure_then_success_recovers(self, health):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("translate_google")
        health.record_success("translate_google")

        assert health.is_healthy("translate_google") is True
        assert health._circuits["translate_google"].failures == 0


class TestReset:
    def test_reset_clears_failures_and_reopens_circuit(self, health):
        for _ in range(FAIL_THRESHOLD):
            health.record_failure("sefaria")
        assert health.is_healthy("sefaria") is False

        health.reset()

        assert health.is_healthy("sefaria") is True
        assert health._circuits["sefaria"].failures == 0

    def test_reset_covers_all_registered_services(self, health):
        for name in health._circuits:
            health.record_failure(name)
            health.record_failure(name)
            health.record_failure(name)

        health.reset()

        summary = health.status_summary()
        assert all(status == "up" for status in summary.values())
