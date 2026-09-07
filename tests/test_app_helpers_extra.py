"""
Direct unit coverage for app.py's module-level helper functions that have
no route to exercise them through test_client. Split out as SonarCloud
python:S3776 complexity-reduction refactors land, so each extracted piece
gets test-anchored instead of relying only on indirect coverage.
"""

from __future__ import annotations


class TestParseIpGeolocationResponse:
    def test_ip_api_success_shape(self, test_client):
        import app as flask_app_module
        lat, lon = flask_app_module._parse_ip_geolocation_response({
            "status": "success", "lat": 40.7128, "lon": -74.0060,
        })
        assert lat == 40.7128
        assert lon == -74.0060

    def test_ipwho_success_shape(self, test_client):
        import app as flask_app_module
        lat, lon = flask_app_module._parse_ip_geolocation_response({
            "success": True, "latitude": 51.5074, "longitude": -0.1278,
        })
        assert lat == 51.5074
        assert lon == -0.1278

    def test_ip_api_failure_status_returns_none(self, test_client):
        import app as flask_app_module
        lat, lon = flask_app_module._parse_ip_geolocation_response({
            "status": "fail", "message": "invalid query",
        })
        assert lat is None
        assert lon is None

    def test_ipwho_failure_returns_none(self, test_client):
        import app as flask_app_module
        lat, lon = flask_app_module._parse_ip_geolocation_response({
            "success": False,
        })
        assert lat is None
        assert lon is None

    def test_empty_response_returns_none(self, test_client):
        import app as flask_app_module
        lat, lon = flask_app_module._parse_ip_geolocation_response({})
        assert lat is None
        assert lon is None


class TestCollectTrustedAuthorityCandidates:
    def test_collects_source_registry_primary(self, test_client):
        import app as flask_app_module
        candidates = flask_app_module._collect_trusted_authority_candidates({
            "source_registry": {"primary": ["Shulchan Arukh", "Mishnah Berurah"]},
        })
        assert candidates == ["Shulchan Arukh", "Mishnah Berurah"]

    def test_collects_all_core_halachic_authority_keys(self, test_client):
        import app as flask_app_module
        candidates = flask_app_module._collect_trusted_authority_candidates({
            "core_halachic_authorities": {
                "primary_codes": ["Rif"],
                "major_rishonim_base": ["Rambam"],
                "later_ashkenazi_poskim": ["Rema"],
                "later_sephardi_poskim": ["Ben Ish Chai"],
                "later_moroccan_poskim": ["Kaf HaChaim"],
                "later_turkish_poskim": ["Chida"],
            },
        })
        assert candidates == ["Rif", "Rambam", "Rema", "Ben Ish Chai", "Kaf HaChaim", "Chida"]

    def test_non_dict_or_non_list_shapes_are_ignored(self, test_client):
        import app as flask_app_module
        candidates = flask_app_module._collect_trusted_authority_candidates({
            "source_registry": "not a dict",
            "core_halachic_authorities": {"primary_codes": "not a list"},
        })
        assert candidates == []

    def test_missing_keys_return_empty_list(self, test_client):
        import app as flask_app_module
        assert flask_app_module._collect_trusted_authority_candidates({}) == []


class TestDedupeSourceLabels:
    def test_strips_and_drops_blank_items(self, test_client):
        import app as flask_app_module
        result = flask_app_module._dedupe_source_labels(["  Rambam  ", "", None, "   "])
        assert result == ["Rambam"]

    def test_case_insensitive_dedupe_preserves_first_seen(self, test_client):
        import app as flask_app_module
        result = flask_app_module._dedupe_source_labels(["Rambam", "RAMBAM", "rambam", "Rema"])
        assert result == ["Rambam", "Rema"]

    def test_empty_input_returns_empty_list(self, test_client):
        import app as flask_app_module
        assert flask_app_module._dedupe_source_labels([]) == []


class TestBuildTrustedCustomSources:
    def test_non_dict_input_returns_empty_list(self, test_client):
        import app as flask_app_module
        assert flask_app_module._build_trusted_custom_sources(None) == []
        assert flask_app_module._build_trusted_custom_sources("not a dict") == []

    def test_combines_and_caps_at_six(self, test_client):
        import app as flask_app_module
        data = {
            "source_registry": {"primary": ["A", "B", "C"]},
            "core_halachic_authorities": {
                "primary_codes": ["D", "E"],
                "major_rishonim_base": ["F", "G"],
            },
        }
        result = flask_app_module._build_trusted_custom_sources(data)
        assert result == ["A", "B", "C", "D", "E", "F"]

    def test_deduplicates_across_registry_and_authorities(self, test_client):
        import app as flask_app_module
        data = {
            "source_registry": {"primary": ["Rambam"]},
            "core_halachic_authorities": {"primary_codes": ["rambam", "Rif"]},
        }
        result = flask_app_module._build_trusted_custom_sources(data)
        assert result == ["Rambam", "Rif"]


class TestCollectPreferredLanguageLines:
    def test_hebrew_answer_prefers_hebrew_falls_back_to_english(self, test_client):
        import app as flask_app_module
        lines = [{"he": "שלום", "en": "hello"}, {"he": "", "en": "world"}]
        result = flask_app_module._collect_preferred_language_lines(lines, "he")
        assert result == ["שלום", "world"]

    def test_english_answer_prefers_english_falls_back_to_hebrew(self, test_client):
        import app as flask_app_module
        lines = [{"he": "שלום", "en": "hello"}, {"he": "עולם", "en": ""}]
        result = flask_app_module._collect_preferred_language_lines(lines, "en")
        assert result == ["hello", "עולם"]

    def test_non_dict_lines_are_skipped(self, test_client):
        import app as flask_app_module
        result = flask_app_module._collect_preferred_language_lines(
            ["not a dict", None, {"en": "hello"}], "en")
        assert result == ["hello"]

    def test_lines_with_no_text_in_either_language_are_omitted(self, test_client):
        import app as flask_app_module
        result = flask_app_module._collect_preferred_language_lines(
            [{"he": "", "en": ""}], "en")
        assert result == []


class TestFlattenPrimarySourcesForClaude:
    def test_flattens_ref_and_joins_preferred_text(self, test_client):
        import app as flask_app_module
        primary_sources = [{
            "ref": "Genesis 1:1",
            "lines": [{"en": "In the beginning"}, {"en": "God created"}],
        }]
        result = flask_app_module._flatten_primary_sources_for_claude(primary_sources, "en")
        assert result == [{"ref": "Genesis 1:1", "text": "In the beginning God created"}]

    def test_non_dict_source_yields_empty_ref_and_text(self, test_client):
        import app as flask_app_module
        result = flask_app_module._flatten_primary_sources_for_claude(["not a dict"], "en")
        assert result == [{"ref": "", "text": ""}]

    def test_non_list_lines_yields_empty_text(self, test_client):
        import app as flask_app_module
        result = flask_app_module._flatten_primary_sources_for_claude(
            [{"ref": "Genesis 1:1", "lines": "not a list"}], "en")
        assert result == [{"ref": "Genesis 1:1", "text": ""}]

    def test_empty_input_returns_empty_list(self, test_client):
        import app as flask_app_module
        assert flask_app_module._flatten_primary_sources_for_claude([], "en") == []
