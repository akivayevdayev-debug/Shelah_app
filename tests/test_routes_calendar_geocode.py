"""
Tests for /api/geocode in backend/routes_calendar.py: the server-side Nominatim
proxy behind the zmanim "search by city" box (_fetch_geocode_results and
geocode_city).

The `_reset_api_health` autouse fixture in conftest.py resets the shared
`backend.health_check.health` singleton around every test, so the circuit-breaker
assertions here start from a closed circuit.
"""

from __future__ import annotations

import pytest
import requests
import responses as responses_lib

from backend.health_check import FAIL_THRESHOLD

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


@pytest.fixture
def routes_calendar_module():
    import backend.routes_calendar as module

    return module


class TestGeocodeQueryValidation:
    @pytest.mark.parametrize("query_string", ["", "?q=", "?q=%20%20%09"])
    def test_missing_or_blank_query_returns_400(self, test_client, query_string):
        with responses_lib.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            response = test_client.get(f"/api/geocode{query_string}")
            assert len(rsps.calls) == 0

        assert response.status_code == 400
        assert "'q'" in response.get_json()["error"]


class TestGeocodeUpstreamFailures:
    @responses_lib.activate
    def test_open_circuit_returns_503_without_calling_nominatim(self, test_client, routes_calendar_module):
        for _ in range(FAIL_THRESHOLD):
            routes_calendar_module.health.record_failure("nominatim")
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=[], status=200)

        response = test_client.get("/api/geocode?q=Jerusalem")

        assert response.status_code == 503
        assert response.get_json() == {"error": "City search is temporarily unavailable."}
        assert len(responses_lib.calls) == 0

    @responses_lib.activate
    def test_non_ok_upstream_status_returns_502_and_records_failure(self, test_client, routes_calendar_module):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json={"error": "rate limited"}, status=429)

        response = test_client.get("/api/geocode?q=Jerusalem")

        assert response.status_code == 502
        assert response.get_json() == {"error": "City search failed."}
        assert routes_calendar_module.health._circuits["nominatim"].failures == 1

    @responses_lib.activate
    def test_network_error_returns_502_and_records_failure(self, test_client, routes_calendar_module):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, body=requests.ConnectionError("dns down"))

        response = test_client.get("/api/geocode?q=Jerusalem")

        assert response.status_code == 502
        assert response.get_json() == {"error": "City search failed."}
        assert routes_calendar_module.health._circuits["nominatim"].failures == 1

    @responses_lib.activate
    def test_non_json_body_is_treated_as_upstream_failure(self, test_client, routes_calendar_module):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, body="<html>maintenance</html>", status=200)

        response = test_client.get("/api/geocode?q=Jerusalem")

        assert response.status_code == 502
        assert routes_calendar_module.health._circuits["nominatim"].failures == 1

    @responses_lib.activate
    def test_repeated_failures_open_the_circuit(self, test_client, routes_calendar_module):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json={}, status=500)

        for _ in range(FAIL_THRESHOLD):
            assert test_client.get("/api/geocode?q=Jerusalem").status_code == 502
        calls_before_open = len(responses_lib.calls)

        assert routes_calendar_module.health.is_healthy("nominatim") is False
        assert test_client.get("/api/geocode?q=Jerusalem").status_code == 503
        assert len(responses_lib.calls) == calls_before_open == FAIL_THRESHOLD


class TestGeocodeSuccess:
    @responses_lib.activate
    def test_returns_top_result_as_floats(self, test_client):
        responses_lib.add(
            responses_lib.GET,
            NOMINATIM_URL,
            json=[
                {"lat": "31.7683", "lon": "35.2137", "display_name": "Jerusalem, Israel"},
                {"lat": "1", "lon": "2", "display_name": "Ignored second hit"},
            ],
        )

        response = test_client.get("/api/geocode?q=Jerusalem")

        assert response.status_code == 200
        assert response.get_json() == {
            "results": [{"lat": 31.7683, "lon": 35.2137, "display_name": "Jerusalem, Israel"}]
        }

    @responses_lib.activate
    def test_sends_identifying_user_agent_and_the_stripped_query(self, test_client):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=[])

        test_client.get("/api/geocode?q=%20%20Tel%20Aviv%20")

        request = responses_lib.calls[0].request
        assert request.headers["User-Agent"].startswith("ShelahApp/")
        assert request.headers["Accept-Language"] == "en"
        assert request.params["q"] == "Tel Aviv"
        assert request.params["format"] == "json"
        assert request.params["limit"] == "1"

    @responses_lib.activate
    def test_lang_he_requests_hebrew_results_from_nominatim(self, test_client):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=[])

        test_client.get("/api/geocode?q=Jerusalem&lang=he")

        request = responses_lib.calls[0].request
        assert request.headers["Accept-Language"] == "he"

    @responses_lib.activate
    @pytest.mark.parametrize("lang", ["", "fr", "HE", "he "])
    def test_unrecognized_lang_values_fall_back_to_english(self, test_client, lang):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=[])

        test_client.get(f"/api/geocode?q=Jerusalem&lang={lang}")

        request = responses_lib.calls[0].request
        assert request.headers["Accept-Language"] == "en"

    @responses_lib.activate
    def test_success_closes_a_partially_failed_circuit(self, test_client, routes_calendar_module):
        routes_calendar_module.health.record_failure("nominatim")
        routes_calendar_module.health.record_failure("nominatim")
        responses_lib.add(
            responses_lib.GET, NOMINATIM_URL, json=[{"lat": "1", "lon": "2", "display_name": "X"}],
        )

        assert test_client.get("/api/geocode?q=X").status_code == 200

        assert routes_calendar_module.health._circuits["nominatim"].failures == 0

    @responses_lib.activate
    def test_missing_display_name_falls_back_to_the_query(self, test_client):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=[{"lat": 10, "lon": 20}])

        response = test_client.get("/api/geocode?q=Somewhere")

        assert response.get_json()["results"] == [{"lat": 10.0, "lon": 20.0, "display_name": "Somewhere"}]


class TestGeocodeUnusableResults:
    @pytest.mark.parametrize(
        "payload",
        [
            [],
            {"unexpected": "object"},
            ["not-a-dict"],
            [{"lat": "north", "lon": "35.0"}],
            [{"lat": "31.7", "lon": None}],
            [{"display_name": "no coordinates"}],
        ],
        ids=["empty-list", "dict-payload", "non-dict-hit", "non-numeric-lat", "null-lon", "no-coordinates"],
    )
    @responses_lib.activate
    def test_returns_empty_results_instead_of_an_error(self, test_client, payload):
        responses_lib.add(responses_lib.GET, NOMINATIM_URL, json=payload)

        response = test_client.get("/api/geocode?q=Nowhere")

        assert response.status_code == 200
        assert response.get_json() == {"results": []}
