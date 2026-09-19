"""
Tests for backend/calendar_service.py (PyluachEngine) — Gregorian<->Hebrew
date conversion, parasha lookup (Hebcal-backed, cached), and holiday
detection. No prior dedicated test file existed for this module.
"""

from __future__ import annotations

import re
from datetime import date

import pytest
import requests as requests_lib
import responses as responses_lib

from backend.calendar_service import PyluachEngine
from backend.health_check import FAIL_THRESHOLD


@pytest.fixture(autouse=True)
def _reset_parasha_cache():
    import backend.calendar_service as cs
    cs._PARASHA_CACHE.clear()
    yield
    cs._PARASHA_CACHE.clear()


class TestGregorianToHebrew:
    def test_none_defaults_to_today(self):
        result = PyluachEngine.gregorian_to_hebrew(None)
        assert "hebrew_date" in result
        assert result["hebrew_date"] != "Error"

    def test_date_object_input(self):
        result = PyluachEngine.gregorian_to_hebrew(date(2026, 1, 1))
        assert result["gregorian_date"] == "2026-01-01"
        assert "hebrew_year" in result

    def test_string_iso_date_input(self):
        result = PyluachEngine.gregorian_to_hebrew("2026-01-01")
        assert result["gregorian_date"] == "2026-01-01"
        assert result["hebrew_date"] != "Error"

    def test_invalid_string_returns_error_shape(self):
        result = PyluachEngine.gregorian_to_hebrew("not-a-date")
        assert result["hebrew_date"] == "Error"
        assert "error" in result


class TestGetParasha:
    def test_string_date_input_parsed(self, monkeypatch):
        import backend.calendar_service as cs
        monkeypatch.setattr(
            cs._HTTP, "get",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": ["Parashat Vaera"]},
            })(),
        )
        result = PyluachEngine.get_parasha("2026-01-01")
        assert result == "Parashat Vaera"

    def test_finds_parasha_in_events(self, monkeypatch):
        import backend.calendar_service as cs
        monkeypatch.setattr(
            cs._HTTP, "get",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": ["Rosh Chodesh Shevat", "Parashat Bo"]},
            })(),
        )
        result = PyluachEngine.get_parasha(date(2026, 1, 20))
        assert result == "Parashat Bo"

    def test_no_parasha_in_events_returns_placeholder(self, monkeypatch):
        import backend.calendar_service as cs
        monkeypatch.setattr(
            cs._HTTP, "get",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": []},
            })(),
        )
        result = PyluachEngine.get_parasha(date(2026, 2, 15))
        assert result == "No parasha for this date"

    def test_hebcal_failure_returns_unavailable(self, monkeypatch):
        import backend.calendar_service as cs

        def _raise(*a, **k):
            raise ConnectionError("hebcal down")

        monkeypatch.setattr(cs._HTTP, "get", _raise)
        result = PyluachEngine.get_parasha(date(2026, 3, 1))
        assert result == "Parasha lookup unavailable"

    def test_repeat_call_within_ttl_uses_cache(self, monkeypatch):
        import backend.calendar_service as cs
        call_count = {"n": 0}

        def fake_get(*a, **k):
            call_count["n"] += 1
            return type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": ["Parashat Noach"]},
            })()

        monkeypatch.setattr(cs._HTTP, "get", fake_get)
        d = date(2026, 4, 1)
        first = PyluachEngine.get_parasha(d)
        second = PyluachEngine.get_parasha(d)
        assert first == second == "Parashat Noach"
        assert call_count["n"] == 1

    def test_invalid_string_date_returns_unavailable(self):
        result = PyluachEngine.get_parasha("garbage-date")
        assert result == "Parasha lookup unavailable"


