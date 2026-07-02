"""
Supplementary coverage tests for backend/routes_calendar.py, covering routes
and fallback branches tests/test_routes_calendar.py doesn't reach: set_location,
/api/daily-study, and the Hebcal-failure/Sefaria-failure fallback chains for
/api/holidays and /api/parasha.
"""

from __future__ import annotations

import re

import responses as responses_lib


class TestSetLocation:
    def test_valid_coordinates_sets_session(self, test_client):
        response = test_client.post("/set_location", json={"lat": 40.7, "lon": -74.0})
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "success"
        assert body["lat"] == 40.7

    def test_invalid_coordinates_returns_400(self, test_client):
        response = test_client.post("/set_location", json={"lat": 999, "lon": -74.0})
        assert response.status_code == 400

    def test_missing_coordinates_returns_400(self, test_client):
        response = test_client.post("/set_location", json={})
        assert response.status_code == 400

    def test_non_numeric_coordinates_returns_400(self, test_client):
        response = test_client.post("/set_location", json={"lat": "not-a-number", "lon": -74.0})
        assert response.status_code == 400


class TestDailyStudy:
    def test_daily_study_returns_200(self, test_client):
        response = test_client.get("/api/daily-study")
        assert response.status_code == 200
        assert isinstance(response.get_json(), dict)


class TestHolidaysFallbackChain:
    def test_hebcal_failure_falls_back_to_pyluach(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("hebcal down")

        monkeypatch.setattr(requests, "get", _raise)
        monkeypatch.setattr(
            routes_calendar_module, "_build_pyluach_holiday_events",
            lambda year: [{"title": "Rosh Hashanah", "start": "2026-09-12"}],
        )
        response = test_client.get("/api/holidays")
        assert response.status_code == 200
        body = response.get_json()
        assert body == [{"title": "Rosh Hashanah", "start": "2026-09-12"}]

    def test_hebcal_and_pyluach_fail_falls_back_to_monthly_zmanim(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("hebcal down")

        monkeypatch.setattr(requests, "get", _raise)
        monkeypatch.setattr(routes_calendar_module, "_build_pyluach_holiday_events", lambda year: [])
        monkeypatch.setattr(
            routes_calendar_module, "get_engine",
            lambda: type("FakeEngine", (), {"get_monthly_zmanim": lambda self: [{"title": "Zmanim Event"}]})(),
        )
        response = test_client.get("/api/holidays")
        assert response.status_code == 200
        assert response.get_json() == [{"title": "Zmanim Event"}]

    def test_all_fallbacks_fail_returns_503(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("hebcal down")

        monkeypatch.setattr(requests, "get", _raise)
        monkeypatch.setattr(routes_calendar_module, "_build_pyluach_holiday_events", lambda year: [])

        def _raise_engine():
            raise RuntimeError("engine unavailable")

        monkeypatch.setattr(routes_calendar_module, "get_engine", _raise_engine)
        response = test_client.get("/api/holidays")
        assert response.status_code == 503
        assert "error" in response.get_json()

    def test_hebcal_error_field_in_response_triggers_fallback(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module
        monkeypatch.setattr(
            routes_calendar_module, "_build_pyluach_holiday_events",
            lambda year: [{"title": "Fallback Event", "start": "2026-01-01"}],
        )
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"error": "invalid year"}, status=200,
            )
            response = test_client.get("/api/holidays?year=9999")
            assert response.status_code == 200
            assert response.get_json() == [{"title": "Fallback Event", "start": "2026-01-01"}]

    def test_non_dict_items_and_missing_title_are_skipped(self, test_client):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": [
                    "not a dict",
                    {"title": "", "date": "2026-01-01"},
                    {"category": "holiday", "title": "Purim", "date": "2026-03-03"},
                ]},
                status=200,
            )
            response = test_client.get("/api/holidays")
            assert response.status_code == 200
            body = response.get_json()
            assert len(body) == 1
            assert "Purim" in body[0]["title"]


class TestParashaFallbackChain:
    def test_sefaria_failure_falls_back_to_calendar_service(self, test_client, monkeypatch):
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("sefaria down")

        monkeypatch.setattr(requests, "get", _raise)

        import backend.calendar_service as calendar_service_module
        monkeypatch.setattr(calendar_service_module.calendar_engine, "get_parasha", lambda: "Vaera")

        response = test_client.get("/api/parasha")
        assert response.status_code == 200
        body = response.get_json()
        assert body["title"] == "Vaera"
        assert body["source"] == "calendar-fallback"

    def test_sefaria_and_calendar_service_fail_returns_default(self, test_client, monkeypatch):
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("sefaria down")

        monkeypatch.setattr(requests, "get", _raise)

        import backend.calendar_service as calendar_service_module

        def _raise_parasha():
            raise RuntimeError("calendar engine down")

        monkeypatch.setattr(calendar_service_module.calendar_engine, "get_parasha", _raise_parasha)

        response = test_client.get("/api/parasha")
        assert response.status_code == 200
        body = response.get_json()
        assert body["source"] == "default-fallback"

    def test_sefaria_response_without_parasha_item_falls_through(self, test_client, monkeypatch):
        import backend.calendar_service as calendar_service_module
        monkeypatch.setattr(calendar_service_module.calendar_engine, "get_parasha", lambda: "Bo")
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.sefaria\.org/api/calendars.*"),
                json={"calendar_items": [{"title": {"en": "Daf Yomi"}, "displayValue": {"en": "Berakhot 2a"}}]},
                status=200,
            )
            response = test_client.get("/api/parasha")
            assert response.status_code == 200
            assert response.get_json()["source"] == "calendar-fallback"
