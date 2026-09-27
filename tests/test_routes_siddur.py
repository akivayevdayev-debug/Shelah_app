"""Siddur v2 API (backend/routes_siddur.py, backend/siddur_data.py): the
table of contents, whole services, the day's guidance, and their cache and
rate-limit classes."""

import pytest

from backend import cache_policy, rate_limit, siddur_data


class TestToc:
    def test_toc_lists_occasions_services_and_sections(self, test_client):
        response = test_client.get("/api/siddur/v2/toc/edot-hamizrach")
        assert response.status_code == 200
        toc = response.get_json()
        assert [o["slug"] for o in toc["occasions"]] == ["weekday", "shabbat", "festivals", "berachot"]
        shacharit = toc["occasions"][0]["services"][1]
        assert shacharit["slug"] == "shacharit" and len(shacharit["sections"]) == 18
        assert toc["source"]["he"]["license"] == "CC0"
        # Hour-long tier: the toc carries the data version (see cache_policy).
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_CORPUS

    @pytest.mark.parametrize("rite", ["ashkenaz", "Edot-HaMizrach", "..", "edot_hamizrach"])
    def test_unknown_rite_is_404_and_never_publicly_cached(self, test_client, rite):
        response = test_client.get(f"/api/siddur/v2/toc/{rite}")
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_PRIVATE


class TestService:
    def test_service_returns_typed_lines_for_every_section(self, test_client):
        response = test_client.get("/api/siddur/v2/service/edot-hamizrach/arbit?v=anything")
        assert response.status_code == 200
        payload = response.get_json()
        assert [s["slug"] for s in payload["sections"]] == ["barchu", "keriat-shema", "amidah", "alenu"]
        amidah = payload["sections"][2]
        assert amidah["ref"] == "Siddur Edot HaMizrach, Weekday Arvit, Amidah"
        barech = [line for line in amidah["lines"] if line.get("when") == ["barech-alenu"]]
        assert len(barech) == 1 and barech[0]["t"] == "instruction"
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_IMMUTABLE

    @pytest.mark.parametrize("path", [
        "/api/siddur/v2/service/edot-hamizrach/no-such-service",
        "/api/siddur/v2/service/edot-hamizrach/Arbit",
        "/api/siddur/v2/service/nope/arbit",
    ])
    def test_unknown_service_is_404(self, test_client, path):
        response = test_client.get(path)
        assert response.status_code == 404
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_PRIVATE


class TestDay:
    def test_day_guidance(self, test_client):
        response = test_client.get("/api/siddur/v2/day?date=2026-12-06&il=0")
        assert response.status_code == 200
        day = response.get_json()
        assert day["date"] == "2026-12-06" and day["il"] is False
        assert "chanukah" in day["occasions"] and day["alHanissim"] is True
        assert day["hallel"] == {"kind": "full", "beracha": True}
        assert day["birkatHashanim"] == "barech-alenu"
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_IMMUTABLE

    def test_israel_flag_changes_the_answer(self, test_client):
        # 7 Cheshvan 5787: Barech Alenu starts in Israel, not yet outside it.
        outside = test_client.get("/api/siddur/v2/day?date=2026-10-18&il=0").get_json()
        israel = test_client.get("/api/siddur/v2/day?date=2026-10-18&il=1").get_json()
        assert (outside["birkatHashanim"], israel["birkatHashanim"]) == ("barchenu", "barech-alenu")

    def test_il_defaults_to_diaspora(self, test_client):
        assert test_client.get("/api/siddur/v2/day?date=2026-10-18").get_json()["il"] is False

    @pytest.mark.parametrize("query", [
        "", "date=2026-13-01", "date=tomorrow", "date=2026-10-18&il=yes", "date=1899-12-31", "date=2100-01-01",
    ])
    def test_bad_parameters_are_400(self, test_client, query):
        response = test_client.get(f"/api/siddur/v2/day?{query}")
        assert response.status_code == 400
        assert "error" in response.get_json()
        assert response.headers["Cache-Control"] == cache_policy.CACHE_TIER_PRIVATE


class TestClassification:
    @pytest.mark.parametrize("path", [
        "/api/siddur/v2/toc/edot-hamizrach",
        "/api/siddur/v2/service/edot-hamizrach/shacharit",
        "/api/siddur/v2/day",
    ])
    def test_siddur_v2_is_classified_cheap(self, path):
        assert rate_limit.classify_route(path) == "cheap"


class TestSiddurData:
    def test_find_multi_and_single_section_services(self):
        service, section = siddur_data.find("edot-hamizrach", "shacharit")
        assert service["slug"] == "shacharit" and section is None
        service, section = siddur_data.find("edot-hamizrach", "shacharit", "amida")
        assert section["ref"] == "Siddur Edot HaMizrach, Weekday Shacharit, Amida"
        service, section = siddur_data.find("edot-hamizrach", "birkat-hamazon")
        assert section["slug"] == "birkat-hamazon"

    @pytest.mark.parametrize("args", [
        ("edot-hamizrach", "shacharit", "nope"),
        ("edot-hamizrach", "birkat-hamazon", "birkat-hamazon"),  # a lone section has no URL of its own
        ("edot-hamizrach", "nope"),
        ("nope", "shacharit"),
    ])
    def test_find_misses(self, args):
        assert siddur_data.find(*args) is None

    def test_find_by_ref_accepts_a_segment_ref(self):
        service, section = siddur_data.find_by_ref("Siddur Edot HaMizrach, Weekday Mincha, Amida 12")
        assert (service["slug"], section["slug"]) == ("mincha", "amida")
        assert siddur_data.find_by_ref("Siddur Edot HaMizrach, Weekday Mincha, Amidah") is None

    @pytest.mark.parametrize("query, expected", [
        ("Shacharit", ("shacharit", None)),
        ("ערבית לימי החול", ("arbit", None)),
        ("birkat hamazon", ("birkat-hamazon", None)),
        ("Grace after meals", ("birkat-hamazon", None)),
        ("weekday evening", ("arbit", None)),
        ("Blessing of Children", ("leil-shabbat", "blessing-of-children")),
        # The name the query covers most closely wins: Shacharit's Shema,
        # not the bedtime Shema, whose name only contains the word.
        ("Shema", ("shacharit", "keriat-shema")),
        ("Bedtime Shema", ("keriat-shema-al-hamita", None)),
        ("Amida of Mincha", ("mincha", "amida")),
        ("weekday arvit", ("arbit", None)),
        ("Ma'ariv", ("arbit", None)),
        ("Shemoneh Esrei", ("shacharit", "amida")),
        ("Havdalah", ("motzaei-shabbat", "havdala")),
        ("Aleinu", ("shacharit", "alenu")),
    ])
    def test_search_services(self, query, expected):
        service, section = siddur_data.search_services(query)
        assert (service["slug"], section["slug"] if section else None) == expected

    def test_search_misses(self):
        assert siddur_data.search_services("") is None
        assert siddur_data.search_services("zzzz qqqq") is None
        assert siddur_data.search_services("the") is None  # only stopwords
        # A service name that is a small part of the query isn't a match.
        assert siddur_data.search_services("Kaddish after Mincha") is None

    def test_paths(self):
        assert siddur_data.siddur_path("edot-hamizrach") == "/siddur/edot-hamizrach"
        assert siddur_data.siddur_path("edot-hamizrach", "shacharit", "amida") == "/siddur/edot-hamizrach/shacharit/amida"

    def test_bad_slugs_never_touch_the_filesystem(self):
        assert siddur_data.get_toc("../etc") is None
        assert siddur_data.get_service("edot-hamizrach", "../toc") is None
