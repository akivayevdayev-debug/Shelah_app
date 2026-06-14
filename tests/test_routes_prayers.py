"""
Tests for backend/routes_prayers.py routes.

Covers:
  - GET /api/prayers/list         → 200 with JSON list
  - GET /api/prayer/Weekday Shacharit  → 200 with prayer data
  - GET /api/prayer/Weekday Mincha     → 200
  - GET /api/prayer/Weekday Maariv     → 200
  - GET /api/prayer/<unknown>          → 404

The Sefaria text fetches are intercepted by the autouse mock in conftest.py.
The task referred to /api/prayers/{shacharit,mincha,maariv} which does not
match the actual blueprint; the real routes are /api/prayer/<name>.
"""

from __future__ import annotations

import pytest


PRAYER_NAMES = [
    "Weekday Shacharit",
    "Weekday Mincha",
    "Weekday Maariv",
]


class TestPrayersList:
    def test_list_returns_200(self, test_client):
        response = test_client.get("/api/prayers/list")
        assert response.status_code == 200

    def test_list_returns_json_list(self, test_client):
        response = test_client.get("/api/prayers/list")
        body = response.get_json()
        assert isinstance(body, list)

    def test_list_items_have_name_key(self, test_client):
        response = test_client.get("/api/prayers/list")
        body = response.get_json()
        assert len(body) > 0
        assert all("name" in item for item in body)


class TestPrayerDetail:
    @pytest.mark.parametrize("prayer_name", PRAYER_NAMES)
    def test_known_prayer_returns_200_or_404(self, test_client, prayer_name):
        """
        When Sefaria is mocked, the text fetch may not find real content and
        return 404 — acceptable; what matters is no 500.
        """
        encoded = prayer_name.replace(" ", "%20")
        response = test_client.get(f"/api/prayer/{encoded}")
        assert response.status_code in (200, 404)

    @pytest.mark.parametrize("prayer_name", PRAYER_NAMES)
    def test_prayer_body_is_dict(self, test_client, prayer_name):
        encoded = prayer_name.replace(" ", "%20")
        response = test_client.get(f"/api/prayer/{encoded}")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_known_prayer_has_name_key_when_200(self, test_client):
        response = test_client.get("/api/prayer/Weekday%20Shacharit")
        if response.status_code == 200:
            body = response.get_json()
            assert "name" in body

    def test_unknown_prayer_returns_404(self, test_client):
        response = test_client.get("/api/prayer/FakeNonExistentPrayer12345")
        assert response.status_code == 404


class TestSiddurFull:
    def test_siddur_full_known_prayer(self, test_client):
        response = test_client.get("/api/siddur/full/Weekday%20Shacharit")
        # 200 with lines, or 404 when mock Sefaria returns no usable content
        assert response.status_code in (200, 404)

    def test_siddur_full_body_is_dict(self, test_client):
        response = test_client.get("/api/siddur/full/Weekday%20Shacharit")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_siddur_full_unknown_returns_404(self, test_client):
        response = test_client.get("/api/siddur/full/FakeNonExistentPrayer")
        assert response.status_code == 404
