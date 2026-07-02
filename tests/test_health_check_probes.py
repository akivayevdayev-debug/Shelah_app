"""
Supplementary tests for backend/health_check.py's actual HTTP probe functions
and APIHealth.check()/._probe() internals — tests/test_health_check.py only
exercises circuit-state transitions via record_success()/record_failure()
directly, never the real _probe_* functions or the public check() method.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib

from backend.health_check import (
    APIHealth,
    _probe_sefaria,
    _probe_hebcal,
    _probe_gemini,
    _probe_claude,
)

SEFARIA_PROBE_URL_RE = re.compile(r"https://www\.sefaria\.org/api/texts/Berakhot\.2a.*")
HEBCAL_PROBE_URL_RE = re.compile(r"https://www\.hebcal\.com/api/holidays.*")
GEMINI_PROBE_URL_RE = re.compile(r"https://generativelanguage\.googleapis\.com/v1beta/models.*")
CLAUDE_PROBE_URL_RE = re.compile(r"https://api\.anthropic\.com/v1/models.*")


@pytest.fixture()
def health():
    return APIHealth()


class TestProbeSefaria:
    def test_200_returns_true(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, SEFARIA_PROBE_URL_RE, json={}, status=200)
            assert _probe_sefaria() is True

    def test_non_200_returns_false(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, SEFARIA_PROBE_URL_RE, status=503)
            assert _probe_sefaria() is False


class TestProbeHebcal:
    def test_200_returns_true(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, HEBCAL_PROBE_URL_RE, json={}, status=200)
            assert _probe_hebcal() is True

    def test_non_200_returns_false(self):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, HEBCAL_PROBE_URL_RE, status=500)
            assert _probe_hebcal() is False


class TestProbeGemini:
    def test_no_api_key_assumes_up(self, monkeypatch):
        monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert _probe_gemini() is True

    def test_with_api_key_200_returns_true(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, GEMINI_PROBE_URL_RE, json={}, status=200)
            assert _probe_gemini() is True

    def test_with_api_key_non_200_returns_false(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, GEMINI_PROBE_URL_RE, status=500)
            assert _probe_gemini() is False


class TestProbeClaude:
    def test_no_api_key_assumes_up(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert _probe_claude() is True

    def test_with_api_key_200_returns_true(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, CLAUDE_PROBE_URL_RE, json={}, status=200)
            assert _probe_claude() is True

    def test_with_api_key_403_still_returns_true(self, monkeypatch):
        # 403 means the endpoint is up but auth failed at a layer above — still "available".
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, CLAUDE_PROBE_URL_RE, status=403)
            assert _probe_claude() is True

    def test_with_api_key_500_returns_false(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, CLAUDE_PROBE_URL_RE, status=500)
            assert _probe_claude() is False


class TestApiHealthCheckMethod:
    def test_check_probes_and_records_success(self, health):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, SEFARIA_PROBE_URL_RE, json={}, status=200)
            result = health.check("sefaria")
            assert result is True
            assert health.status_summary()["sefaria"] == "up"

    def test_check_probes_and_records_failure(self, health):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, SEFARIA_PROBE_URL_RE, status=500)
            for _ in range(3):
                health.check("sefaria")
            assert health.status_summary()["sefaria"] == "down"

    def test_check_unknown_service_returns_true(self, health):
        assert health.check("unknown-service") is True

    def test_probe_exception_is_caught_and_recorded_as_failure(self, health, monkeypatch):
        def _raise():
            raise ConnectionError("network down")

        monkeypatch.setitem(APIHealth._PROBES, "sefaria", _raise)
        result = health.check("sefaria")
        assert result is False
        assert health._circuits["sefaria"].failures == 1
