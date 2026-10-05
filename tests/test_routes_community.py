"""
Tests for backend/routes_community.py routes.

Covers:
  - GET /api/communities/list           → 200 with JSON list of communities
  - GET /api/community/<valid-name>     → 200 with customs dict
  - GET /api/community/<invalid-name>   → 404 or error
  - GET /api/community/<name>/timeline  → 200 with events list

Note: The community routes read from local JSON files in customs/ — no Supabase
call is made here, so no auth header is needed. The task description referenced
/api/community/customs which does not exist; actual routes are listed above.
"""

from __future__ import annotations

import json

import pytest

from backend import routes_community
from backend.helpers import COMMUNITIES

_REAL_COMMUNITIES = [name for name in COMMUNITIES if name != "Israeli"]


class TestCommunitiesList:
    def test_list_returns_200(self, test_client):
        response = test_client.get("/api/communities/list")
        assert response.status_code == 200

    def test_list_returns_json_list(self, test_client):
        response = test_client.get("/api/communities/list")
        body = response.get_json()
        assert isinstance(body, list)

    def test_list_contains_name_key(self, test_client):
        response = test_client.get("/api/communities/list")
        body = response.get_json()
        assert len(body) > 0
        assert all("name" in item for item in body)


class TestCommunityDetail:
    @pytest.mark.parametrize("name", ["Ashkenaz", "Sefardic", "Yemenite"])
    def test_known_community_returns_200(self, test_client, name):
        response = test_client.get(f"/api/community/{name}")
        assert response.status_code == 200

    def test_community_body_has_customs_key(self, test_client):
        response = test_client.get("/api/community/Ashkenaz")
        body = response.get_json()
        assert isinstance(body, dict)
        assert "customs" in body or "error" in body

    def test_community_body_has_name(self, test_client):
        response = test_client.get("/api/community/Sefardic")
        body = response.get_json()
        if response.status_code == 200:
            assert "name" in body

    def test_unknown_community_returns_404(self, test_client):
        response = test_client.get("/api/community/FakeNonExistentCommunity12345")
        assert response.status_code == 404

    def test_community_missing_param_returns_404_or_405(self, test_client):
        """Route requires a name in the path; bare /api/community returns 404 or 405."""
        response = test_client.get("/api/community/")
        assert response.status_code in (404, 405)


class TestCommunityTimeline:
    def test_timeline_known_community_200(self, test_client):
        response = test_client.get("/api/community/Ashkenaz/timeline")
        assert response.status_code == 200

    def test_timeline_body_has_events(self, test_client):
        response = test_client.get("/api/community/Ashkenaz/timeline")
        body = response.get_json()
        assert isinstance(body, dict)
        assert "events" in body

    def test_timeline_events_is_list(self, test_client):
        response = test_client.get("/api/community/Ashkenaz/timeline")
        body = response.get_json()
        if "events" in body:
            assert isinstance(body["events"], list)

    def test_timeline_unknown_community_returns_404(self, test_client):
        response = test_client.get("/api/community/FakeNonExistentCommunity/timeline")
        assert response.status_code == 404