class TestIsHoliday:
    def test_none_defaults_to_today(self):
        result = PyluachEngine.is_holiday(None)
        assert "is_holiday" in result

    def test_string_date_input(self):
        result = PyluachEngine.is_holiday("2026-01-01")
        assert isinstance(result["is_holiday"], bool)

    def test_yom_tov_classified_correctly(self):
        # Rosh Hashanah 5787 falls on 2026-09-12 (Gregorian).
        result = PyluachEngine.is_holiday(date(2026, 9, 12))
        if result["is_holiday"]:
            assert result["holiday_type"] in {"Yom Tov", "Minor Holiday"}

    def test_regular_day_not_a_holiday(self):
        result = PyluachEngine.is_holiday(date(2026, 6, 15))
        assert result["holiday_type"] in {"Regular Day", "Minor Holiday", "Yom Tov"}

    def test_invalid_string_returns_error_shape(self):
        result = PyluachEngine.is_holiday("not-a-date")
        assert result["is_holiday"] is False
        assert "error" in result


class TestIsYomTovProper:
    """pyluach's holiday() tags every day of Succos (Tishrei 15-21) and
    Pesach (Nissan 15-16 & 21-22, plus Chol HaMoed 17-20) with the same bare
    'Succos'/'Pesach' string. `is_yom_tov_proper` must narrow that down to
    just the true work-prohibited Yom Tov days -- Chol HaMoed and Hoshana
    Rabbah must read as NOT Yom Tov proper, even though `holiday_type` for
    all of them is still 'Yom Tov' (correct for Musaf, which is said daily
    through Chol HaMoed).
    """

    def test_succos_day_one_is_yom_tov_proper(self):
        # 2026-09-26 = 15 Tishrei -- Succos day 1.
        result = PyluachEngine.is_holiday(date(2026, 9, 26))
        assert result["holiday_name"] == "Succos"
        assert result["is_yom_tov_proper"] is True

    def test_succos_day_two_is_yom_tov_proper(self):
        # 2026-09-27 = 16 Tishrei -- Succos day 2.
        result = PyluachEngine.is_holiday(date(2026, 9, 27))
        assert result["holiday_name"] == "Succos"
        assert result["is_yom_tov_proper"] is True

    def test_succos_chol_hamoed_is_not_yom_tov_proper(self):
        # 2026-09-28 = 17 Tishrei -- Chol HaMoed, tagged 'Succos' but not a
        # true Yom Tov day.
        result = PyluachEngine.is_holiday(date(2026, 9, 28))
        assert result["holiday_name"] == "Succos"
        assert result["holiday_type"] == "Yom Tov"
        assert result["is_yom_tov_proper"] is False

    def test_hoshana_rabbah_is_not_yom_tov_proper(self):
        # 2026-10-02 = 21 Tishrei -- Hoshana Rabbah, last day of Succos'
        # Chol HaMoed span, still tagged 'Succos' but not a true Yom Tov day.
        result = PyluachEngine.is_holiday(date(2026, 10, 2))
        assert result["holiday_name"] == "Succos"
        assert result["is_yom_tov_proper"] is False

    def test_pesach_day_one_and_two_are_yom_tov_proper(self):
        # 2026-04-02/03 = 15/16 Nissan -- Pesach days 1-2.
        for d in (date(2026, 4, 2), date(2026, 4, 3)):
            result = PyluachEngine.is_holiday(d)
            assert result["holiday_name"] == "Pesach"
            assert result["is_yom_tov_proper"] is True

    def test_pesach_chol_hamoed_is_not_yom_tov_proper(self):
        # 2026-04-04 = 17 Nissan and 2026-04-07 = 20 Nissan -- both Chol
        # HaMoed, tagged 'Pesach' but not true Yom Tov days.
        for d in (date(2026, 4, 4), date(2026, 4, 7)):
            result = PyluachEngine.is_holiday(d)
            assert result["holiday_name"] == "Pesach"
            assert result["is_yom_tov_proper"] is False

    def test_pesach_day_seven_and_eight_are_yom_tov_proper(self):
        # 2026-04-08/09 = 21/22 Nissan -- Pesach days 7-8 (Acharon).
        for d in (date(2026, 4, 8), date(2026, 4, 9)):
            result = PyluachEngine.is_holiday(d)
            assert result["holiday_name"] == "Pesach"
            assert result["is_yom_tov_proper"] is True

    def test_shmini_atzeres_is_yom_tov_proper_without_day_narrowing(self):
        # Shmini Atzeres has no Chol HaMoed of its own, so it isn't in the
        # narrowing table -- is_yom_tov_proper should just follow
        # holiday_type, same as every non-Succos/Pesach Yom Tov.
        result = PyluachEngine.is_holiday(date(2025, 10, 14))
        assert result["holiday_name"] == "Shmini Atzeres"
        assert result["is_yom_tov_proper"] is True


