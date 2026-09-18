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
        # Parts are joined with "\n" and then whitespace-normalised, so the
        # stored content is a single line with the parts separated by a space.
        content = mc._build_content("Summary.", ["a", "b", "c", "d", "e"], "Careful.")
        assert content == "Summary. Common practices: a | b | c | d Notes: Careful."

    def test_blank_practices_are_dropped_and_non_lists_ignored(self):
        assert mc._build_content("S", ["", "  ", "x"], None) == "S Common practices: x"
        assert mc._build_content("S", "not a list", None) == "S"

    def test_everything_empty_gives_empty_content(self):
        assert mc._build_content("", [], "") == ""
        assert mc._build_content(None, None, None) == ""

    def test_overlong_content_is_capped_at_2200_chars_plus_ellipsis(self):
        content = mc._build_content("x" * 2000, ["y" * 180] * 4, "z" * 2000)
        assert len(content) <= 2203
        assert content.endswith("...")


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
        assert rows[0]["content"] == "Standing. Notes: n"

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
        assert row["content"] == "Avoid. Common practices: rice"
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
        assert mc.load_rows_from_json(tmp_path) == mc.load_rows_from_json(tmp_path)

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
                            lambda table, rows, dry_run, chunk_size: calls.update(
                                table=table, rows=rows, dry_run=dry_run, chunk_size=chunk_size))
        mc.main()
        return calls

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", raising=False)
        calls = self.run(monkeypatch)
        assert calls["table"] == "community_knowledge"
        assert (calls["dry_run"], calls["chunk_size"], calls["filter"]) == (False, 250, "")
        assert calls["directory"] == mc.CUSTOMS_DIR

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
