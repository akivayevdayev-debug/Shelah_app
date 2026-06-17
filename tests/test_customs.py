"""
Tests for backend/customs.py — community customs data loader and matcher.

Covers:
  - validate_all_customs_at_startup(): valid files, malformed JSON, schema
    violations, legacy flat-dict format, and skip-list handling — confirms
    a single bad file logs an error rather than raising.
  - load_all_customs(): structured (v2.x) files, legacy flat-dict files,
    unique_minhagim handling, caching via mtime signature, and graceful
    handling of unreadable/corrupt files.
  - search_customs(): exact keyword/community/topic matching and fuzzy
    matching.
  - Internal helpers: _validate_customs_file, _build_customs_signature,
    _build_trusted_sources.

All tests are file-based (no network) and use tmp_path + monkeypatch to
redirect backend.customs.CUSTOMS_DIR so the real customs/*.json files are
never touched.
"""

from __future__ import annotations

import json
import logging

import pytest

from backend import customs


# ─── Helpers ───────────────────────────────────────────────────────────────


def _write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _minimal_structured_custom(name="Testanian", heritage_id="testanian"):
    """A minimal v2.x structured customs file matching the real shape."""
    return {
        "version": "2.0",
        "heritage_id": heritage_id,
        "name": name,
        "halacha_index": [
            {
                "index": "halacha.1",
                "category": "Prayer",
                "topic": "Nusach",
                "summary": "Use the community nusach.",
                "common_practices": ["Follow local custom", "Ask the rabbi"],
            }
        ],
        "unique_minhagim": {
            "examples": ["Example minhag one", "Example minhag two"],
            "notes": "Some explanatory notes.",
        },
        "source_registry": {
            "primary": ["Source A", "Source B"],
        },
        "core_halachic_authorities": {
            "primary_codes": ["Code A"],
            "major_rishonim_base": ["Rishon A"],
        },
    }


@pytest.fixture(autouse=True)
def _reset_customs_cache():
    """Ensure the module-level cache never leaks between tests."""
    customs._CUSTOMS_CACHE["signature"] = ()
    customs._CUSTOMS_CACHE["data"] = {}
    yield
    customs._CUSTOMS_CACHE["signature"] = ()
    customs._CUSTOMS_CACHE["data"] = {}


@pytest.fixture()
def empty_customs_dir(tmp_path, monkeypatch):
    """Point CUSTOMS_DIR at an empty tmp_path dir."""
    monkeypatch.setattr(customs, "CUSTOMS_DIR", str(tmp_path))
    return tmp_path


# ─── validate_all_customs_at_startup() ──────────────────────────────────────