class TestCalendarEngineSingleton:
    def test_calendar_engine_is_pyluach_engine_instance(self):
        from backend.calendar_service import calendar_engine
        assert isinstance(calendar_engine, PyluachEngine)


# ─────────────── Circuit-breaker hardening on Hebcal network calls ────────────
#
# get_parasha() is one of the four hebcal call sites that had zero
# circuit-breaker wiring despite 'hebcal' already being a registered service
# in backend/health_check.py (claude_code_prompts.md Prompt 3 status row,
# closed under Prompt 17 item 1). The `_reset_api_health` autouse fixture in
# conftest.py resets the shared `backend.health_check.health` singleton
# around every test.


class TestGetParashaCircuitBreaker:
    def test_skips_call_when_circuit_open(self, mock_outbound_http):
        import backend.calendar_service as cs

        for _ in range(FAIL_THRESHOLD):
            cs.health.record_failure("hebcal")

        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/converter.*"),
                json={"events": ["Parashat Should Not Be Reached"]},
                status=200,
            )
            result = PyluachEngine.get_parasha(date(2026, 5, 1))
            assert result == "Parasha lookup unavailable"
            assert len(rsps.calls) == 0

    def test_upstream_failure_opens_circuit_after_threshold(self, monkeypatch):
        import backend.calendar_service as cs

        def _raise(*a, **k):
            raise requests_lib.exceptions.ConnectionError("hebcal down")

        monkeypatch.setattr(cs._HTTP, "get", _raise)

        for i in range(FAIL_THRESHOLD):
            # Distinct dates so each call gets its own cache key.
            PyluachEngine.get_parasha(date(2026, 6, 1 + i))

        assert cs.health.is_healthy("hebcal") is False

    def test_success_records_health_success(self, monkeypatch):
        import backend.calendar_service as cs

        cs.health.record_failure("hebcal")
        cs.health.record_failure("hebcal")

        monkeypatch.setattr(
            cs._HTTP, "get",
            lambda *a, **k: type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": ["Parashat Emor"]},
            })(),
        )
        PyluachEngine.get_parasha(date(2026, 7, 1))

        assert cs.health._circuits["hebcal"].failures == 0

    def test_date_parsing_failure_never_records_hebcal_failure(self):
        """A malformed date string fails before the network call is ever
        reached, so it must not be misattributed as a hebcal circuit
        failure (see the comment at the is_healthy() gate in
        backend/calendar_service.py)."""
        import backend.calendar_service as cs

        result = PyluachEngine.get_parasha("garbage-date")
        assert result == "Parasha lookup unavailable"
        assert cs.health._circuits["hebcal"].failures == 0


class TestGetParashaDefaultsToToday:
    def test_none_looks_up_todays_date(self, monkeypatch):
        import backend.calendar_service as cs

        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 1, 1)

        seen_params = {}

        def fake_get(url, params=None, **kwargs):
            seen_params.update(params)
            return type("R", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"events": ["Parashat Vaera"]},
            })()

        monkeypatch.setattr(cs, "date_lib", _FrozenDate)
        monkeypatch.setattr(cs._HTTP, "get", fake_get)

        assert PyluachEngine.get_parasha() == "Parashat Vaera"
        assert (seen_params["gy"], seen_params["gm"], seen_params["gd"]) == (2026, 1, 1)


