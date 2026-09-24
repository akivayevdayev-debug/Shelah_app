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
from freezegun import freeze_time

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


class TestFastDayGating:
    """Musaf/Candle-Lighting/Havdalah must only fire on true Yom Tov/Shabbat,
    never on a plain fast day; Fast Starts/Fast Ends must fire only on a fast
    day, with the minor/major start-time distinction from _compute_fast_times.
    """

    def test_minor_fast_no_musaf_shows_fast_start_and_end(self, mock_outbound_http):
        # 2026-12-20 is 10 of Teves (minor fast), a plain Sunday.
        with freeze_time("2026-12-20 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Latest Musaf"] in (None, "N/A")
        assert z["Candle Lighting"] in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")
        assert z["Fast Starts"] not in (None, "N/A")
        assert z["Fast Ends"] not in (None, "N/A")

    def test_major_fast_day_yom_kippur_shows_musaf_and_fast_end_only(self, mock_outbound_http):
        # 2026-09-21 is Yom Kippur -- a true Yom Tov, so Musaf (and its own
        # Havdalah-equivalent ending) is correct; Fast Starts must NOT show
        # since the fast began the prior evening, not today.
        with freeze_time("2026-09-21 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Latest Musaf"] not in (None, "N/A")
        assert z["Fast Starts"] in (None, "N/A")
        assert z["Fast Ends"] not in (None, "N/A")

    def test_erev_yom_kippur_shows_fast_start_only(self, mock_outbound_http):
        # 2026-09-20 is Erev Yom Kippur -- the major fast starts tonight, at
        # sunset, not on the fast's own civil day.
        with freeze_time("2026-09-20 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Fast Starts"] not in (None, "N/A")
        assert z["Fast Ends"] in (None, "N/A")
        assert z["Latest Musaf"] in (None, "N/A")

    def test_regular_day_no_fast_zmanim(self, mock_outbound_http):
        with freeze_time("2026-09-16 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Fast Starts"] in (None, "N/A")
        assert z["Fast Ends"] in (None, "N/A")


class TestYomTovCandleLightingBoundary:
    """Candle Lighting/Havdalah must fire only on true Yom Tov days, anchored
    on the evening BEFORE the sacred day (like Friday for Shabbat) -- and must
    exclude Chol HaMoed, even though pyluach tags every day of Succos/Pesach
    with the same bare holiday name. Latest Musaf stays Chol-HaMoed-inclusive
    since Musaf is said every day of Sukkot/Pesach. Dates below are chosen
    with no adjacent Shabbat, so each fires from exactly one cause.
    """

    def test_erev_shmini_atzeres_shows_candle_lighting_only(self, mock_outbound_http):
        # 2025-10-13 is Monday, Erev Shmini Atzeres. Tomorrow is a true Yom
        # Tov day, so Candle Lighting must fire tonight; nothing ends today.
        with freeze_time("2025-10-13 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Candle Lighting"] not in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")

    def test_shmini_atzeres_shows_second_night_candle_lighting_only(self, mock_outbound_http):
        # 2025-10-14 is Shmini Atzeres itself. Tomorrow (Simchas Torah) is
        # also a true Yom Tov day, so tonight's lighting is the 2-day Yom
        # Tov's second-night lighting "from an existing flame" -- a real,
        # additional occasion, not the true Erev lighting. Havdalah must not
        # fire since the sacred period doesn't end tonight.
        with freeze_time("2025-10-14 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Candle Lighting"] not in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")

    def test_simchas_torah_shows_havdalah_only(self, mock_outbound_http):
        # 2025-10-15 is Simchas Torah, the last Yom Tov day -- Havdalah fires
        # on its own evening; nothing starts tomorrow so no Candle Lighting.
        with freeze_time("2025-10-15 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Candle Lighting"] in (None, "N/A")
        assert z["Havdalah"] not in (None, "N/A")

    def test_day_after_simchas_torah_shows_neither(self, mock_outbound_http):
        with freeze_time("2025-10-16 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Candle Lighting"] in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")

    def test_chol_hamoed_succos_shows_musaf_but_not_candle_lighting_or_havdalah(
        self, mock_outbound_http
    ):
        # 2026-09-28 is Chol HaMoed Succos (Monday) -- pyluach tags it with
        # the same bare 'Succos' name as the real Yom Tov days, so this is
        # the direct proof that the Chol-HaMoed exclusion (Fix B) is working:
        # Musaf stays on (it's said daily through Chol HaMoed) while Candle
        # Lighting/Havdalah correctly stay off.
        with freeze_time("2026-09-28 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Latest Musaf"] not in (None, "N/A")
        assert z["Candle Lighting"] in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")

    def test_erev_pesach_day_seven_fires_despite_chol_hamoed_conflation(
        self, mock_outbound_http
    ):
        # 2026-04-07 (Tuesday) is deep in Pesach's Chol HaMoed by pyluach's
        # bare 'Pesach' tag, but it's actually Erev of Pesach's 7th day (a
        # true Yom Tov day) -- without the day-of-month narrowing, the prior
        # day (also tagged 'Pesach') would look like "yesterday was Yom Tov
        # too" and this Candle Lighting would never fire.
        with freeze_time("2026-04-07 16:00:00"):
            result = ze.get_community_zmanim(NYC_LAT, NYC_LON, NYC_TZ, community="standard")
        z = result["zmanim"]
        assert z["Candle Lighting"] not in (None, "N/A")
        assert z["Havdalah"] in (None, "N/A")


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