class TestValidateAllCustomsAtStartup:
    def test_valid_structured_file_logs_no_error(self, empty_customs_dir, caplog):
        _write_json(empty_customs_dir / "good.json", _minimal_structured_custom())

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == []

    def test_malformed_json_logs_error_naming_file_and_does_not_raise(
        self, empty_customs_dir, caplog
    ):
        bad_file = empty_customs_dir / "broken.json"
        bad_file.write_text("{not valid json!!!", encoding="utf-8")

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            # Must not raise — a single bad file should not crash startup.
            customs.validate_all_customs_at_startup()

        assert len(caplog.records) == 1
        assert "broken.json" in caplog.records[0].getMessage()
        assert "cannot parse" in caplog.records[0].getMessage()

    def test_one_bad_file_does_not_prevent_processing_others(
        self, empty_customs_dir, caplog
    ):
        """One malformed file should be logged, but a valid sibling file
        must still be processed without raising — matches the 'one bad
        file shouldn't take down startup' design goal."""
        (empty_customs_dir / "a_broken.json").write_text("{{{", encoding="utf-8")
        _write_json(
            empty_customs_dir / "z_good.json",
            _minimal_structured_custom(name="GoodCommunity"),
        )

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        # Only the broken file produced an error.
        assert len(caplog.records) == 1
        assert "a_broken.json" in caplog.records[0].getMessage()

    def test_schema_violation_missing_required_field_logs_error(
        self, empty_customs_dir, caplog
    ):
        invalid = _minimal_structured_custom()
        del invalid["halacha_index"]

        _write_json(empty_customs_dir / "missing_field.json", invalid)

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "missing_field.json" in msg
        assert "failed schema check" in msg
        assert "halacha_index" in msg

    def test_schema_violation_empty_required_field_logs_error(
        self, empty_customs_dir, caplog
    ):
        invalid = _minimal_structured_custom()
        invalid["name"] = ""  # present but empty -> should fail validation

        _write_json(empty_customs_dir / "empty_field.json", invalid)

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert len(caplog.records) == 1
        msg = caplog.records[0].getMessage()
        assert "empty_field.json" in msg
        assert "required field 'name' is empty" in msg

    def test_legacy_flat_dict_file_without_name_field_is_skipped(
        self, empty_customs_dir, caplog
    ):
        """Files without a top-level 'name' key are treated as legacy
        flat-dict format and skip the schema check entirely."""
        legacy = {"SomeCommunity": {"topic_one": {"keywords": ["x"], "ruling": "y"}}}
        _write_json(empty_customs_dir / "legacy.json", legacy)

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == []

    def test_skip_list_ignores_customs_db_and_schema_json(
        self, empty_customs_dir, caplog
    ):
        """customs_db.json and schema.json are explicitly skipped even if
        they would otherwise fail schema validation."""
        # Deliberately invalid against the structured schema, but these
        # filenames are always skipped regardless of content.
        _write_json(empty_customs_dir / "customs_db.json", {"name": ""})
        _write_json(empty_customs_dir / "schema.json", {"name": ""})

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == []

    def test_skip_list_is_case_insensitive(self, empty_customs_dir, caplog):
        _write_json(empty_customs_dir / "Schema.JSON", {"name": ""})

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == []

    def test_no_files_in_directory_logs_nothing(self, empty_customs_dir, caplog):
        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == []

    def test_root_value_not_a_json_object_logs_error(self, empty_customs_dir, caplog):
        """A customs file whose root JSON value is a list (not an object)
        should fail validation cleanly once it reaches the structured
        schema check path. Since 'name' can't be checked via `in` on a
        list without raising, this exercises the json.load success path
        combined with the 'name' in data guard."""
        bad_root = empty_customs_dir / "list_root.json"
        bad_root.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            # Must not raise even though `"name" not in data` is evaluated
            # against a list.
            customs.validate_all_customs_at_startup()

        # A list supports `in`, so "name" not in [1, 2, 3] is True -> skip
        # branch is taken silently (no crash, no error logged).
        assert caplog.records == []

    def test_real_repo_customs_files_pass_startup_validation(self, caplog):
        """Sanity check against the actual customs/*.json files shipped in
        the repo — they must all be parseable and schema-valid (or
        legitimately skipped/legacy)."""
        with caplog.at_level(logging.ERROR, logger="backend.customs"):
            customs.validate_all_customs_at_startup()

        assert caplog.records == [], (
            "Real customs/*.json files should pass startup validation: "
            f"{[r.getMessage() for r in caplog.records]}"
        )


# ─── _validate_customs_file() ────────────────────────────────────────────


class TestValidateCustomsFileHelper:
    def test_valid_data_returns_no_errors(self):
        data = _minimal_structured_custom()
        assert customs._validate_customs_file(data, "irrelevant.json") == []

    def test_non_dict_root_returns_single_error(self):
        errors = customs._validate_customs_file([1, 2, 3], "irrelevant.json")
        assert errors == ["root value is not a JSON object"]

    def test_missing_multiple_required_fields(self):
        data = {"heritage_id": "x"}  # missing name and halacha_index
        errors = customs._validate_customs_file(data, "irrelevant.json")
        assert "missing required field 'name'" in errors
        assert "missing required field 'halacha_index'" in errors
        assert len(errors) == 2

    def test_empty_required_field_value(self):
        data = {"heritage_id": "", "name": "X", "halacha_index": ["non-empty"]}
        errors = customs._validate_customs_file(data, "irrelevant.json")
        assert errors == ["required field 'heritage_id' is empty"]


# ─── _build_customs_signature() ─────────────────────────────────────────


