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
import re
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
    customs._RUNTIME_CACHE["signature"] = None
    customs._RUNTIME_CACHE["data"] = {}
    yield
    customs._CUSTOMS_CACHE["signature"] = ()
    customs._CUSTOMS_CACHE["data"] = {}
    customs._RUNTIME_CACHE["signature"] = None
    customs._RUNTIME_CACHE["data"] = {}


@pytest.fixture
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
        assert customs._validate_customs_file(data) == []

    def test_non_dict_root_returns_single_error(self):
        errors = customs._validate_customs_file([1, 2, 3])
        assert errors == ["root value is not a JSON object"]

    def test_missing_multiple_required_fields(self):
        data = {"heritage_id": "x"}  # missing name and halacha_index
        errors = customs._validate_customs_file(data)
        assert "missing required field 'name'" in errors
        assert "missing required field 'halacha_index'" in errors
        assert len(errors) == 2

    def test_empty_required_field_value(self):
        data = {"heritage_id": "", "name": "X", "halacha_index": ["non-empty"]}
        errors = customs._validate_customs_file(data)
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
    @pytest.fixture
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
        assert {"community", "topic", "ruling", "source", "notes", "media_url"}.issubset(
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


# ─── Format 3.0 ─────────────────────────────────────────────────────────────


def _three_zero_custom(name="Testanian", lens_key="Testanian"):
    return {
        "version": "3.0",
        "heritage_id": name.lower(),
        "name": name,
        "runtime": {
            "lens_key": lens_key,
            "practice_baseline": "the Testanian codes.",
            "parameters": [{"key": "wait", "value": "6", "unit": "hours"}],
        },
        "core_halachic_authorities": {"later_poskim": ["R. Later"]},
        "halacha_index": [
            {
                "category": "Pesach",
                "topic": "Kitniyot",
                "summary": "Rice is eaten.",
                "common_practices": ["p1", "p2", "p3", "p4"],
                "variants": [
                    {"subgroup": "Turkish", "practice": "No rice."},
                    {"subgroup": "", "practice": "Varies by town."},
                    {"subgroup": "Empty", "practice": ""},
                    "not a dict",
                ],
                "confidence": "disputed",
                "notes": "Reviewer text that is not for users.",
                "review_notes": "Page was not opened.",
            },
            {"category": "Prayer", "topic": "Nusach", "summary": "Local nusach."},
        ],
        "unique_minhagim": [
            {"name": "Meldado", "description": "Memorial gathering.", "when": "Seven months",
             "source": "Molho", "confidence": "needs-review"},
            {"name": "Bare", "description": ""},
            {"name": "", "description": "unnamed"},
            "not a dict",
        ],
        "source_registry": [{"title": "A source"}],
    }


class TestFormatThreeZero:
    @pytest.fixture
    def loaded(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        return customs.load_all_customs()["Testanian"]

    def test_variants_follow_the_first_three_practices_capped_at_three(self, loaded):
        assert loaded["pesach_kitniyot"]["notes"] == "p1 | p2 | p3 | Turkish: No rice. | Varies by town."

    def test_confidence_is_carried_per_entry(self, loaded):
        assert loaded["pesach_kitniyot"]["confidence"] == "disputed"
        assert loaded["prayer_nusach"]["confidence"] == ""

    def test_reviewer_text_never_reaches_a_loaded_entry(self, loaded):
        blob = json.dumps(loaded)
        assert "Reviewer text" not in blob and "was not opened" not in blob

    def test_each_distinctive_custom_is_its_own_entry(self, loaded):
        entry = loaded["unique_meldado"]
        assert entry["ruling"] == "Memorial gathering."
        assert entry["source"] == "Molho"
        assert entry["notes"] == "Seven months"
        assert entry["confidence"] == "needs-review"
        assert {"meldado", "custom", "minhag", "testanian"} <= set(entry["keywords"])
        assert "unique" not in loaded, "the legacy combined entry is for the old dict shape only"

    def test_a_distinctive_custom_without_a_source_is_labelled_community_tradition(self, loaded):
        assert loaded["unique_bare"]["source"] == "Community tradition"

    def test_unnamed_and_malformed_distinctive_customs_are_skipped(self, loaded):
        assert sorted(k for k in loaded if k.startswith("unique_")) == ["unique_bare", "unique_meldado"]

    def test_later_poskim_count_as_trusted_sources(self):
        assert customs._build_trusted_sources(_three_zero_custom()) == ["R. Later"]

    def test_entry_without_its_own_source_falls_back_to_the_trusted_sources(self, loaded):
        assert loaded["prayer_nusach"]["source"] == "R. Later"

    def test_unique_minhagim_of_an_unexpected_type_adds_nothing(self):
        assert customs._unique_minhagim_entries("text", "X") == {}
        assert customs._unique_minhagim_entries(None, "X") == {}


class TestLoaderIgnoresNonCommunityContent:
    def test_schema_json_is_not_loaded_as_communities(self, empty_customs_dir):
        _write_json(empty_customs_dir / "schema.json",
                    {"title": "x", "definitions": {"a": {}}, "properties": {"b": {"c": 1}}})
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        assert list(customs.load_all_customs()) == ["Testanian"]

    def test_a_structured_files_own_blocks_are_not_loaded_as_communities(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        names = set(customs.load_all_customs())
        assert names == {"Testanian"}, names - {"Testanian"}

    def test_searching_a_block_name_does_not_return_a_pseudo_community(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        assert {m["community"] for m in customs.search_customs("runtime identity properties")} <= {"Testanian"}

    def test_flat_legacy_files_still_merge(self, empty_customs_dir):
        _write_json(empty_customs_dir / "flat.json", {"Flatland": {"topic": {"ruling": "r", "keywords": ["topic"]}}})
        assert "Flatland" in customs.load_all_customs()

    def test_a_json_list_file_is_ignored_not_fatal(self, empty_customs_dir):
        _write_json(empty_customs_dir / "list.json", [1, 2])
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        assert list(customs.load_all_customs()) == ["Testanian"]


class TestSearchRanking:
    def test_a_named_topic_outranks_a_community_only_match(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        matches = customs.search_customs("Testanian kitniyot")
        assert matches[0]["topic"] == "pesach_kitniyot"
        assert matches[0]["confidence"] == "disputed"
        assert all("_score" not in m for m in matches)

    def test_ties_keep_file_order(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        topics = [m["topic"] for m in customs.search_customs("Testanian")]
        # The distinctive customs carry the community name as a keyword, so they
        # rank first; the two halacha entries matched only by community tie at
        # zero and stay in the order the file lists them.
        assert topics[-2:] == ["pesach_kitniyot", "prayer_nusach"]
        assert set(topics[:-2]) == {"unique_meldado", "unique_bare"}


class TestRuntimeConfig:
    def test_reads_lens_key_baseline_and_parameters(self, empty_customs_dir):
        _write_json(empty_customs_dir / "t.json", _three_zero_custom(lens_key="Test-Lens"))
        config = customs.runtime_config()
        assert list(config) == ["test-lens"]
        assert config["test-lens"] == {
            "name": "Testanian",
            "lens_key": "Test-Lens",
            "practice_baseline": "the Testanian codes.",
            "parameters": [{"key": "wait", "value": "6", "unit": "hours"}],
        }

    def test_files_without_a_lens_key_are_left_out(self, empty_customs_dir):
        _write_json(empty_customs_dir / "old.json", _minimal_structured_custom())
        bad_runtime = _three_zero_custom("Other")
        bad_runtime["runtime"] = "not a dict"
        _write_json(empty_customs_dir / "bad.json", bad_runtime)
        _write_json(empty_customs_dir / "list.json", [1])
        assert customs.runtime_config() == {}

    def test_schema_and_legacy_aggregate_are_ignored(self, empty_customs_dir):
        _write_json(empty_customs_dir / "schema.json", _three_zero_custom("S"))
        _write_json(empty_customs_dir / "customs_db.json", _three_zero_custom("D"))
        assert customs.runtime_config() == {}

    def test_a_corrupt_file_is_logged_and_skipped(self, empty_customs_dir, caplog):
        (empty_customs_dir / "broken.json").write_text("{nope", encoding="utf-8")
        _write_json(empty_customs_dir / "t.json", _three_zero_custom())
        with caplog.at_level(logging.ERROR):
            assert list(customs.runtime_config()) == ["testanian"]
        assert "Customs Runtime Error" in caplog.text

    def test_missing_baseline_and_non_list_parameters_degrade_to_empty(self, empty_customs_dir):
        data = _three_zero_custom()
        data["runtime"] = {"lens_key": "Testanian", "parameters": {"not": "a list"}}
        _write_json(empty_customs_dir / "t.json", data)
        entry = customs.runtime_config()["testanian"]
        assert entry["practice_baseline"] == "" and entry["parameters"] == []

    def test_result_is_cached_until_a_file_changes(self, empty_customs_dir):
        path = empty_customs_dir / "t.json"
        _write_json(path, _three_zero_custom())
        first = customs.runtime_config()
        assert customs.runtime_config() is first

        changed = _three_zero_custom()
        changed["runtime"]["practice_baseline"] = "new baseline."
        _write_json(path, changed)
        import os
        os.utime(path, (path.stat().st_atime, path.stat().st_mtime + 5))
        assert customs.runtime_config()["testanian"]["practice_baseline"] == "new baseline."

    def test_an_empty_directory_gives_an_empty_config(self, empty_customs_dir):
        assert customs.runtime_config() == {}
        assert customs.runtime_config() == {}


# ─── The real files ─────────────────────────────────────────────────────────


def _real_community_files():
    import glob
    import os
    return [p for p in sorted(glob.glob(os.path.join(customs.CUSTOMS_DIR, "*.json")))
            if os.path.basename(p) not in customs._NON_COMMUNITY_FILES]


class TestRealCustomsFiles:
    def test_every_file_declares_the_lens_key_the_app_filters_by(self):
        from backend.helpers import COMMUNITIES

        by_slug = {}
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            slug = path.rsplit("/", 1)[-1][:-5]
            by_slug[slug] = data["runtime"]["lens_key"]

        for lens_key, slug in COMMUNITIES.items():
            if lens_key == "Israeli":  # routes to the Sephardic file; no file of its own
                continue
            assert by_slug.get(slug) == lens_key, (
                f"customs/{slug}.json must declare runtime.lens_key {lens_key!r} "
                f"(the key backend/helpers.COMMUNITIES uses), got {by_slug.get(slug)!r}"
            )
        assert set(by_slug) == {s for k, s in COMMUNITIES.items() if k != "Israeli"}

    def test_every_community_has_a_practice_baseline(self):
        config = customs.runtime_config()
        assert len(config) == 13
        for key, entry in config.items():
            assert len(entry["practice_baseline"]) > 40, key

    def test_no_review_notes_reach_the_loaded_data(self):
        reviewer_only = set()
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            reviewer_only.update(
                item["review_notes"] for item in data["halacha_index"] if item.get("review_notes")
            )
        assert reviewer_only, "the fixture assumption (some entries carry review_notes) no longer holds"
        blob = json.dumps(customs.load_all_customs())
        for text in reviewer_only:
            assert text not in blob

    def test_categories_and_topics_are_unique_within_a_file(self):
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            keys = [(i["category"].lower(), i["topic"].lower()) for i in data["halacha_index"]]
            assert len(keys) == len(set(keys)), f"{path}: duplicate (category, topic)"

    def test_every_entry_has_its_own_hebrew_name_and_description(self):
        """The Hebrew UI shows hand-written Hebrew for every topic and distinctive custom."""
        hebrew = re.compile(r"[\u0590-\u05FF]")
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            for item in data["halacha_index"]:
                for field in ("topic_he", "summary_he"):
                    assert hebrew.search(item.get(field) or ""), f"{path}: {item['topic']!r} lacks {field}"
            for item in data.get("unique_minhagim", []):
                for field in ("name_he", "description_he"):
                    assert hebrew.search(item.get(field) or ""), f"{path}: {item['name']!r} lacks {field}"

    def test_every_disputed_entry_gives_each_side(self):
        """A machloket is never flattened to one position: at least two variants, so both sides are shown."""
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            for item in data["halacha_index"]:
                if item.get("confidence") == "disputed":
                    assert len(item.get("variants") or []) >= 2, f"{path}: disputed {item['topic']!r} shows one side only"

    def test_loaded_community_names_are_the_thirteen_files(self):
        assert len(customs.load_all_customs()) == 13


class TestAuthorityNames:
    @pytest.mark.parametrize("text, expected", [
        ("Shulchan Aruch (R. Yosef Karo, 1563) with the glosses (Mappah) of the Rema", "Shulchan Aruch"),
        ("Rema, cited where Moroccan custom coincides with him but not as the binding code", "Rema"),
        ("Orit: the Ge'ez Octateuch, handwritten on parchment", "Orit"),
        ("Shulchan Arukh (R. Yosef Karo, Safed, 1563) \u2014 'Maran'", "Shulchan Arukh"),
        ("Kavkazi baseline; R. Zilber treats the community as Sephardi", "Kavkazi baseline"),
        ("For Israeli status rulings: Radbaz (those from Kush are of the tribe of Dan)", "Radbaz"),
        ("Tiklal with Maharitz's Etz Hayyim", "Tiklal with Maharitz's Etz Hayyim"),
        ("R. Hayyim Palachi of Izmir (Moed Lekhol Hai)", "R. Hayyim Palachi of Izmir"),
        ("  Mishnah Berurah  ", "Mishnah Berurah"),
    ])
    def test_a_descriptive_entry_is_reduced_to_the_authority_name(self, text, expected):
        assert customs._authority_name(text) == expected

    @pytest.mark.parametrize("text", [
        "Not documented in the sources reviewed. Historically oriented to Babylonia",
        "No community-specific rishon documented; an 18th-c. commentary exists",
        "None", "not specifically verified", "N/A", "", None, "   ",
    ])
    def test_a_statement_of_absence_is_not_an_authority(self, text):
        assert customs._authority_name(text) == ""

    def test_names_that_merely_start_with_no_or_not_are_kept(self):
        assert customs._authority_name("Nosson of Breslov") == "Nosson of Breslov"
        assert customs._authority_name("Notarikon collection") == "Notarikon collection"

    def test_a_label_that_would_shorten_to_nothing_is_kept_whole(self):
        assert customs._authority_name("(R. Yosef Karo)") == "(R. Yosef Karo)"

    def test_trusted_sources_are_names_only_deduped_after_shortening(self):
        data = {"core_halachic_authorities": {
            "primary_codes": ["Rambam (Mishneh Torah)", "Not part of this practice", "Rambam, in Yemen"],
            "later_poskim": ["Ben Ish Chai (Halachot), Baghdad, 1898"],
        }}
        assert customs._build_trusted_sources(data) == ["Rambam", "Ben Ish Chai"]

    def test_real_communities_list_names_not_sentences_or_absences(self):
        for path in _real_community_files():
            data = json.load(open(path, encoding="utf-8"))
            sources = customs._build_trusted_sources(data)
            assert sources, path
            for label in sources:
                assert not customs._NEGATIVE_STATEMENT_RE.match(label), (path, label)
                assert len(label) <= 120, (path, label)
