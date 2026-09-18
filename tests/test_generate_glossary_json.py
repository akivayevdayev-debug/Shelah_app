"""Tests for scripts/generate_glossary_json.py.

The rule the generator exists to enforce: a definition is taken from the live
lexicon only when the lookup reports a real lexicon source; a low-confidence
machine translation, an empty answer, or a lookup that raises must fall back to
the hand-written gloss. Also pins that the committed static/data/glossary.json
stays in step with GLOSSARY_TERMS, since /glossary serves that file verbatim.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "generate_glossary_json.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("generate_glossary_json", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gg = _load_script()


class TestIsTrusted:
    @pytest.mark.parametrize("source", [
        "local-hebrew-glossary", "Sefaria Lexicon", "BDB Augmented Strong",
        "Jastrow Dictionary", "Brown-Driver-Briggs", "JASTROW",
    ])
    def test_real_lexicon_sources_are_trusted_case_insensitively(self, source):
        assert gg._is_trusted(source)

    @pytest.mark.parametrize("source", ["automatic-translation", "", None, "google-translate"])
    def test_guesses_and_blanks_are_not_trusted(self, source):
        assert not gg._is_trusted(source)


class TestBuildGlossary:
    def test_trusted_lexicon_hit_replaces_the_curated_gloss_and_records_its_source(self, monkeypatch):
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning",
                            lambda word: (f"lexicon says {word}", "sefaria-lexicon"))

        entries = gg.build_glossary()

        assert {e["source"] for e in entries} == {"sefaria-lexicon"}
        kezayit = entries[0]
        assert kezayit["term_en"] == "Kezayit"
        assert kezayit["definition"] == "lexicon says כזית"

    def test_low_confidence_translation_is_rejected_for_the_curated_gloss(self, monkeypatch):
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning",
                            lambda word: ("machine guess", "automatic-translation"))

        entries = gg.build_glossary()

        assert all(e["source"] == "curated" for e in entries)
        assert entries[0]["definition"] == gg.GLOSSARY_TERMS[0][2]
        assert all(e["definition"] != "machine guess" for e in entries)

    @pytest.mark.parametrize("result", [("", "sefaria"), (None, "sefaria")])
    def test_empty_definition_falls_back_even_from_a_trusted_source(self, monkeypatch, result):
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", lambda word: result)
        assert all(e["source"] == "curated" for e in gg.build_glossary())

    def test_a_lookup_that_raises_falls_back_instead_of_aborting_the_build(self, monkeypatch):
        def boom(word):
            raise ConnectionError("sefaria unreachable")

        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", boom)

        entries = gg.build_glossary()

        assert len(entries) == len(gg.GLOSSARY_TERMS)
        assert all(e["source"] == "curated" for e in entries)

    def test_one_failing_term_does_not_affect_the_others(self, monkeypatch):
        def flaky(word):
            if word == "כזית":
                raise TimeoutError
            return ("ok", "bdb")

        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", flaky)

        entries = gg.build_glossary()

        assert entries[0]["source"] == "curated"
        assert all(e["source"] == "bdb" for e in entries[1:])

    def test_output_preserves_term_order_and_shape(self, monkeypatch):
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", lambda word: ("", ""))

        entries = gg.build_glossary()

        assert [e["term_en"] for e in entries] == [t[0] for t in gg.GLOSSARY_TERMS]
        assert all(set(e) == {"term_en", "term_he", "definition", "source"} for e in entries)


class TestCuratedData:
    def test_terms_are_unique_and_fully_populated(self):
        english = [t[0] for t in gg.GLOSSARY_TERMS]
        hebrew = [t[1] for t in gg.GLOSSARY_TERMS]
        assert len(set(english)) == len(english)
        assert len(set(hebrew)) == len(hebrew)
        assert all(en.strip() and he.strip() and gloss.strip() for en, he, gloss in gg.GLOSSARY_TERMS)

    def test_committed_glossary_json_lists_exactly_the_scripts_terms(self):
        committed = json.loads((REPO_ROOT / "static" / "data" / "glossary.json").read_text(encoding="utf-8"))
        assert [(e["term_en"], e["term_he"]) for e in committed] == [(t[0], t[1]) for t in gg.GLOSSARY_TERMS]


class TestMain:
    def test_writes_utf8_json_under_static_data_and_reports_the_count(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(gg, "__file__", str(tmp_path / "scripts" / "generate_glossary_json.py"))
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", lambda word: ("", ""))

        gg.main()

        out_path = tmp_path / "static" / "data" / "glossary.json"
        raw = out_path.read_text(encoding="utf-8")
        assert raw.endswith("]\n")
        assert "כזית" in raw, "Hebrew must be written as-is, not \\u-escaped"
        assert len(json.loads(raw)) == len(gg.GLOSSARY_TERMS)
        assert f"Wrote {len(gg.GLOSSARY_TERMS)} glossary entries to {out_path}" in capsys.readouterr().out

    def test_overwrites_a_stale_file_rather_than_appending(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gg, "__file__", str(tmp_path / "scripts" / "generate_glossary_json.py"))
        monkeypatch.setattr(gg, "_lookup_hebrew_word_meaning", lambda word: ("", ""))
        out_path = tmp_path / "static" / "data" / "glossary.json"
        out_path.parent.mkdir(parents=True)
        out_path.write_text("[\"stale\"]", encoding="utf-8")

        gg.main()

        assert "stale" not in out_path.read_text(encoding="utf-8")


def test_script_entry_point_runs_main(monkeypatch, tmp_path, capsys):
    import runpy

    import backend.helpers as helpers

    monkeypatch.setattr(helpers, "_lookup_hebrew_word_meaning", lambda word: ("", ""))
    # runpy.run_path executes the file fresh with __file__ pointing at the real
    # script, so redirect the one write it makes.
    real_open = open
    written = {}

    def fake_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            written["path"] = str(path)
            return real_open(tmp_path / "out.json", mode, *args, **kwargs)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    monkeypatch.setattr("os.makedirs", lambda *a, **k: None)

    runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    assert written["path"].endswith("static/data/glossary.json")
    assert (tmp_path / "out.json").exists()
    assert "glossary entries" in capsys.readouterr().out
