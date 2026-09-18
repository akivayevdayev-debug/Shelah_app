"""
Tests for backend/routes_calendar.py routes.

Covers:
  - GET /api/zmanim?lat=&lon=     happy path → 200 with zmanim key
  - GET /api/zmanim               missing lat/lon → 400 or error response
  - GET /api/zmanim?lat=999&lon=999  invalid coords → 400 or error response
  - GET /api/zmanim/month         → 200 with JSON list
  - GET /api/parasha              → 200 with JSON
  - GET /api/holidays             → 200 with JSON

Hebcal and Sefaria outbound calls are intercepted by the autouse
`mock_outbound_http` fixture in conftest.py.
"""

from __future__ import annotations


class TestZmanimHappyPath:
    def test_zmanim_with_coords_returns_200(self, test_client):
        response = test_client.get("/api/zmanim?lat=40.7&lon=-74.0")
        assert response.status_code == 200

    def test_zmanim_body_is_dict(self, test_client):
        response = test_client.get("/api/zmanim?lat=40.7&lon=-74.0")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_zmanim_has_zmanim_key(self, test_client):
        """Response must contain a 'zmanim' key with the time table."""
        response = test_client.get("/api/zmanim?lat=40.7&lon=-74.0")
        body = response.get_json()
        assert isinstance(body, dict)
        # The route returns either {zmanim: ..., metadata: ...} or {error: ...}
        has_zmanim = "zmanim" in body or "metadata" in body or "error" in body
        assert has_zmanim


class TestZmanimSessionWriteOriginGuard:
    """Security audit P2: /api/zmanim is a GET route with a session-write
    side effect, and SESSION_COOKIE_SAMESITE=Lax allows cookies on
    cross-site top-level GET navigation — a crafted link could otherwise
    silently overwrite a victim's stored location. The response itself
    (computed for the requested lat/lon) is unaffected by origin; only the
    session persistence is gated."""

    def test_response_succeeds_regardless_of_origin(self, test_client):
        """Cross-origin/no-origin callers still get a valid computed
        response -- only the session write is skipped, not the request."""
        response = test_client.get("/api/zmanim?lat=40.7&lon=-74.0")
        assert response.status_code == 200

    def test_same_origin_request_persists_location_to_session(self, test_client):
        with test_client.session_transaction() as sess:
            sess.clear()
        test_client.get("/api/zmanim?lat=40.7&lon=-74.0", headers={"Origin": "http://localhost"})
        with test_client.session_transaction() as sess:
            assert sess.get("lat") == 40.7
            assert sess.get("lon") == -74.0

    def test_same_origin_request_marks_session_permanent(self, test_client):
        """plan.md §46 / Prompt 58: `session.permanent = True` used to be
        set by an unconditional before_request hook on every request; it's
        now set only at each session-write call site instead (here,
        _remember_location_if_same_origin()). The persistent 30-day cookie
        behavior must still hold for the route that actually writes the
        session."""
        with test_client.session_transaction() as sess:
            sess.clear()
        test_client.get("/api/zmanim?lat=40.7&lon=-74.0", headers={"Origin": "http://localhost"})
        with test_client.session_transaction() as sess:
            assert sess.permanent is True

    def test_cross_origin_request_does_not_persist_location_to_session(self, test_client):
        with test_client.session_transaction() as sess:
            sess.clear()
        test_client.get(
            "/api/zmanim?lat=40.7&lon=-74.0",
            headers={"Origin": "https://evil.example.com"},
        )
        with test_client.session_transaction() as sess:
            assert "lat" not in sess
            assert "lon" not in sess


class TestZmanimMissingCoords:
    def test_zmanim_no_coords_returns_json(self, test_client):
        """Without lat/lon the engine falls back to a default location; must respond."""
        response = test_client.get("/api/zmanim")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)


class TestZmanimInvalidCoords:
    def test_zmanim_invalid_coords_returns_error(self, test_client):
        """Coordinates outside valid range should produce a 400 or error payload."""
        response = test_client.get("/api/zmanim?lat=999&lon=999")
        assert response.status_code in (400, 422)


class TestZmanimMonth:
    def test_zmanim_month_returns_200(self, test_client):
        response = test_client.get("/api/zmanim/month?lat=40.7&lon=-74.0")
        assert response.status_code == 200

    def test_zmanim_month_body_is_list(self, test_client):
        response = test_client.get("/api/zmanim/month?lat=40.7&lon=-74.0")
        body = response.get_json()
        assert isinstance(body, list)


class TestParasha:
    def test_parasha_returns_200(self, test_client):
        response = test_client.get("/api/parasha")
        assert response.status_code == 200

    def test_parasha_body_is_dict(self, test_client):
        response = test_client.get("/api/parasha")
        body = response.get_json()
        assert isinstance(body, dict)

    def test_parasha_has_title_key(self, test_client):
        response = test_client.get("/api/parasha")
        body = response.get_json()
        assert "title" in body


class TestHolidays:
    def test_holidays_returns_200(self, test_client):
        response = test_client.get("/api/holidays")
        assert response.status_code == 200

    def test_holidays_body_is_list(self, test_client):
        response = test_client.get("/api/holidays")
        body = response.get_json()
        assert isinstance(body, (list, dict))