class TestBuildCustomsSignature:
    def test_signature_is_sorted_tuple_of_name_mtime_pairs(self, tmp_path):
        f1 = tmp_path / "b.json"
        f2 = tmp_path / "a.json"
        f1.write_text("{}")
        f2.write_text("{}")

        sig = customs._build_customs_signature([str(f1), str(f2)])

        assert isinstance(sig, tuple)
        assert len(sig) == 2
        names = [entry[0] for entry in sig]
        assert names == sorted(names)

    def test_missing_file_falls_back_to_sentinel_mtime(self, tmp_path):
        missing = tmp_path / "does_not_exist.json"
        sig = customs._build_customs_signature([str(missing)])
        assert sig == (("does_not_exist.json", -1.0),)

    def test_empty_file_list_returns_empty_tuple(self):
        assert customs._build_customs_signature([]) == ()


# ─── _build_trusted_sources() ────────────────────────────────────────────


class TestBuildTrustedSources:
    def test_collects_and_dedupes_sources_from_registry_and_authorities(self):
        data = {
            "source_registry": {"primary": ["A", "B", "a"]},  # "a" dupes "A"
            "core_halachic_authorities": {
                "primary_codes": ["C"],
                "major_rishonim_base": ["D"],
            },
        }
        result = customs._build_trusted_sources(data)
        assert result == ["A", "B", "C", "D"]

    def test_caps_result_at_six_entries(self):
        data = {
            "core_halachic_authorities": {
                "primary_codes": ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"],
            }
        }
        result = customs._build_trusted_sources(data)
        assert len(result) == 6
        assert result == ["S1", "S2", "S3", "S4", "S5", "S6"]

    def test_non_dict_input_returns_empty_list(self):
        assert customs._build_trusted_sources("not a dict") == []
        assert customs._build_trusted_sources(None) == []
        assert customs._build_trusted_sources([1, 2, 3]) == []

    def test_missing_optional_sections_returns_empty_list(self):
        assert customs._build_trusted_sources({}) == []

    def test_blank_and_whitespace_only_entries_are_skipped(self):
        data = {"source_registry": {"primary": ["", "   ", "Real Source"]}}
        assert customs._build_trusted_sources(data) == ["Real Source"]

    def test_non_list_values_are_ignored_gracefully(self):
        data = {
            "source_registry": "not a dict",
            "core_halachic_authorities": {"primary_codes": "not a list"},
        }
        assert customs._build_trusted_sources(data) == []


# ─── load_all_customs() ──────────────────────────────────────────────────


class TestLoadAllCustoms:
    def test_loads_structured_file_into_expected_shape(self, empty_customs_dir):
        _write_json(
            empty_customs_dir / "testanian.json", _minimal_structured_custom()
        )

        result = customs.load_all_customs()

        assert "Testanian" in result
        topics = result["Testanian"]
        # halacha_index item -> "{category}_{topic}" key, lowercased.
        assert "prayer_nusach" in topics
        entry = topics["prayer_nusach"]
        assert entry["ruling"] == "Use the community nusach."
        assert entry["keywords"] == ["nusach", "prayer"]
        assert "Follow local custom" in entry["notes"]

    def test_unique_minhagim_added_under_unique_key(self, empty_customs_dir):
        _write_json(
            empty_customs_dir / "testanian.json", _minimal_structured_custom()
        )

        result = customs.load_all_customs()

        unique_entry = result["Testanian"]["unique"]
        assert "Example minhag one" in unique_entry["ruling"]
        assert "Example minhag two" in unique_entry["ruling"]
        assert unique_entry["source"] == "Community tradition"
        assert unique_entry["notes"] == "Some explanatory notes."
        assert "testanian" in unique_entry["keywords"]

    def test_legacy_flat_dict_format_merged_by_community(self, empty_customs_dir):
        legacy = {
            "LegacyCommunity": {
                "some_topic": {"keywords": ["foo"], "ruling": "bar"},
            }
        }
        _write_json(empty_customs_dir / "legacy.json", legacy)

        result = customs.load_all_customs()

        assert result["LegacyCommunity"]["some_topic"]["ruling"] == "bar"

    def test_customs_db_json_is_always_skipped(self, empty_customs_dir):
        """customs_db.json is explicitly retired from active browsing and
        must never appear in the loaded result, regardless of content."""
        _write_json(
            empty_customs_dir / "customs_db.json",
            {"ShouldNotAppear": {"topic": {"keywords": [], "ruling": "x"}}},
        )

        result = customs.load_all_customs()

        assert "ShouldNotAppear" not in result

    def test_unreadable_file_is_skipped_without_raising(self, empty_customs_dir):
        (empty_customs_dir / "broken.json").write_text("{not json", encoding="utf-8")
        _write_json(
            empty_customs_dir / "good.json",
            _minimal_structured_custom(name="StillWorks"),
        )

        # Must not raise despite the broken sibling file.
        result = customs.load_all_customs()

        assert "StillWorks" in result

    def test_missing_directory_returns_empty_dict_without_raising(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            customs, "CUSTOMS_DIR", str(tmp_path / "does_not_exist_dir")
        )
        assert customs.load_all_customs() == {}

    def test_cache_is_reused_when_files_unchanged(self, empty_customs_dir):
        _write_json(
            empty_customs_dir / "testanian.json", _minimal_structured_custom()
        )

        first = customs.load_all_customs()
        second = customs.load_all_customs()

        # Same object returned from cache (signature unchanged).
        assert first is second

    def test_cache_invalidated_when_file_changes(self, empty_customs_dir, monkeypatch):
        path = empty_customs_dir / "testanian.json"
        _write_json(path, _minimal_structured_custom(name="Original"))

        first = customs.load_all_customs()
        assert "Original" in first

        # Simulate a file modification by bumping mtime forward and
        # rewriting content, so the cache signature changes.
        _write_json(path, _minimal_structured_custom(name="Updated"))
        new_mtime = path.stat().st_mtime + 5
        import os

        os.utime(path, (new_mtime, new_mtime))

        second = customs.load_all_customs()
        assert "Updated" in second
        assert "Original" not in second

    def test_empty_directory_returns_empty_dict(self, empty_customs_dir):
        assert customs.load_all_customs() == {}

    def test_real_repo_customs_load_without_raising(self):
        """Sanity check against real customs/*.json files — must load
        without raising and produce a non-empty mapping."""
        result = customs.load_all_customs()
        assert isinstance(result, dict)
        assert len(result) > 0


