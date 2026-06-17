"""
Characterization tests for the _DAILY_STUDY_CACHE singleton entry in
backend/sefaria.py (get_daily_study()), now backed by backend.cache.TTLCache
(plan.md §3.5/§4 cache consolidation).

Note: get_daily_study() calls a hardcoded "https://www.sefaria.org/api/calendars"
URL rather than the SEFARIA_API env var used elsewhere in the codebase, so the
autouse mock_outbound_http fixture's catch-all (which only covers the
env-configured mock.sefaria.org test domain) does NOT intercept it — each
test here registers an explicit mock for the real domain instead.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib

import backend.cache as cache_module
import backend.sefaria as sefaria_module

SEFARIA_CALENDARS_URL = re.compile(r"https://www\.sefaria\.org/api/calendars.*")


@pytest.fixture(autouse=True)
def _reset_daily_study_cache():
    sefaria_module._DAILY_STUDY_CACHE.clear()
    yield
    sefaria_module._DAILY_STUDY_CACHE.clear()


def _mock_calendars_response(mock_outbound_http, calendar_items=None):
    mock_outbound_http.add(
        responses_lib.GET,
        SEFARIA_CALENDARS_URL,
        json={"calendar_items": calendar_items or []},
        status=200,
    )


class TestDailyStudyCache:
    def test_first_call_fetches_and_caches(self, mock_outbound_http):
        _mock_calendars_response(mock_outbound_http)
        result = sefaria_module.get_daily_study()
        assert isinstance(result, dict)
        assert "hebrew_date" in result
        assert sefaria_module._DAILY_STUDY_CACHE.get(sefaria_module._DAILY_STUDY_CACHE_KEY) == result

    def test_second_call_within_ttl_skips_network(self, mock_outbound_http):
        _mock_calendars_response(mock_outbound_http)
        first = sefaria_module.get_daily_study()
        second = sefaria_module.get_daily_study()

        assert first == second
        calendar_calls = [
            c for c in mock_outbound_http.calls if "api/calendars" in c.request.url
        ]
        assert len(calendar_calls) == 1

    def test_call_after_ttl_expiry_refetches(self, mock_outbound_http, monkeypatch):
        _mock_calendars_response(mock_outbound_http)
        sefaria_module.get_daily_study()

        # Simulate 6 minutes passing (TTL is 5 min) via the cache module's clock.
        real_monotonic = cache_module.time.monotonic()
        monkeypatch.setattr(
            cache_module.time, "monotonic", lambda: real_monotonic + 6 * 60
        )

        _mock_calendars_response(mock_outbound_http)
        sefaria_module.get_daily_study()

        calendar_calls = [
            c for c in mock_outbound_http.calls if "api/calendars" in c.request.url
        ]
        assert len(calendar_calls) == 2

    def test_parses_rambam_daf_yomi_mishnah_yomi_from_items(self, mock_outbound_http):
        _mock_calendars_response(mock_outbound_http, calendar_items=[
            {"title": {"en": "Daily Rambam"}, "displayValue": {"en": "Rambam Title", "he": "x"}, "ref": "Rambam 1"},
            {"title": {"en": "Daf Yomi"}, "displayValue": {"en": "Daf Title", "he": "x"}, "ref": "Daf 1"},
            {"title": {"en": "Mishnah Yomi"}, "displayValue": {"en": "Mishnah Title", "he": "x"}, "ref": "Mishnah 1"},
        ])
        result = sefaria_module.get_daily_study()
        assert result["rambam"]["ref"] == "Rambam 1"
        assert result["daf_yomi"]["ref"] == "Daf 1"
        assert result["mishnah_yomi"]["ref"] == "Mishnah 1"

    def test_network_failure_falls_back_gracefully_and_caches_fallback(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated network failure")

        monkeypatch.setattr(sefaria_module._HTTP, "get", _raise)

        result = sefaria_module.get_daily_study()
        assert isinstance(result, dict)
        assert result.get("offline") is True
        # Fallback is cached too, so a transient outage doesn't cause every
        # subsequent call to retry within the TTL window.
        assert sefaria_module._DAILY_STUDY_CACHE.get(sefaria_module._DAILY_STUDY_CACHE_KEY) == result
