"""
Tests for backend/calendar_service.py (PyluachEngine) — Gregorian<->Hebrew
date conversion, parasha lookup (Hebcal-backed, cached), and holiday
detection. No prior dedicated test file existed for this module.
"""

from __future__ import annotations

from datetime import date

import pytest
import responses as responses_lib

from backend.calendar_service import PyluachEngine


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


class TestCalendarEngineSingleton:
    def test_calendar_engine_is_pyluach_engine_instance(self):
        from backend.calendar_service import calendar_engine
        assert isinstance(calendar_engine, PyluachEngine)
