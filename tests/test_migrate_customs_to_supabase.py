"""Tests for scripts/migrate_customs_to_supabase.py.

The migration is an idempotent upsert keyed on a deterministic id, so the
properties that matter are: the id is stable across runs and case/whitespace
variations of the same (community, topic, source); both JSON shapes in
customs/ parse into the same row format; the community filter is exact; a
dry run never connects to Supabase; and a missing table fails BEFORE any row
is written.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "migrate_customs_to_supabase.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_customs_to_supabase", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mc = _load_script()


class TestNormalizeText:
    def test_collapses_whitespace_and_none_to_empty(self):
        assert mc._normalize_text("  a \n\t b   c ") == "a b c"
        assert mc._normalize_text(None) == ""

    def test_truncates_with_an_ellipsis_and_no_trailing_space_before_it(self):
        assert mc._normalize_text("abcdefghij", max_chars=5) == "abcde..."
        assert mc._normalize_text("ab cdefgh", max_chars=3) == "ab..."

    def test_text_at_the_limit_is_untouched(self):
        assert mc._normalize_text("abcde", max_chars=5) == "abcde"

    def test_non_strings_are_stringified(self):
        assert mc._normalize_text(42) == "42"


class TestStableId:
    def test_is_32_hex_chars_and_deterministic(self):
        first = mc._stable_id("Yemenite", "Kiddush", "Rambam")
        assert first == mc._stable_id("Yemenite", "Kiddush", "Rambam")
        assert len(first) == 32
        int(first, 16)

    def test_ignores_case_and_surrounding_whitespace(self):
        assert mc._stable_id("Yemenite", "Kiddush", "Rambam") == mc._stable_id(" yemenite ", "KIDDUSH", "rambam ")

    @pytest.mark.parametrize("other", [("Yemenite", "Kiddush", "Shulchan Arukh"),
                                       ("Yemenite", "Havdalah", "Rambam"),
                                       ("Persian", "Kiddush", "Rambam")])
    def test_any_differing_component_changes_the_id(self, other):
        assert mc._stable_id("Yemenite", "Kiddush", "Rambam") != mc._stable_id(*other)


class TestAuthoritiesFallback:
    def test_collects_across_fields_dedupes_case_insensitively_and_keeps_four(self):
        payload = {"core_halachic_authorities": {
            "primary_codes": ["Rambam", " ", "Shulchan Arukh"],
            "major_rishonim_base": ["rambam", "Rashba"],
            "later_sephardi_poskim": ["Ben Ish Chai", "Ovadia Yosef"],
        }}
        assert mc._authorities_fallback(payload) == "Rambam, Shulchan Arukh, Rashba, Ben Ish Chai"

    @pytest.mark.parametrize("payload", [{}, {"core_halachic_authorities": "text"},
                                         {"core_halachic_authorities": {"primary_codes": "not a list"}}])
    def test_missing_or_malformed_authorities_give_empty_string(self, payload):
        assert mc._authorities_fallback(payload) == ""


class TestBuildContent:
    def test_combines_summary_practices_and_notes(self):
        # Each part sits on its own line; the line breaks are kept in the
        # stored content instead of being flattened to spaces.
        content = mc._build_content("Summary.", list("abcdefg"), "Careful.")
        assert content == "Summary.\nCommon practices: a | b | c | d | e | f\nNotes: Careful."
        assert content.split("\n") == ["Summary.", "Common practices: a | b | c | d | e | f", "Notes: Careful."]

    def test_blank_practices_are_dropped_and_non_lists_ignored(self):
        assert mc._build_content("S", ["", "  ", "x"], None) == "S\nCommon practices: x"
        assert mc._build_content("S", "not a list", None) == "S"

    def test_a_single_part_has_no_stray_line_breaks(self):
        assert mc._build_content("Only summary.", None, None) == "Only summary."
        assert mc._build_content("", None, "Only notes.") == "Notes: Only notes."

    def test_whitespace_inside_a_field_is_still_collapsed(self):
        # Each field is normalised on its own (as topic/source/name are); only
        # the separators BETWEEN parts are preserved.
        content = mc._build_content("Line one\n\n  line   two", ["a\tb"], "n1\nn2")
        assert content == "Line one line two\nCommon practices: a b\nNotes: n1 n2"

    def test_everything_empty_gives_empty_content(self):
        assert mc._build_content("", [], "") == ""
        assert mc._build_content(None, None, None) == ""

    def test_overlong_content_is_capped_at_2200_chars_plus_ellipsis(self):
        content = mc._build_content("x" * 2000, ["y" * 180] * 4, "z" * 2000)
        assert len(content) <= 2203
        assert content.endswith("...")
        assert content.startswith("x" * 2000 + "\nCommon practices: ")
        assert "\n" in content, "truncation must not flatten the line breaks"


MODERN = {
    "name": "Yemenite",
    "core_halachic_authorities": {"primary_codes": ["Rambam"]},
    "halacha_index": [
        {"topic": "Kiddush", "source": "Rambam", "summary": "Standing.", "notes": "n"},
        {"index": "Havdalah", "summary": "Uses fallback source."},
        {"topic": "Empty", "summary": ""},
        "not a dict",
    ],
}

LEGACY = {
    "Ashkenazi": {
        "kitniyot_on_pesach": {"ruling": "Avoid.", "keywords": ["rice"], "source": "Rema"},
        "no_content": {"ruling": ""},
        "not_a_dict": "x",
    },
    "ignored_scalar": 5,
}


class TestParseModernPayload:
    def test_rows_carry_deterministic_ids_and_fallback_fields(self):
        rows = mc._parse_modern_payload(MODERN)

        assert [r["topic"] for r in rows] == ["Kiddush", "Havdalah"]
        assert rows[0]["halakhic_source"] == "Rambam"
        assert rows[1]["halakhic_source"] == "Rambam", "no per-item source -> the authorities fallback"
        assert rows[0]["id"] == mc._stable_id("Yemenite", "Kiddush", "Rambam")
        assert rows[0]["content"] == "Standing.\nNotes: n"

    def test_falls_back_to_heritage_id_then_unknown_and_generic_source(self):
        rows = mc._parse_modern_payload({"heritage_id": "H1", "halacha_index": [{"summary": "s"}]})
        assert rows[0]["community_name"] == "H1"
        assert rows[0]["topic"] == "General"
        assert rows[0]["halakhic_source"] == "Community halakhic tradition"
        assert mc._parse_modern_payload({"halacha_index": [{"summary": "s"}]})[0]["community_name"] == "Unknown"

    def test_null_index_yields_no_rows(self):
        assert mc._parse_modern_payload({"name": "X", "halacha_index": None}) == []


class TestParseLegacyPayload:
    def test_topic_keys_become_words_and_defaults_apply(self):
        rows = mc._parse_legacy_payload(LEGACY)

        assert len(rows) == 1
        row = rows[0]
        assert row["community_name"] == "Ashkenazi"
        assert row["topic"] == "kitniyot on pesach"
        assert row["halakhic_source"] == "Rema"
        assert row["content"] == "Avoid.\nCommon practices: rice"
        assert row["id"] == mc._stable_id("Ashkenazi", "kitniyot on pesach", "Rema")

    def test_missing_source_defaults_to_community_tradition(self):
        rows = mc._parse_legacy_payload({"C": {"t": {"ruling": "r"}}})
        assert rows[0]["halakhic_source"] == "Community tradition"


class TestLoadRowsFromJson:
    def write(self, directory, name, payload):
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")

    def test_reads_both_shapes_skips_bad_files_and_ignores_non_json(self, tmp_path, capsys):
        self.write(tmp_path, "a_modern.json", MODERN)
        self.write(tmp_path, "b_legacy.json", LEGACY)
        (tmp_path / "c_broken.json").write_text("{not json", encoding="utf-8")
        self.write(tmp_path, "d_list.json", ["a", "list", "payload"])
        (tmp_path / "notes.md").write_text("# ignored", encoding="utf-8")

        rows = mc.load_rows_from_json(tmp_path)

        assert {r["community_name"] for r in rows} == {"Yemenite", "Ashkenazi"}
        assert "Skipping c_broken.json" in capsys.readouterr().out

    def test_community_filter_is_exact_and_case_insensitive(self, tmp_path):
        self.write(tmp_path, "a.json", MODERN)
        self.write(tmp_path, "b.json", LEGACY)

        assert {r["community_name"] for r in mc.load_rows_from_json(tmp_path, " yemenite ")} == {"Yemenite"}
        assert mc.load_rows_from_json(tmp_path, "Yemen") == []

    def test_duplicate_ids_across_files_collapse_last_file_wins(self, tmp_path):
        first = {"name": "C", "halacha_index": [{"topic": "T", "source": "S", "summary": "old"}]}
        second = {"name": "C", "halacha_index": [{"topic": "T", "source": "S", "summary": "new"}]}
        self.write(tmp_path, "1.json", first)
        self.write(tmp_path, "2.json", second)

        rows = mc.load_rows_from_json(tmp_path)

        assert [r["content"] for r in rows] == ["new"]

    def test_rerunning_yields_identical_rows_so_upserts_are_idempotent(self, tmp_path):
        self.write(tmp_path, "a.json", MODERN)

        first_run = mc.load_rows_from_json(tmp_path)
        second_run = mc.load_rows_from_json(tmp_path)

        assert first_run, "the fixture must produce rows, or the comparison below proves nothing"
        assert second_run == first_run

    def test_the_real_customs_directory_parses_into_unique_well_formed_rows(self):
        rows = mc.load_rows_from_json(mc.CUSTOMS_DIR)

        assert len(rows) > 100
        assert len({r["id"] for r in rows}) == len(rows)
        for row in rows:
            assert set(row) == {"id", "community_name", "topic", "halakhic_source", "content"}
            assert all(row[k] for k in row), row


class TestChunked:
    def test_splits_in_order_with_a_short_last_chunk(self):
        assert mc.chunked([{"n": i} for i in range(5)], 2) == [
            [{"n": 0}, {"n": 1}], [{"n": 2}, {"n": 3}], [{"n": 4}]]

    def test_empty_input_is_no_chunks(self):
        assert mc.chunked([], 3) == []


class TestResolveSupabaseConfig:
    @pytest.fixture(autouse=True)
    def _no_dotenv(self, monkeypatch):
        monkeypatch.setattr(mc, "load_dotenv", lambda: None)

    def test_returns_stripped_values(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", " https://p.supabase.co ")
        monkeypatch.setenv("SUPABASE_SECRET_KEY", " sk ")
        assert mc.resolve_supabase_config() == {"url": "https://p.supabase.co", "secret_key": "sk"}

    def test_missing_url_and_missing_key_are_distinct_errors(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.setenv("SUPABASE_SECRET_KEY", "sk")
        with pytest.raises(RuntimeError, match="Missing SUPABASE_URL"):
            mc.resolve_supabase_config()

        monkeypatch.setenv("SUPABASE_URL", "https://p.supabase.co")
        monkeypatch.delenv("SUPABASE_SECRET_KEY")
        with pytest.raises(RuntimeError, match="secret key is required"):
            mc.resolve_supabase_config()


class _FakeTable:
    def __init__(self, client, name):
        self.client, self.name = client, name

    def select(self, columns):
        self.client.events.append(("probe", self.name, columns))
        return self

    def limit(self, n):
        return self

    def upsert(self, batch, on_conflict):
        self.client.events.append(("upsert", self.name, len(batch), on_conflict))
        self.batch = batch
        return self

    def execute(self):
        if self.client.fail_probe and self.client.events[-1][0] == "probe":
            raise RuntimeError("relation does not exist")
        return SimpleNamespace(data=[])


class _FakeClient:
    def __init__(self, fail_probe=False):
        self.events = []
        self.fail_probe = fail_probe

    def table(self, name):
        return _FakeTable(self, name)


ROWS = [{"id": str(i), "community_name": "C", "topic": "T", "halakhic_source": "S", "content": "c"}
        for i in range(5)]


class TestRunMigration:
    @pytest.fixture
    def client(self, monkeypatch):
        fake = _FakeClient()
        monkeypatch.setattr(mc, "create_client", lambda url, key: fake)
        monkeypatch.setattr(mc, "resolve_supabase_config", lambda: {"url": "u", "secret_key": "k"})
        return fake

    def test_dry_run_prints_three_samples_and_never_connects(self, monkeypatch, capsys):
        monkeypatch.setattr(mc, "create_client", lambda *a: pytest.fail("dry run must not connect"))
        monkeypatch.setattr(mc, "resolve_supabase_config",
                            lambda: pytest.fail("dry run must not read credentials"))

        mc.run_migration("community_knowledge", ROWS, dry_run=True, chunk_size=2)

        out = capsys.readouterr().out
        assert "Prepared 5 rows" in out
        assert out.count('"community_name"') == 3

    def test_no_rows_returns_before_reading_credentials(self, monkeypatch, capsys):
        monkeypatch.setattr(mc, "resolve_supabase_config",
                            lambda: pytest.fail("nothing to migrate: no credentials needed"))
        mc.run_migration("t", [], dry_run=False, chunk_size=2)
        assert "Nothing to migrate." in capsys.readouterr().out

    def test_probes_the_table_first_then_upserts_in_batches_keyed_on_id(self, client, capsys):
        mc.run_migration("community_knowledge", ROWS, dry_run=False, chunk_size=2)

        assert client.events == [
            ("probe", "community_knowledge", "id"),
            ("upsert", "community_knowledge", 2, "id"),
            ("upsert", "community_knowledge", 2, "id"),
            ("upsert", "community_knowledge", 1, "id"),
        ]
        out = capsys.readouterr().out
        assert "Upserted batch 3/3 (1 rows)" in out
        assert "Done. Upserted 5 rows" in out

    def test_missing_table_fails_before_any_row_is_written(self, monkeypatch):
        fake = _FakeClient(fail_probe=True)
        monkeypatch.setattr(mc, "create_client", lambda url, key: fake)
        monkeypatch.setattr(mc, "resolve_supabase_config", lambda: {"url": "u", "secret_key": "k"})

        with pytest.raises(RuntimeError, match="relation does not exist"):
            mc.run_migration("nope", ROWS, dry_run=False, chunk_size=2)

        assert all(event[0] == "probe" for event in fake.events)

    def test_nonpositive_chunk_size_is_clamped_to_one_row_per_batch(self, client):
        mc.run_migration("t", ROWS[:2], dry_run=False, chunk_size=0)
        assert [e[2] for e in client.events if e[0] == "upsert"] == [1, 1]


class TestMain:
    def run(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["migrate", *argv])
        calls = {}
        monkeypatch.setattr(mc, "load_rows_from_json",
                            lambda directory, community_filter="": calls.update(
                                directory=directory, filter=community_filter) or ROWS)
        monkeypatch.setattr(mc, "run_migration",
                            lambda table, rows, dry_run, chunk_size, prune_names=None: calls.update(
                                table=table, rows=rows, dry_run=dry_run, chunk_size=chunk_size,
                                prune_names=prune_names))
        mc.main()
        return calls

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", raising=False)
        calls = self.run(monkeypatch)
        assert calls["table"] == "community_knowledge"
        assert (calls["dry_run"], calls["chunk_size"], calls["filter"]) == (False, 250, "")
        assert calls["directory"] == mc.CUSTOMS_DIR
        assert calls["prune_names"] is None, "pruning deletes rows, so it must be opt-in"

    def test_prune_flag_passes_the_names_it_may_delete_from(self, monkeypatch):
        calls = self.run(monkeypatch, "--prune")
        assert {"Yemenite", "Sefardic", "Kafkazi"} <= set(calls["prune_names"])

    def test_flags_are_forwarded(self, monkeypatch):
        calls = self.run(monkeypatch, "--table", "kb2", "--community", "Persian",
                         "--chunk-size", "10", "--dry-run")
        assert (calls["table"], calls["filter"], calls["chunk_size"], calls["dry_run"]) == (
            "kb2", "Persian", 10, True)


def test_script_entry_point_runs_a_dry_run_against_the_real_customs(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["migrate", "--dry-run", "--community", "Yemenite"])
    with patch("dotenv.load_dotenv"):
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")
    out = capsys.readouterr().out
    assert "Dry run enabled" in out
    assert "Yemenite" in out


# ─── 3.0 data: variants, confidence, lens key, distinctive customs ──────────────

MODERN_3 = {
    "name": "Sephardi",
    "runtime": {"lens_key": "Sefardic"},
    "halacha_index": [
        {
            "topic": "Kitniyot", "source": "Shulchan Aruch OC 453", "summary": "Permitted.",
            "common_practices": ["Rice is eaten"],
            "variants": [
                {"subgroup": "Turkish", "practice": "No rice."},
                {"subgroup": "", "practice": "Local custom varies."},
                {"subgroup": "Empty", "practice": ""},
                "not a dict",
            ],
            "confidence": "disputed",
            "review_notes": "Page not opened.",
        },
        {"topic": "Kiddush", "source": "S", "summary": "Standing.", "confidence": "well-attested"},
    ],
    "unique_minhagim": [
        {"name": "Meldado", "description": "Memorial gathering.", "when": "Seven months", "source": "Molho",
         "confidence": "needs-review"},
        {"name": "", "description": "no name"},
        {"name": "No description", "description": ""},
        "not a dict",
    ],
}


class TestThreeZeroContent:
    def test_variants_and_confidence_reach_the_content(self):
        content = mc._build_content("S.", ["a"], None, variants=[{"subgroup": "G", "practice": "x"}],
                                    confidence="disputed")
        assert content.split("\n") == [
            "S.",
            "Confidence: disputed. Rabbis differ; give each side and tell the user to ask their own rabbi.",
            "Variants: G: x",
            "Common practices: a",
        ]

    def test_the_caveat_comes_before_the_long_parts_so_a_prompt_cap_cannot_cut_it(self):
        content = mc._build_content("S." * 200, ["p" * 200] * 6, None,
                                    variants=[{"subgroup": "g", "practice": "v" * 200}], confidence="disputed")
        assert content.index("Confidence: disputed") < 420

    def test_every_caveat_sends_the_user_to_their_own_rabbi(self):
        for confidence in ("disputed", "regional", "needs-review"):
            assert "own rabbi" in mc._CONFIDENCE_NOTES[confidence], confidence

    def test_variants_come_before_common_practices_so_both_sides_survive_the_prompt_cap(self):
        content = mc._build_content("S.", ["p" * 200] * 6, None,
                                    variants=[{"subgroup": "A", "practice": "yes"}, {"subgroup": "B", "practice": "no"}],
                                    confidence="disputed")
        assert content.index("Variants: A: yes | B: no") < content.index("Common practices:")
        assert content.index("Variants: A: yes | B: no") < 250

    def test_well_attested_adds_no_caveat_and_unknown_confidence_is_ignored(self):
        assert mc._build_content("S.", None, None, confidence="well-attested") == "S."
        assert mc._build_content("S.", None, None, confidence="bogus") == "S."
        assert mc._build_content("S.", None, None, confidence=None) == "S."

    def test_variants_are_capped_and_malformed_ones_dropped(self):
        variants = [{"subgroup": f"g{i}", "practice": f"p{i}"} for i in range(9)] + ["x", {"practice": ""}]
        line = mc._build_content("S.", None, None, variants=variants).split("\n")[1]
        assert line == "Variants: g0: p0 | g1: p1 | g2: p2 | g3: p3"

    def test_review_notes_never_reach_a_row(self):
        rows = mc._parse_modern_payload(MODERN_3)
        assert all("Page not opened" not in row["content"] for row in rows)

    def test_community_name_is_the_lens_key_not_the_display_name(self):
        rows = mc._parse_modern_payload(MODERN_3)
        assert {row["community_name"] for row in rows} == {"Sefardic"}

    def test_community_name_falls_back_to_name_without_a_lens_key(self):
        assert mc._community_key({"name": "Yemenite"}) == "Yemenite"
        assert mc._community_key({"name": "Yemenite", "runtime": {}}) == "Yemenite"
        assert mc._community_key({"runtime": "not a dict", "heritage_id": "h"}) == "h"

    def test_each_distinctive_custom_becomes_its_own_row(self):
        rows = {r["topic"]: r for r in mc._parse_modern_payload(MODERN_3)}
        row = rows["Distinctive custom: Meldado"]
        assert row["halakhic_source"] == "Molho"
        assert "Memorial gathering." in row["content"]
        assert "When: Seven months" in row["content"]
        assert "Confidence: needs review" in row["content"]
        assert len([t for t in rows if t.startswith("Distinctive custom:")]) == 1

    def test_the_legacy_examples_dict_makes_no_distinctive_rows(self):
        payload = {"name": "C", "halacha_index": [], "unique_minhagim": {"examples": ["a"], "notes": "n"}}
        assert mc._parse_modern_payload(payload) == []

    def test_later_poskim_is_a_source_fallback(self):
        payload = {"name": "C", "core_halachic_authorities": {"later_poskim": ["R. X"]},
                   "halacha_index": [{"topic": "T", "summary": "s"}]}
        assert mc._parse_modern_payload(payload)[0]["halakhic_source"] == "R. X"


class TestNonCommunityFiles:
    def test_schema_and_the_retired_aggregate_are_never_read(self, tmp_path):
        (tmp_path / "schema.json").write_text(json.dumps(MODERN_3), encoding="utf-8")
        (tmp_path / "customs_db.json").write_text(json.dumps(LEGACY), encoding="utf-8")
        (tmp_path / "real.json").write_text(json.dumps(MODERN), encoding="utf-8")
        assert {r["community_name"] for r in mc.load_rows_from_json(tmp_path)} == {"Yemenite"}

    def test_every_real_community_file_declares_a_lens_key(self):
        for path in sorted(mc.CUSTOMS_DIR.glob("*.json")):
            if path.name in mc.SKIPPED_FILES:
                continue
            runtime = json.loads(path.read_text(encoding="utf-8")).get("runtime") or {}
            assert runtime.get("lens_key"), f"{path.name} has no runtime.lens_key"


class _PruneTable:
    def __init__(self, client):
        self.client, self.name, self.filters = client, None, {}

    def select(self, columns):
        self.op = "select"
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, column, value):
        self.filters["community_name"] = value
        return self

    def in_(self, column, values):
        self.filters["ids"] = list(values)
        return self

    def range(self, start, end):
        self.filters["range"] = (start, end)
        return self

    def execute(self):
        if self.op == "delete":
            self.client.deleted.extend(self.filters["ids"])
            return SimpleNamespace(data=[])
        start, end = self.filters["range"]
        ids = self.client.existing.get(self.filters["community_name"], [])
        return SimpleNamespace(data=[{"id": i} for i in ids[start:end + 1]])


class _PruneClient:
    def __init__(self, existing):
        self.existing, self.deleted = existing, []

    def table(self, name):
        return _PruneTable(self)


class TestPruneStaleRows:
    ROWS = [{"id": "keep1"}, {"id": "keep2"}]

    def test_deletes_only_ids_missing_from_the_json_under_the_given_names(self):
        client = _PruneClient({"Yemenite": ["keep1", "old1", "old2"], "Other": ["x"]})
        removed = mc.prune_stale_rows(client, "t", self.ROWS, ["Yemenite"])
        assert removed == 2
        assert sorted(client.deleted) == ["old1", "old2"]

    def test_pages_through_more_rows_than_one_page(self):
        client = _PruneClient({"Y": [f"old{i}" for i in range(7)] + ["keep1"]})
        assert mc.prune_stale_rows(client, "t", self.ROWS, ["Y"], page_size=3) == 7
        assert len(client.deleted) == 7

    def test_nothing_stale_deletes_nothing(self):
        client = _PruneClient({"Y": ["keep1", "keep2"]})
        assert mc.prune_stale_rows(client, "t", self.ROWS, ["Y"]) == 0
        assert client.deleted == []

    def test_deletes_in_batches_of_200(self):
        client = _PruneClient({"Y": [f"old{i}" for i in range(450)]})
        assert mc.prune_stale_rows(client, "t", self.ROWS, ["Y"], page_size=1000) == 450

    def test_run_migration_prunes_only_after_every_upsert_succeeded(self, monkeypatch, capsys):
        events = []

        class Client:
            def table(self, name):
                return _FakeTable(self_client, name)

        self_client = _FakeClient()
        monkeypatch.setattr(mc, "create_client", lambda url, key: self_client)
        monkeypatch.setattr(mc, "resolve_supabase_config", lambda: {"url": "u", "secret_key": "k"})
        monkeypatch.setattr(mc, "prune_stale_rows",
                            lambda client, table, rows, names: events.append(
                                ("prune", [e[0] for e in self_client.events])) or 3)

        mc.run_migration("t", ROWS, dry_run=False, chunk_size=10, prune_names=["C"])

        assert events == [("prune", ["probe", "upsert"])]
        assert "Removed 3 stale row(s)" in capsys.readouterr().out

    def test_a_failed_probe_means_nothing_is_pruned(self, monkeypatch):
        fake = _FakeClient(fail_probe=True)
        monkeypatch.setattr(mc, "create_client", lambda url, key: fake)
        monkeypatch.setattr(mc, "resolve_supabase_config", lambda: {"url": "u", "secret_key": "k"})
        monkeypatch.setattr(mc, "prune_stale_rows", lambda *a: pytest.fail("must not prune"))
        with pytest.raises(RuntimeError):
            mc.run_migration("t", ROWS, dry_run=False, chunk_size=2, prune_names=["C"])

    def test_dry_run_reports_the_prune_names_without_connecting(self, monkeypatch, capsys):
        monkeypatch.setattr(mc, "create_client", lambda *a: pytest.fail("dry run must not connect"))
        mc.run_migration("t", ROWS, dry_run=True, chunk_size=2, prune_names=["A", "B"])
        assert "--prune would clear stale rows under: A, B" in capsys.readouterr().out


class TestKnownCommunityNames:
    def test_collects_lens_key_name_and_the_retired_names(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(MODERN_3), encoding="utf-8")
        (tmp_path / "schema.json").write_text(json.dumps(MODERN), encoding="utf-8")
        (tmp_path / "bad.json").write_text("{nope", encoding="utf-8")
        (tmp_path / "list.json").write_text("[1]", encoding="utf-8")
        names = mc.known_community_names(tmp_path)
        assert names == sorted({"Sefardic", "Sephardi", *mc.LEGACY_COMMUNITY_NAMES})

    def test_a_community_filter_leaves_out_the_retired_names(self, tmp_path):
        (tmp_path / "a.json").write_text(json.dumps(MODERN_3), encoding="utf-8")
        (tmp_path / "b.json").write_text(json.dumps(MODERN), encoding="utf-8")
        assert mc.known_community_names(tmp_path, " sefardic ") == ["Sefardic", "Sephardi"]


class TestEmitSql:
    def test_build_sql_is_one_transaction_that_deletes_then_upserts(self):
        rows = [{"id": "i1", "community_name": "C", "topic": "It's", "halakhic_source": "S", "content": "a\nb"}]
        sql = mc.build_sql("community_knowledge", rows, ["C", "Old"])
        assert sql.startswith("begin;\n") and sql.endswith("commit;\n")
        assert "delete from public.community_knowledge where community_name in ('C', 'Old');" in sql
        assert "'It''s'" in sql, "single quotes must be doubled"
        assert "on conflict (id) do update set" in sql
        assert sql.index("delete from") < sql.index("insert into")

    def test_build_sql_rejects_an_unsafe_table_name(self):
        with pytest.raises(ValueError):
            mc.build_sql("t; drop table users", ROWS, [])

    def test_emit_writes_one_file_per_community_plus_the_retired_cleanup(self, tmp_path):
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        (src / "a.json").write_text(json.dumps(MODERN_3), encoding="utf-8")
        (src / "b.json").write_text(json.dumps(MODERN), encoding="utf-8")

        written = mc.emit_sql(src, "community_knowledge", out)

        assert [p.name for p in written] == [
            "00_retired_customs_db_cleanup.sql", "01_sefardic.sql", "02_yemenite.sql"]
        cleanup = written[0].read_text(encoding="utf-8")
        assert "'Kafkazi'" in cleanup and "insert into" not in cleanup
        sefardic = written[1].read_text(encoding="utf-8")
        assert "community_name in ('Sefardic', 'Sephardi')" in sefardic
        assert "'Sephardic'" not in sefardic, "a community file must not delete another community's rows"

    def test_emit_for_one_community_skips_the_retired_cleanup(self, tmp_path):
        src, out = tmp_path / "src", tmp_path / "out"
        src.mkdir()
        (src / "a.json").write_text(json.dumps(MODERN_3), encoding="utf-8")
        written = mc.emit_sql(src, "t", out, community_filter="Sefardic")
        assert [p.name for p in written] == ["01_sefardic.sql"]

    def test_main_emit_sql_never_connects(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(mc, "create_client", lambda *a: pytest.fail("--emit-sql must not connect"))
        monkeypatch.setattr(sys, "argv", ["migrate", "--emit-sql", str(tmp_path / "sql")])
        mc.main()
        assert "Wrote" in capsys.readouterr().out
        assert len(list((tmp_path / "sql").glob("*.sql"))) == 14
