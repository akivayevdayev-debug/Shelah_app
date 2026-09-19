"""
Supplementary coverage tests for backend/routes_calendar.py, covering routes
and fallback branches tests/test_routes_calendar.py doesn't reach: set_location,
/api/daily-study, and the Hebcal-failure/Sefaria-failure fallback chains for
/api/holidays and /api/parasha.
"""

from __future__ import annotations

import re

import pytest
import responses as responses_lib

from backend.health_check import FAIL_THRESHOLD


_SAME_ORIGIN_HEADERS = {"Origin": "http://localhost"}


class TestSetLocation:
    """Security audit P3: /set_location now requires a same-origin
    Origin/Referer, matching the guard already used by
    routes_devtools.py's /api/client-errors."""

    def test_valid_coordinates_sets_session(self, test_client):
        response = test_client.post(
            "/set_location", json={"lat": 40.7, "lon": -74.0}, headers=_SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "success"
        assert body["lat"] == 40.7

    def test_valid_coordinates_mark_session_permanent(self, test_client):
        """plan.md §46 / Prompt 58: session.permanent = True is now set at
        this write site directly (not a blanket before_request hook), so
        the 30-day persistent cookie behavior must still hold here."""
        test_client.post(
            "/set_location", json={"lat": 40.7, "lon": -74.0}, headers=_SAME_ORIGIN_HEADERS,
        )
        with test_client.session_transaction() as sess:
            assert sess.permanent is True

    def test_invalid_coordinates_returns_400(self, test_client):
        response = test_client.post(
            "/set_location", json={"lat": 999, "lon": -74.0}, headers=_SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 400

    def test_missing_coordinates_returns_400(self, test_client):
        response = test_client.post("/set_location", json={}, headers=_SAME_ORIGIN_HEADERS)
        assert response.status_code == 400

    def test_non_numeric_coordinates_returns_400(self, test_client):
        response = test_client.post(
            "/set_location", json={"lat": "not-a-number", "lon": -74.0}, headers=_SAME_ORIGIN_HEADERS,
        )
        assert response.status_code == 400

    def test_cross_origin_request_is_rejected(self, test_client):
        response = test_client.post(
            "/set_location",
            json={"lat": 40.7, "lon": -74.0},
            headers={"Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403

    def test_request_with_no_origin_or_referer_is_rejected(self, test_client):
        response = test_client.post("/set_location", json={"lat": 40.7, "lon": -74.0})
        assert response.status_code == 403


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

    def test_year_query_param_is_coerced_not_interpolated_raw(self, test_client):
        # plan.md §8.C.5 security-audit pass: `year` used to be dropped
        # straight into the outbound Hebcal URL unvalidated, so a value like
        # "2026&geo=pos&latitude=1" injected extra query params onto the
        # real request. Assert the actual outbound URL only ever carries a
        # plain bounded integer.
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": []}, status=200,
            )
            response = test_client.get(
                "/api/holidays?year=2026%26geo=pos%26latitude=1")
            assert response.status_code == 200
            sent_url = rsps.calls[0].request.url
            assert "geo=pos" not in sent_url
            assert "latitude=1" not in sent_url
            assert "year=2026" in sent_url

    def test_out_of_range_year_falls_back_to_current_year(self, test_client):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": []}, status=200,
            )
            response = test_client.get("/api/holidays?year=99999999")
            assert response.status_code == 200
            sent_url = rsps.calls[0].request.url
            assert "year=99999999" not in sent_url


class TestParashaFallbackChain:
    """GET /api/parasha (routes_calendar.py) routes its Sefaria fetch through
    backend.sefaria_library._cached_get(), a memory+disk TTLCache shared by
    every Sefaria fetch in the codebase (added because the parasha only
    changes weekly but this endpoint used to hit Sefaria's calendars API on
    every request, ~1.8s each -- see the comment at routes_calendar.py's
    get_parasha()). That's a deliberate, correct perf win, not a regression:
    but it means a cache hit -- the in-process memory tier (a module-level
    singleton other tests/files can warm before this class ever runs) or the
    on-disk tier (.sefaria_cache/, gitignored, persists real Sefaria
    responses across whole dev sessions) -- returns real cached data and
    short-circuits _cached_get() *before* it ever attempts the network call
    these tests fail/mock. That masks the very fallback path under test:
    every request_mock/monkeypatch below is a no-op against a warm cache.
    Give each test here a private, empty memory cache and a disabled disk
    tier so the simulated Sefaria failure always actually reaches the
    network call.
    """

    @pytest.fixture(autouse=True)
    def _isolate_sefaria_cache(self, monkeypatch):
        import backend.sefaria_library as sefaria_library_module
        from backend.cache import TTLCache

        monkeypatch.setattr(
            sefaria_library_module, "_cache",
            TTLCache(maxsize=2048, ttl=3600, redis_prefix="sefaria_http:"),
        )
        monkeypatch.setattr(sefaria_library_module, "_disk_cache_get", lambda url: None)
        monkeypatch.setattr(sefaria_library_module, "_disk_cache_set", lambda url, data: None)

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


# ─────────────── Circuit-breaker hardening on Hebcal network calls ────────────
#
# /api/holidays is one of the four hebcal call sites that had zero
# circuit-breaker wiring despite 'hebcal' already being a registered service
# in backend/health_check.py (claude_code_prompts.md Prompt 3 status row,
# closed under Prompt 17 item 1). Mirrors the 'nominatim' circuit-breaker
# tests below for _fetch_geocode_results/geocode_city in this same file. The
# `_reset_api_health` autouse fixture in conftest.py resets the shared
# `backend.health_check.health` singleton around every test.

class TestHolidaysCircuitBreaker:
    def test_skips_call_when_circuit_open(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module

        for _ in range(FAIL_THRESHOLD):
            routes_calendar_module.health.record_failure("hebcal")

        monkeypatch.setattr(
            routes_calendar_module, "_build_pyluach_holiday_events",
            lambda year: [{"title": "Pyluach Fallback", "start": "2026-01-01"}],
        )
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": [{"title": "Should Not Be Reached", "date": "2026-01-01"}]},
                status=200,
            )
            response = test_client.get("/api/holidays")
            assert response.status_code == 200
            assert response.get_json() == [{"title": "Pyluach Fallback", "start": "2026-01-01"}]
            assert len(rsps.calls) == 0

    def test_upstream_failure_opens_circuit_after_threshold(self, test_client, monkeypatch):
        import backend.routes_calendar as routes_calendar_module
        import requests

        def _raise(*a, **k):
            raise requests.RequestException("hebcal down")

        monkeypatch.setattr(requests, "get", _raise)
        monkeypatch.setattr(routes_calendar_module, "_build_pyluach_holiday_events", lambda year: [])
        monkeypatch.setattr(
            routes_calendar_module, "get_engine",
            lambda: type("FakeEngine", (), {"get_monthly_zmanim": lambda self: []})(),
        )

        for _ in range(FAIL_THRESHOLD):
            test_client.get("/api/holidays")

        assert routes_calendar_module.health.is_healthy("hebcal") is False

    def test_success_records_health_success(self, test_client):
        import backend.routes_calendar as routes_calendar_module

        routes_calendar_module.health.record_failure("hebcal")
        routes_calendar_module.health.record_failure("hebcal")

        response = test_client.get("/api/holidays")
        assert response.status_code == 200

        assert routes_calendar_module.health._circuits["hebcal"].failures == 0


class TestZmanimMonthCoordinates:
    """GET /api/zmanim/month shares _resolve_zmanim_query_coords() with
    /api/zmanim; these cover the month route's own branches."""

    @pytest.mark.parametrize(
        "query_string",
        ["?lat=999&lon=35", "?lat=abc&lon=35", "?lat=31.7&lon=999", "?lat=31.7&lon=north"],
    )
    def test_invalid_coordinates_return_400(self, test_client, query_string):
        response = test_client.get(f"/api/zmanim/month{query_string}")

        assert response.status_code == 400
        assert "Invalid coordinates" in response.get_json()["error"]

    def test_without_coordinates_uses_the_default_engine_and_is_never_publicly_cached(
        self, test_client
    ):
        """No lat/lon means the response depends on the caller's remembered
        location (get_engine() reads the session), so a CDN must not cache it."""
        from backend.cache_policy import CACHE_TIER_PRIVATE

        response = test_client.get("/api/zmanim/month")

        assert response.status_code == 200
        assert isinstance(response.get_json(), list)
        assert response.headers["Cache-Control"] == CACHE_TIER_PRIVATE

    def test_with_coordinates_the_response_is_not_forced_private(self, test_client):
        from backend.cache_policy import CACHE_TIER_PRIVATE

        response = test_client.get("/api/zmanim/month?lat=40.7&lon=-74.0")

        assert response.status_code == 200
        assert response.headers["Cache-Control"] != CACHE_TIER_PRIVATE


class TestHolidaysUnexpectedHebcalShapes:
    @pytest.mark.parametrize("items", [None, 5, "unexpected"], ids=["null", "number", "string"])
    def test_items_that_is_not_a_list_yields_no_events(self, test_client, items):
        """A well-formed 200 whose `items` is not a list is an empty calendar,
        not an upstream failure: it must not fall through to the pyluach /
        monthly-zmanim fallback chain (which would return other events)."""
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses_lib.GET, re.compile(r"https://www\.hebcal\.com/.*"),
                json={"items": items}, status=200,
            )
            response = test_client.get("/api/holidays")

        assert response.status_code == 200
        assert response.get_json() == []


class TestParashaSefariaHelperRaises:
    def test_an_exception_from_the_sefaria_helper_falls_back_to_the_calendar_engine(
        self, test_client, monkeypatch
    ):
        import backend.calendar_service as calendar_service_module
        import backend.sefaria_library as sefaria_library_module

        def _boom(*args, **kwargs):
            raise RuntimeError("cache layer exploded")

        monkeypatch.setattr(sefaria_library_module, "_cached_get", _boom)
        monkeypatch.setattr(calendar_service_module.calendar_engine, "get_parasha", lambda: "Parashat Bo")

        response = test_client.get("/api/parasha")

        assert response.status_code == 200
        assert response.get_json()["title"] == "Parashat Bo"
        assert response.get_json()["source"] == "calendar-fallback"