class TestCommunityDetailForTheResearchedData:
    """What the browser receives from the format-3.0 files."""

    @pytest.fixture
    def bodies(self, test_client):
        return {name: test_client.get(f"/api/community/{name}").get_json() for name in _REAL_COMMUNITIES}

    def test_all_thirteen_communities_load(self, bodies):
        assert len(bodies) == 13
        assert all("error" not in body for body in bodies.values()), [n for n, b in bodies.items() if "error" in b]

    def test_no_reviewer_only_text_is_sent_to_the_browser(self, bodies):
        for name, body in bodies.items():
            blob = json.dumps(body, ensure_ascii=False)
            assert '"review_notes"' not in blob, name
            assert '"needs_rabbinic_review"' not in blob, name

    def test_the_stripped_text_really_exists_in_the_files(self):
        """Guards the test above: if no file carried review_notes it would pass for nothing."""
        raw = routes_community._PROJECT_ROOT + "/customs/moroccan.json"
        data = json.load(open(raw, encoding="utf-8"))
        assert any("review_notes" in item for item in data["halacha_index"])
        assert data["needs_rabbinic_review"]

    def test_major_authorities_come_from_the_core_authorities(self, bodies):
        for name, body in bodies.items():
            assert body["major_authorities"], name
            assert len(body["major_authorities"]) <= 6, name

    def test_entries_carry_confidence_variants_and_references(self, bodies):
        entries = list(bodies["Moroccan"]["customs"].values())
        assert {"category", "topic", "ruling", "common_practices", "variants", "confidence",
                "source", "source_url", "references"} <= set(entries[0])
        assert {e["confidence"] for e in entries} <= {"well-attested", "disputed", "regional", "needs-review", ""}
        assert any(e["variants"] for e in entries)
        assert any(e["references"] for e in entries)

    def test_every_entry_and_distinctive_custom_carries_its_hebrew(self, bodies):
        for name, body in bodies.items():
            for entry in body["customs"].values():
                assert entry["topic_he"] and entry["ruling_he"], (name, entry["topic"])
            for custom in body["distinctive_customs"]:
                assert custom["name_he"] and custom["description_he"], (name, custom["name"])

    def test_hebrew_helper_returns_blank_for_a_missing_or_non_dict_entry(self):
        assert routes_community._hebrew({"topic": "x"}, "topic") == ""
        assert routes_community._hebrew({"topic_he": " ש "}, "topic") == "ש"
        assert routes_community._hebrew("not a dict", "topic") == ""

    def test_entries_keep_the_datas_capitalisation_for_display(self, bodies):
        entries = bodies["Moroccan"]["customs"]
        assert all(key == key.lower() for key in entries), "keys stay lowercase for stable lookup"
        topics = {e["topic"] for e in entries.values()}
        categories = {e["category"] for e in entries.values()}
        assert any(t != t.lower() for t in topics), "topics are no longer lowercased"
        assert "Brit milah and pidyon haben" in categories

    def test_distinctive_customs_disputes_and_gaps_are_top_level(self, bodies):
        for name, body in bodies.items():
            assert body["distinctive_customs"], name
            assert all(c["name"] and c["description"] for c in body["distinctive_customs"]), name
            assert body["gaps"], name
            assert all(isinstance(g, str) and g for g in body["gaps"]), name
            assert isinstance(body["disputes"], list), name

    def test_raw_data_halacha_index_is_still_there_for_the_customs_modal(self, bodies):
        for name, body in bodies.items():
            assert body["raw_data"]["halacha_index"], name
            assert body["raw_data"]["runtime"]["lens_key"] == name


class TestPublicCommunityDataHelpers:
    def test_reviewer_fields_are_removed_without_touching_the_input(self):
        data = {
            "name": "X", "needs_rabbinic_review": ["q"],
            "halacha_index": [{"topic": "t", "review_notes": "r", "summary": "s"}, "odd"],
            "unique_minhagim": [{"name": "n", "review_notes": "r"}],
        }
        public = routes_community._public_community_data(data)
        assert public == {"name": "X", "halacha_index": [{"topic": "t", "summary": "s"}, "odd"],
                          "unique_minhagim": [{"name": "n"}]}
        assert data["halacha_index"][0]["review_notes"] == "r"

    def test_other_shapes_pass_through(self):
        assert routes_community._public_community_data([1]) == [1]
        legacy = {"name": "X", "unique_minhagim": {"examples": ["a"]}}
        assert routes_community._public_community_data(legacy) == legacy

    def test_distinctive_customs_skip_unnamed_and_malformed_items(self):
        data = {"unique_minhagim": [
            {"name": " Mimouna ", "description": "d", "when": "w", "source": "s", "source_url": "u", "confidence": "c"},
            {"name": "", "description": "unnamed"}, "text", {"description": "no name"},
        ]}
        assert routes_community._distinctive_customs(data) == [
            {"name": "Mimouna", "name_he": "", "description": "d", "description_he": "", "when": "w",
             "source": "s", "source_url": "u", "confidence": "c"}]

    def test_distinctive_customs_carry_their_hebrew_when_the_data_has_it(self):
        data = {"unique_minhagim": [{"name": "Mimouna", "name_he": " מימונה ", "description": "d",
                                     "description_he": "תיאור"}]}
        entry = routes_community._distinctive_customs(data)[0]
        assert (entry["name_he"], entry["description_he"]) == ("מימונה", "תיאור")

    def test_distinctive_customs_of_the_legacy_dict_shape_is_empty(self):
        assert routes_community._distinctive_customs({"unique_minhagim": {"examples": ["a"]}}) == []
        assert routes_community._distinctive_customs({}) == []

    def test_text_list_drops_blanks_and_non_lists(self):
        assert routes_community._text_list([" a ", "", None, 3]) == ["a", "3"]
        assert routes_community._text_list("not a list") == []
        assert routes_community._text_list(None) == []