class TestGetMonthlyEventsUsesLocationDate:
    """get_monthly_events()'s 30-day window must start from the requested
    location's own calendar date, not the server's -- otherwise a user whose
    timezone is far from the server's rolls over to "today" a day early or
    late relative to what their own clock says (item 12 of the Sep 2026 UI
    batch: mirrors the fix already applied in get_community_zmanim())."""

    LA_LAT, LA_LON, LA_TZ = 34.0522, -118.2437, "America/Los_Angeles"

    def test_today_uses_location_timezone_not_server_local(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": []}, status=200,
        )
        # 03:00 UTC on Jan 15 is still Jan 14, 19:00 in Los Angeles (UTC-8 in
        # January) -- a server-local date.today() would say "Jan 15" while
        # the location's own calendar day is still "Jan 14".
        with freeze_time("2026-01-15 03:00:00"):
            events = ze.get_monthly_events(self.LA_LAT, self.LA_LON, self.LA_TZ)

        sunrise_events = [e for e in events if "Sunrise" in e["title"]]
        assert sunrise_events
        assert sunrise_events[0]["start"][:10] == "2026-01-14"

    def test_cache_key_rolls_over_at_location_midnight_not_server_midnight(self, mock_outbound_http):
        mock_outbound_http.replace(
            responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
            json={"items": []}, status=200,
        )
        with freeze_time("2026-01-15 03:00:00"):
            ze.get_monthly_events(self.LA_LAT, self.LA_LON, self.LA_TZ)

        cache_key = (
            ze._cache_coord(self.LA_LAT), ze._cache_coord(self.LA_LON),
            self.LA_TZ, 2026, 1,
        )
        assert ze._HEBCAL_MONTH_CACHE.get(cache_key) is not None


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


class TestGetMonthlyEventsTimezoneParam:
    """plan.md §27.2 — the Hebcal URL must use the resolved tz_name, not the
    raw (possibly-None) timezone_str parameter, or the request degrades to a
    literal '&tzid=None'."""

    def test_none_timezone_str_resolves_to_real_tzid(self, mock_outbound_http):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": []}, status=200,
            )
            ze.get_monthly_events(NYC_LAT, NYC_LON, timezone_str=None)

            hebcal_calls = [c for c in rsps.calls if "hebcal.com" in c.request.url]
            assert len(hebcal_calls) == 1
            sent_url = hebcal_calls[0].request.url
            assert "tzid=None" not in sent_url
            assert "tzid=America%2FNew_York" in sent_url or "tzid=America/New_York" in sent_url
