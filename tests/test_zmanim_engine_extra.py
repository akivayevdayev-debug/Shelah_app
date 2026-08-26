"""
Supplementary coverage tests for backend/zmanim_engine.py, targeting branches
tests/test_zmanim_cache.py and tests/test_zmanim_dst.py don't reach: the
private helpers (_cache_coord, _resolve_timezone, _get_weekly_shabbat_parasha,
_get_omer_info), the Bukharian sunset offset / Friday shabbat-warning /
non-Shabbat havdalah branches in get_community_zmanim, its top-level
exception handler, and get_monthly_events' emoji-by-category branches plus
its exception handler.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
import responses as responses_lib

import backend.zmanim_engine as ze
from backend.health_check import FAIL_THRESHOLD

NYC_LAT, NYC_LON, NYC_TZ = 40.7128, -74.006, "America/New_York"


@pytest.fixture(autouse=True)
def _reset_caches():
    ze._HEBCAL_DAY_CACHE.clear()
    ze._HEBCAL_MONTH_CACHE.clear()
    yield
    ze._HEBCAL_DAY_CACHE.clear()
    ze._HEBCAL_MONTH_CACHE.clear()


class TestCacheCoord:
    def test_rounds_float(self):
        assert ze._cache_coord(40.712812345) == 40.7128

    def test_non_numeric_passthrough_on_exception(self):
        assert ze._cache_coord("not-a-number") == "not-a-number"


class TestResolveTimezone:
    def test_given_tz_used_directly(self):
        _, tz_str = ze._resolve_timezone(NYC_LAT, NYC_LON, given_tz="Europe/London")
        assert tz_str == "Europe/London"

    def test_resolves_from_coordinates(self):
        _, tz_str = ze._resolve_timezone(NYC_LAT, NYC_LON)
        assert tz_str == "America/New_York"

    def test_timezonefinder_exception_falls_back_to_default(self, monkeypatch):
        class FakeFinder:
            def timezone_at(self, lng, lat):
                raise RuntimeError("boom")
        monkeypatch.setattr(ze, "_tf", FakeFinder())
        _, tz_str = ze._resolve_timezone(999, 999)
        assert tz_str == "America/New_York"

    def test_unresolvable_coordinates_falls_back_to_default(self, monkeypatch):
        class FakeFinder:
            def timezone_at(self, lng, lat):
                return None
        monkeypatch.setattr(ze, "_tf", FakeFinder())
        _, tz_str = ze._resolve_timezone(0, 0)
        assert tz_str == "America/New_York"


class TestGetWeeklyShabbatParasha:
    def test_strips_parashat_prefix(self, monkeypatch):
        monkeypatch.setattr(ze.calendar_engine, "get_parasha", lambda d: "Parashat Vaera")
        assert ze._get_weekly_shabbat_parasha(date(2026, 1, 12)) == "Vaera"

    def test_strips_parasha_prefix(self, monkeypatch):
        monkeypatch.setattr(ze.calendar_engine, "get_parasha", lambda d: "Parasha Bo")
        assert ze._get_weekly_shabbat_parasha(date(2026, 1, 12)) == "Bo"

    def test_no_prefix_returned_as_is(self, monkeypatch):
        monkeypatch.setattr(ze.calendar_engine, "get_parasha", lambda d: "Special Reading")
        assert ze._get_weekly_shabbat_parasha(date(2026, 1, 12)) == "Special Reading"

    def test_empty_result_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(ze.calendar_engine, "get_parasha", lambda d: "")
        assert ze._get_weekly_shabbat_parasha(date(2026, 1, 12)) == ""

    def test_exception_returns_empty_string(self, monkeypatch):
        def _raise(d):
            raise RuntimeError("boom")
        monkeypatch.setattr(ze.calendar_engine, "get_parasha", _raise)
        assert ze._get_weekly_shabbat_parasha(date(2026, 1, 12)) == ""


class TestGetOmerInfo:
    def test_during_omer_season_returns_day_info(self):
        # 20 Nissan is within the 49-day Omer count starting 16 Nissan.
        result = ze._get_omer_info(date(2026, 4, 7))
        if result is not None:
            assert 1 <= result["day"] <= 49
            assert "Day" in result["label"]

    def test_outside_omer_season_returns_none(self):
        result = ze._get_omer_info(date(2026, 10, 1))
        assert result is None

    def test_exception_returns_none(self, monkeypatch):
        class BadDate:
            year = "not-an-int"
            month = 1
            day = 1
        result = ze._get_omer_info(BadDate())
        assert result is None


class TestGetCommunityZmanimBranches:
    def test_bukharian_community_offsets_sunset_display(self, mock_outbound_http):
        result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="bukharian")
        assert "error" not in result
        assert "(-20m)" in result["zmanim"]["Sunset"]

    def test_standard_community_no_offset_label(self, mock_outbound_http):
        result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        assert "(-20m)" not in result["zmanim"]["Sunset"]

    def test_invalid_coordinates_return_error_shape(self, mock_outbound_http):
        result = ze.get_community_zmanim("not-a-number", "not-a-number", NYC_TZ)
        assert "error" in result

    def test_shabbat_date_shows_havdalah(self, mock_outbound_http):
        result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        assert "zmanim" in result


class TestGetMonthlyEventsBranches:
    def test_havdalah_emoji_applied(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": [{"category": "havdalah", "title": "Havdalah", "date": "2026-01-17T18:00:00-05:00"}]},
            status=200,
        )
        events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
        assert any("🌙" in e["title"] for e in events)

    def test_fast_day_emoji_applied(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": [{"category": "fast", "title": "Fast of Gedaliah", "date": "2026-09-14"}]},
            status=200,
        )
        events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
        assert any("⏳" in e["title"] for e in events)

    def test_rosh_chodesh_emoji_applied(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": [{"category": "roshchodesh", "title": "Rosh Chodesh Shevat", "date": "2026-01-19"}]},
            status=200,
        )
        events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
        assert any("🌙" in e["title"] for e in events)

    def test_major_holiday_emoji_applied(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": [{"category": "major", "title": "Rosh Hashana", "date": "2026-09-12"}]},
            status=200,
        )
        events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
        assert any("✡️" in e["title"] for e in events)

    def test_hebcal_exception_does_not_raise_and_still_returns_list(self, monkeypatch):
        # Daily sunrise/sunset events are computed independently of Hebcal, so
        # the list isn't empty — this exercises the exception-swallowing path
        # (the [Hebcal Error] print) without the holiday-derived events.
        def _raise(*a, **k):
            raise ConnectionError("hebcal down")
        monkeypatch.setattr(ze._HTTP, "get", _raise)
        events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
        assert isinstance(events, list)
        assert not any("🌙" in e["title"] or "✡️" in e["title"] for e in events)


# ─────────────── Circuit-breaker hardening on Hebcal network calls ────────────
#
# backend/health_check.py has always registered 'hebcal' as an actively-probed
# circuit-breaker service, but until now no call site in this module actually
# consulted is_healthy()/recorded success or failure against it (a gap flagged
# in claude_code_prompts.md's Prompt 3 status row, closed under Prompt 17 item
# 1). The `_reset_api_health` autouse fixture in conftest.py resets the shared
# `backend.health_check.health` singleton around every test.


class TestHebcalDayTimesCircuitBreaker:
    """_get_hebcal_day_times() -- used by get_community_zmanim() for
    candle-lighting/havdalah enrichment."""

    def test_skips_call_when_circuit_open(self, mock_outbound_http):
        for _ in range(FAIL_THRESHOLD):
            ze.health.record_failure("hebcal")

        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": [{"date": "2026-01-02T17:00:00-05:00", "category": "candles"}]},
                status=200,
            )
            result = ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2))
            assert result == {"candles": None, "havdalah": None}
            assert len(rsps.calls) == 0

    def test_upstream_failure_opens_circuit_after_threshold(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"), status=500)
            for i in range(FAIL_THRESHOLD):
                # Distinct dates so each call gets its own cache key and
                # actually re-hits the network instead of short-circuiting
                # on the first failed lookup's cached result.
                ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 2 + i))

        assert ze.health.is_healthy("hebcal") is False

    def test_success_records_health_success(self, mock_outbound_http):
        ze.health.record_failure("hebcal")
        ze.health.record_failure("hebcal")

        ze._get_hebcal_day_times(NYC_LAT, NYC_LON, NYC_TZ, date(2026, 1, 5))

        assert ze.health._circuits["hebcal"].failures == 0


class TestGetMonthlyEventsCircuitBreaker:
    """get_monthly_events()'s Hebcal-holidays block (solar events are
    computed independently and are unaffected by circuit state)."""

    def test_skips_call_when_circuit_open(self, mock_outbound_http):
        for _ in range(FAIL_THRESHOLD):
            ze.health.record_failure("hebcal")

        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": [{"category": "major", "title": "Should Not Be Reached", "date": "2026-09-12"}]},
                status=200,
            )
            events = ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)
            assert not any("Should Not Be Reached" in e["title"] for e in events)
            assert len(rsps.calls) == 0

    def test_upstream_failure_opens_circuit_after_threshold(self, monkeypatch):
        def _raise(*a, **k):
            raise ConnectionError("hebcal down")
        monkeypatch.setattr(ze._HTTP, "get", _raise)

        for _ in range(FAIL_THRESHOLD):
            # get_monthly_events() unconditionally caches its return value
            # (solar events survive a Hebcal failure), so without clearing
            # between calls the second+ call would short-circuit on that
            # cache and never re-attempt the Hebcal fetch at all.
            ze._HEBCAL_MONTH_CACHE.clear()
            ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)

        assert ze.health.is_healthy("hebcal") is False

    def test_success_records_health_success(self, mock_outbound_http):
        ze.health.record_failure("hebcal")
        ze.health.record_failure("hebcal")

        ze.get_monthly_events(NYC_LAT, NYC_LON, NYC_TZ)

        assert ze.health._circuits["hebcal"].failures == 0