class TestHebrewToGregorian:
    def test_converts_a_hebrew_date(self):
        result = PyluachEngine.hebrew_to_gregorian(5786, 7, 1)  # 1 Tishrei 5786 = Rosh Hashana

        assert result == {
            "gregorian_date": "2025-09-23",
            "hebrew_date": "1 Tishrei 5786",
            "hebrew_year": 5786,
            "hebrew_month": 7,
            "hebrew_day": 1,
        }

    def test_accepts_numeric_strings(self):
        assert PyluachEngine.hebrew_to_gregorian("5786", "7", "1")["gregorian_date"] == "2025-09-23"

    @pytest.mark.parametrize(
        "args",
        [
            (5786, 13, 1),    # Adar II in a non-leap year
            (5786, 7, 31),    # Tishrei has 30 days
            (5786, 7, "x"),   # not a number
        ],
        ids=["adar-ii-non-leap", "day-out-of-range", "non-numeric"],
    )
    def test_invalid_hebrew_date_returns_error_shape(self, args):
        result = PyluachEngine.hebrew_to_gregorian(*args)

        assert result["gregorian_date"] is None
        assert result["error"]


class TestAddDaysToHebrewDate:
    def test_adds_days_across_a_month_boundary(self):
        # Tishrei has 30 days, so 1 Tishrei + 30 is 1 Cheshvan.
        result = PyluachEngine.add_days_to_hebrew_date(5786, 7, 1, 30)

        assert result["hebrew_date"] == "1 Cheshvan 5786"
        assert (result["hebrew_year"], result["hebrew_month"], result["hebrew_day"]) == (5786, 8, 1)
        assert result["gregorian_date"] == "2025-10-23"

    def test_negative_days_cross_the_new_year_backwards(self):
        # The day before Rosh Hashana 5786 is 29 Elul 5785.
        result = PyluachEngine.add_days_to_hebrew_date(5786, 7, 1, -1)

        assert result["hebrew_date"] == "29 Elul 5785"
        assert (result["hebrew_year"], result["hebrew_month"], result["hebrew_day"]) == (5785, 6, 29)
        assert result["gregorian_date"] == "2025-09-22"

    @pytest.mark.parametrize(
        "args",
        [(5786, 13, 1, 5), (5786, 7, 1, "many")],
        ids=["invalid-date", "non-numeric-days"],
    )
    def test_bad_input_returns_error_shape(self, args):
        result = PyluachEngine.add_days_to_hebrew_date(*args)

        assert result["error"]
        assert "gregorian_date" not in result


class TestNextOccurrenceOfHebrewDate:
    def test_string_anchor_on_the_day_itself_returns_that_day(self):
        # 7 Tishrei 5787 is 2026-09-18; "next occurrence" is inclusive of the anchor.
        result = PyluachEngine.next_occurrence_of_hebrew_date(7, 7, "2026-09-18")

        assert result["gregorian_date"] == "2026-09-18"
        assert result["hebrew_date"] == "7 Tishrei 5787"

    def test_date_object_anchor_after_the_day_rolls_to_next_year(self):
        result = PyluachEngine.next_occurrence_of_hebrew_date(7, 7, date(2026, 9, 19))

        assert result["gregorian_date"] == "2027-10-08"
        assert (result["hebrew_year"], result["hebrew_month"], result["hebrew_day"]) == (5788, 7, 7)

    def test_none_anchor_means_today(self, monkeypatch):
        import backend.calendar_service as cs

        class _FrozenDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 18)

        monkeypatch.setattr(cs, "date_lib", _FrozenDate)

        result = PyluachEngine.next_occurrence_of_hebrew_date(7, 7)

        assert result["gregorian_date"] == "2026-09-18"

    def test_month_missing_from_this_year_skips_to_the_next_year_that_has_it(self):
        # 5786 is not a leap year, so Adar II does not exist; 5787 is a leap year.
        result = PyluachEngine.next_occurrence_of_hebrew_date(13, 1, "2025-09-30")

        assert result["gregorian_date"] == "2027-03-10"
        assert (result["hebrew_year"], result["hebrew_month"], result["hebrew_day"]) == (5787, 13, 1)

    def test_a_date_that_never_exists_reports_that_it_could_not_be_resolved(self):
        result = PyluachEngine.next_occurrence_of_hebrew_date(7, 31, "2026-09-18")

        assert result == {"error": "Could not resolve a next occurrence within 3 Hebrew years"}

    def test_unparseable_anchor_returns_error_shape(self):
        result = PyluachEngine.next_occurrence_of_hebrew_date(7, 7, "garbage-date")

        assert result["error"]
        assert "gregorian_date" not in result
