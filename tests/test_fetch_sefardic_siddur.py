"""Tests for scripts/fetch_sefardic_siddur.py.

The script downloads the Siddur Sefard from Sefaria and emits a Python literal
(`PRAYERS_DATA = {...}`) meant to be pasted into source. The properties that
matter: text extraction copes with both list and string payloads, short/empty
text falls back to a description instead of shipping a blank prayer, and the
generated literal is valid Python that round-trips arbitrary text (quotes,
backslashes, newlines) exactly.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
from pathlib import Path

import pytest
import requests

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "fetch_sefardic_siddur.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("fetch_sefardic_siddur", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fs = _load_script()

LONG_EN = "Blessed are You, Lord our God, King of the universe, who has sanctified us."
LONG_HE = "בָּרוּךְ אַתָּה יְיָ אֱלֹהֵינוּ מֶלֶךְ הָעוֹלָם אֲשֶׁר קִדְּשָׁנוּ בְּמִצְוֺתָיו"


class TestFetchFromSefaria:
    def test_requests_the_url_with_spaces_percent_encoded(self, monkeypatch):
        seen = {}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        def fake_get(url, timeout):
            seen["url"], seen["timeout"] = url, timeout
            return _Resp()

        monkeypatch.setattr(fs.requests, "get", fake_get)

        assert fs.fetch_from_sefaria("Siddur Sefard, Kiddush") == {"ok": True}
        assert seen["url"] == "https://www.sefaria.org/api/texts/Siddur%20Sefard,%20Kiddush"
        assert seen["timeout"] == 10

    def test_http_errors_are_swallowed_into_none_and_reported(self, monkeypatch, capsys):
        class _Resp:
            def raise_for_status(self):
                raise requests.HTTPError("404")

        monkeypatch.setattr(fs.requests, "get", lambda url, timeout: _Resp())

        assert fs.fetch_from_sefaria("Nope") is None
        assert "Error fetching Nope" in capsys.readouterr().out

    def test_network_errors_are_swallowed_into_none(self, monkeypatch):
        def boom(url, timeout):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(fs.requests, "get", boom)
        assert fs.fetch_from_sefaria("Anything") is None


class TestExtractText:
    def test_none_yields_two_nones(self):
        assert fs.extract_text(None) == (None, None)
        assert fs.extract_text({}) == (None, None)

    def test_list_payloads_are_stripped_joined_and_blanks_dropped(self):
        he, en = fs.extract_text({"he": [" א ", "", "ב"], "text": [" one ", None, "two "]})
        assert he == "א ב"
        assert en == "one two"

    def test_string_payloads_are_stripped(self):
        he, en = fs.extract_text({"he": "  שלום  ", "text": "  peace "})
        assert (he, en) == ("שלום", "peace")

    def test_missing_or_empty_language_is_an_empty_string(self):
        assert fs.extract_text({"he": [], "text": None}) == ("", "")


class TestFetchAllPrayers:
    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        self.sleeps = []
        monkeypatch.setattr(fs.time, "sleep", self.sleeps.append)

    def test_full_text_is_kept_and_truncated_to_2000_chars(self, monkeypatch):
        monkeypatch.setattr(fs, "PRAYER_SERVICES", [("Kiddush", "Siddur Sefard, Kiddush")])
        monkeypatch.setattr(fs, "fetch_from_sefaria",
                            lambda ref: {"he": "א" * 3000, "text": "e" * 2500})

        prayers = fs.fetch_all_prayers()

        assert set(prayers) == {"Kiddush"}
        assert len(prayers["Kiddush"]["en"]) == 2000
        assert len(prayers["Kiddush"]["he"]) == 2000
        assert set(prayers["Kiddush"]) == {"en", "he", "ar", "ru"}

    def test_short_or_missing_text_falls_back_to_a_description_and_hebrew_title(self, monkeypatch):
        monkeypatch.setattr(fs, "PRAYER_SERVICES", [("Kiddush", "r1"), ("Custom Service", "r2")])
        monkeypatch.setattr(fs, "fetch_from_sefaria", lambda ref: {"he": "קצר", "text": "short"})

        prayers = fs.fetch_all_prayers()

        assert prayers["Kiddush"]["en"] == "Blessing over wine on Shabbat and holidays to sanctify the day."
        assert prayers["Kiddush"]["he"] == "סידור ספרד - Kiddush"
        assert prayers["Custom Service"]["en"] == "Custom Service from Siddur Sefard (Sefardic Prayer Book)"

    def test_real_text_is_not_replaced_by_the_fallback(self, monkeypatch):
        monkeypatch.setattr(fs, "PRAYER_SERVICES", [("Kiddush", "r1")])
        monkeypatch.setattr(fs, "fetch_from_sefaria", lambda ref: {"he": LONG_HE, "text": LONG_EN})

        prayers = fs.fetch_all_prayers()

        assert prayers["Kiddush"]["en"] == LONG_EN
        assert prayers["Kiddush"]["he"] == LONG_HE

    def test_a_failed_fetch_omits_that_prayer_but_continues_with_the_rest(self, monkeypatch, capsys):
        monkeypatch.setattr(fs, "PRAYER_SERVICES", [("Kiddush", "bad"), ("Bedtime Shema", "good")])
        monkeypatch.setattr(fs, "fetch_from_sefaria",
                            lambda ref: None if ref == "bad" else {"he": LONG_HE, "text": LONG_EN})

        prayers = fs.fetch_all_prayers()

        assert list(prayers) == ["Bedtime Shema"]
        assert "Failed to fetch Kiddush" in capsys.readouterr().out

    def test_it_rate_limits_by_sleeping_once_per_prayer_including_failures(self, monkeypatch):
        monkeypatch.setattr(fs, "PRAYER_SERVICES", [("A", "1"), ("B", "2"), ("C", "3")])
        monkeypatch.setattr(fs, "fetch_from_sefaria", lambda ref: None)

        fs.fetch_all_prayers()

        assert self.sleeps == [1, 1, 1]

    def test_every_configured_service_has_a_curated_fallback_description(self, monkeypatch):
        # A service missing from the curated table would ship the generic
        # "<name> from Siddur Sefard" line whenever Sefaria returns little text.
        monkeypatch.setattr(fs, "fetch_from_sefaria", lambda ref: {"he": "", "text": ""})

        prayers = fs.fetch_all_prayers()

        assert len(prayers) == len(fs.PRAYER_SERVICES) == 10
        for name, texts in prayers.items():
            assert texts["en"] != f"{name} from Siddur Sefard (Sefardic Prayer Book)", name


class TestGeneratePythonDict:
    def _exec(self, code):
        namespace = {}
        exec(code, namespace)  # noqa: S102 - generated literal from our own function, under test
        return namespace["PRAYERS_DATA"]

    def test_output_is_valid_python_that_round_trips_awkward_text_exactly(self):
        awkward = 'He said "amen"\\ then\nleft \\n literally'
        prayers = {"Kiddush": {"en": awkward, "he": LONG_HE, "ar": "a", "ru": "р"}}

        assert self._exec(fs.generate_python_dict(prayers)) == prayers

    def test_entries_are_sorted_by_prayer_name(self):
        entry = {"en": "e", "he": "h", "ar": "a", "ru": "r"}
        code = fs.generate_python_dict({"Zed": entry, "Alpha": entry})

        assert code.index('"Alpha"') < code.index('"Zed"')
        assert list(self._exec(code)) == ["Alpha", "Zed"]

    def test_empty_input_is_an_empty_dict_literal(self):
        assert self._exec(fs.generate_python_dict({})) == {}


def test_running_the_script_writes_both_output_files(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "get", lambda url, timeout: (_ for _ in ()).throw(requests.ConnectionError("offline")))
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    assert (tmp_path / "sefardic_prayers.py").read_text(encoding="utf-8").startswith("PRAYERS_DATA = {")
    assert json.loads((tmp_path / "sefardic_prayers.json").read_text(encoding="utf-8")) == {}


def test_running_the_script_with_data_lists_every_prayer_in_the_summary(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"he": LONG_HE, "text": LONG_EN}

    monkeypatch.setattr(requests, "get", lambda url, timeout: _Resp())
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    out = capsys.readouterr().out
    assert "Successfully fetched 10 prayer services" in out
    assert f"• Kiddush: {len(LONG_EN)} chars (EN), {len(LONG_HE)} chars (HE)" in out
    saved = json.loads((tmp_path / "sefardic_prayers.json").read_text(encoding="utf-8"))
    assert len(saved) == 10
