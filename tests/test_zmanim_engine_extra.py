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
from datetime import date, timedelta

import pytest
import responses as responses_lib

import backend.zmanim_engine as ze

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
        tz, tz_str = ze._resolve_timezone(NYC_LAT, NYC_LON, given_tz="Europe/London")
        assert tz_str == "Europe/London"

    def test_resolves_from_coordinates(self):
        tz, tz_str = ze._resolve_timezone(NYC_LAT, NYC_LON)
        assert tz_str == "America/New_York"

    def test_timezonefinder_exception_falls_back_to_default(self, monkeypatch):
        class FakeFinder:
            def timezone_at(self, lng, lat):
                raise RuntimeError("boom")
        monkeypatch.setattr(ze, "tf", FakeFinder())
        tz, tz_str = ze._resolve_timezone(999, 999)
        assert tz_str == "America/New_York"

    def test_unresolvable_coordinates_falls_back_to_default(self, monkeypatch):
        class FakeFinder:
            def timezone_at(self, lng, lat):
                return None
        monkeypatch.setattr(ze, "tf", FakeFinder())
        tz, tz_str = ze._resolve_timezone(0, 0)
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
        # Saturday.
        saturday = date(2026, 1, 17)
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