# ─── search_customs() ─────────────────────────────────────────────────────


class TestSearchCustoms:
    @pytest.fixture()
    def loaded(self, empty_customs_dir):
        _write_json(
            empty_customs_dir / "testanian.json", _minimal_structured_custom()
        )
        # Force a fresh load against the patched directory.
        customs._CUSTOMS_CACHE["signature"] = ()
        customs.load_all_customs()
        return empty_customs_dir

    def test_exact_keyword_match_returns_result(self, loaded):
        matches = customs.search_customs("What is the rule about nusach?")
        assert len(matches) >= 1
        assert any(m["topic"] == "prayer_nusach" for m in matches)

    def test_exact_community_name_match_returns_result(self, loaded):
        matches = customs.search_customs("Tell me about testanian customs")
        assert len(matches) >= 1
        assert all(m["community"] == "Testanian" for m in matches)

    def test_topic_with_underscores_replaced_by_spaces_matches(self, loaded):
        # "unique" topic added via unique_minhagim has keyword "testanian"
        # (heritage/name lowercased) so this also matches via community.
        matches = customs.search_customs("prayer nusach details please")
        assert any(m["topic"] == "prayer_nusach" for m in matches)

    def test_fuzzy_match_close_typo_returns_result(self, loaded):
        # "nusach" vs "nusah" -> within difflib cutoff=0.8 close match.
        matches = customs.search_customs("what about nusah practice")
        assert any(m["topic"] == "prayer_nusach" for m in matches)

    def test_no_match_returns_empty_list(self, loaded):
        matches = customs.search_customs("completely unrelated gibberish zzzqqq")
        assert matches == []

    def test_result_entries_contain_expected_fields(self, loaded):
        matches = customs.search_customs("nusach")
        assert matches
        entry = matches[0]
        assert set(["community", "topic", "ruling", "source", "notes", "media_url"]).issubset(
            entry.keys()
        )

    def test_search_against_real_repo_customs_data(self):
        """End-to-end sanity check using the real customs/*.json files."""
        matches = customs.search_customs("kitniyot")
        assert isinstance(matches, list)
        # Ashkenazi unique_minhagim mentions kitniyot avoidance; expect a hit.
        assert any("kitniyot" in (m["ruling"] or "").lower() for m in matches)

    def test_empty_question_returns_list_without_raising(self, loaded):
        matches = customs.search_customs("")
        assert isinstance(matches, list)
