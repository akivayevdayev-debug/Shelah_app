"""
Supplementary coverage tests for the three smallest-gap blueprints:
backend/routes_community.py, backend/routes_prayers.py, backend/routes_devtools.py.
Targets branches the existing dedicated test files don't reach: file-read
exceptions, timeline-building loops, backward-compat aliases, prayer preview
fallback text, and the segment-report POST route.
"""

from __future__ import annotations

import builtins

import pytest


class TestCommunityApiAlias:
    def test_communities_alias_delegates_to_list(self, test_client):
        response = test_client.get("/api/communities")
        assert response.status_code == 200
        assert isinstance(response.get_json(), list)


class TestCommunityDetailExceptionPath:
    def test_file_read_failure_returns_500(self, test_client, monkeypatch):
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                raise OSError("disk read error")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Ashkenaz")
        assert response.status_code == 500
        assert "error" in response.get_json()

    def test_timeline_file_read_failure_returns_500(self, test_client, monkeypatch):
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                raise OSError("disk read error")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Ashkenaz/timeline")
        assert response.status_code == 500


class TestCommunityTimelineDataShapes:
    def test_timeline_with_list_of_dict_entries(self, test_client, monkeypatch):
        import backend.routes_community as rc
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                import io
                import json
                payload = json.dumps({
                    "heritage_id": "test",
                    "identity": {"primary_origin": "Test Origin"},
                    "timeline": [{"title": "Event 1", "description": "desc", "year": "1800"}],
                })
                return io.StringIO(payload)
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Ashkenaz/timeline")
        assert response.status_code == 200
        body = response.get_json()
        assert any(e["title"] == "Event 1" for e in body["events"])
        assert any(e["title"] == "Primary Origin" for e in body["events"])

    def test_timeline_with_string_list_entries(self, test_client, monkeypatch):
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                import io
                import json
                payload = json.dumps({"history": ["A historic event occurred."]})
                return io.StringIO(payload)
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Sefardic/timeline")
        assert response.status_code == 200
        body = response.get_json()
        assert any("historic event" in e["description"] for e in body["events"])

    def test_timeline_with_string_value(self, test_client, monkeypatch):
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                import io
                import json
                payload = json.dumps({"migration_story": "A long journey westward."})
                return io.StringIO(payload)
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Yemenite/timeline")
        assert response.status_code == 200
        body = response.get_json()
        assert any("journey westward" in e["description"] for e in body["events"])

    def test_timeline_no_data_falls_back_to_tradition_entry(self, test_client, monkeypatch):
        real_open = builtins.open

        def fake_open(path, *a, **k):
            if "customs" in str(path) and path.endswith(".json"):
                import io
                return io.StringIO("{}")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        response = test_client.get("/api/community/Moroccan/timeline")
        assert response.status_code == 200
        body = response.get_json()
        assert body["events"][0]["title"] == "Tradition"


class TestPrayerPreviewFallbacks:
    def test_prayer_not_found_in_sefaria_returns_404(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_text", lambda ref: {"error": "not found"})
        response = test_client.get("/api/prayer/Weekday Shacharit")
        assert response.status_code == 404

    def test_hebrew_only_content_uses_english_fallback_text(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_text", lambda ref: {
            "he": ["שלום"], "en": [],
            "lines": [{"he": "שלום", "en": ""}],
        })
        response = test_client.get("/api/prayer/Weekday Shacharit")
        assert response.status_code == 200
        body = response.get_json()
        assert "Preview available in Hebrew" in body["content"]["en"]

    def test_english_only_content_uses_hebrew_fallback_text(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_text", lambda ref: {
            "he": [], "en": ["Peace"],
            "lines": [{"he": "", "en": "Peace"}],
        })
        response = test_client.get("/api/prayer/Weekday Shacharit")
        assert response.status_code == 200
        body = response.get_json()
        assert "תצוגה מקדימה" in body["content"]["he"]


class TestSiddurFullFallback:
    def test_no_text_available_returns_404(self, test_client, monkeypatch):
        import backend.sefaria_library as sl
        monkeypatch.setattr(sl, "get_text", lambda ref: {"error": "not found"})
        response = test_client.get("/api/siddur/full/Weekday Shacharit")
        assert response.status_code == 404
        assert "error" in response.get_json()


class TestSegmentReport:
    def test_post_without_auth_logs_report(self, test_client):
        response = test_client.post("/api/devtools/segment-report", json={
            "kind": "typo", "message": "Found an issue", "segment": "1", "ref": "Genesis 1:1",
        })
        assert response.status_code == 200
        body = response.get_json()
        assert body == {"ok": True, "logged": True}

    def test_post_with_missing_payload_still_succeeds(self, test_client):
        response = test_client.post("/api/devtools/segment-report", json={})
        assert response.status_code == 200
