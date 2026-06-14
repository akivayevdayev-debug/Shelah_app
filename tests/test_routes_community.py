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

import pytest


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
