"""Tests for scripts/verify_library_removals.py.

The script decides which of the April crawl's removals come back into the
library (reports/library_leaf_reinstated.json): a work with a complex
schema and real text in Sefaria-Export, never one whose schema is simple
(the crawl's bare-title probe was the right shape there), missing, or
textless. Pinned against a fake export."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "verify_library_removals.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("verify_library_removals", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vr = _load_script()

COMPLEX = {"title": "Siddur Sefard", "schema": {
    "titles": [{"lang": "en", "text": "Siddur Sefard"}], "key": "Siddur Sefard",
    "nodes": [{"titles": [{"lang": "en", "text": "Upon Arising"}], "key": "Upon Arising",
               "nodes": [{"titles": [{"lang": "en", "text": "Modeh Ani"}], "key": "Modeh Ani"}]}],
}}


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeExport:
    """Schemas by title; text files by listing prefix or by title (glob)."""

    def __init__(self, schemas=None, by_prefix=None, by_glob=None, listing_status=200):
        self.schemas = schemas or {}
        self.by_prefix = by_prefix or {}
        self.by_glob = by_glob or {}
        self.listing_status = listing_status
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url == vr.EXPORT_LISTING:
            if self.listing_status != 200:
                return _Resp(self.listing_status)
            if "matchGlob" in params:
                title = params["matchGlob"][len("json/**/"):-len("/**")]
                return _Resp(200, {"items": self.by_glob.get(title, [])})
            return _Resp(200, {"items": self.by_prefix.get(params["prefix"], [])})
        for title, schema in self.schemas.items():
            if url == vr.schema_url(title):
                return _Resp(200, schema)
        return _Resp(404)


ROW = {"title": "Siddur Sefard", "categories": ["Liturgy", "Siddur"]}
PREFIX = "json/Liturgy/Siddur/Siddur Sefard/"


class TestPaths:
    def test_schema_url_uses_underscores_and_escapes_the_rest(self):
        assert vr.schema_url("Kinnot for Tisha B'Av (Ashkenaz)") == (
            f"{vr.EXPORT_BASE}/schemas/Kinnot_for_Tisha_B%27Av_%28Ashkenaz%29.json")

    def test_text_prefix_drops_a_trailing_category_named_for_the_work(self):
        assert vr.text_prefix(ROW) == PREFIX
        assert vr.text_prefix({"title": "Bekhor Shor", "categories": ["Tanakh", "Rishonim on Tanakh", "Bekhor Shor"]}) \
            == "json/Tanakh/Rishonim on Tanakh/Bekhor Shor/"
        assert vr.text_prefix({"title": "X"}) == "json/X/"


class TestVerifyRow:
    def test_a_complex_work_with_text_is_reinstated_with_its_opening_ref(self):
        export = FakeExport(schemas={"Siddur Sefard": COMPLEX},
                            by_prefix={PREFIX: [{"name": "a", "size": "900"}, {"name": "b", "size": "250000"}]})
        assert vr.verify_row(ROW, export) == {
            "title": "Siddur Sefard", "reinstate": True,
            "opening_ref": "Siddur Sefard, Upon Arising, Modeh Ani", "text_bytes": 250000,
        }

    def test_text_filed_under_other_categories_is_found_by_title(self):
        export = FakeExport(schemas={"Siddur Sefard": COMPLEX},
                            by_glob={"Siddur Sefard": [{"name": "x", "size": 5000}]})
        assert vr.verify_row(ROW, export)["reinstate"] is True

    @pytest.mark.parametrize("export, why", [
        (FakeExport(), "no schema in the export"),
        (FakeExport(schemas={"Siddur Sefard": {"title": "Siddur Sefard", "schema": {"depth": 2}}}),
         "simple schema; the crawl's probe was the right shape"),
        (FakeExport(schemas={"Siddur Sefard": {"title": "Siddur Sefard"}}),
         "simple schema; the crawl's probe was the right shape"),
        (FakeExport(schemas={"Siddur Sefard": ["not", "a", "dict"]}), "no schema in the export"),
        (FakeExport(schemas={"Siddur Sefard": COMPLEX}), "no text in the export"),
        (FakeExport(schemas={"Siddur Sefard": COMPLEX}, by_prefix={PREFIX: [{"name": "meta", "size": "300"}]}),
         "no text in the export"),
    ])
    def test_everything_else_stays_removed(self, export, why):
        assert vr.verify_row(ROW, export) == {"title": "Siddur Sefard", "reinstate": False, "why": why}

    def test_a_failed_listing_raises_rather_than_passing_for_no_text(self):
        export = FakeExport(schemas={"Siddur Sefard": COMPLEX}, listing_status=503)
        with pytest.raises(requests.HTTPError):
            vr.verify_row(ROW, export)


REPORT = {
    "generated_at_utc": "2026-04-21T01:44:51+00:00",
    "removals": [ROW, {"title": "Jastrow", "categories": ["Reference", "Dictionary"]}, {"title": ""}, "junk"],
}


class TestRunAndMain:
    def export(self):
        return FakeExport(schemas={"Siddur Sefard": COMPLEX, "Jastrow": COMPLEX},
                          by_prefix={PREFIX: [{"name": "a", "size": 9000}]})

    def test_run_ties_the_result_to_the_report_run(self):
        now = datetime(2026, 9, 27, tzinfo=timezone.utc)
        payload = vr.run(REPORT, self.export(), workers=2, now=lambda: now)

        assert payload["report_generated_at_utc"] == REPORT["generated_at_utc"]
        assert payload["generated_at_utc"] == now.isoformat()
        assert payload["stats"] == {"removals": 2, "reinstated": 1, "still_removed": 1}
        assert payload["reinstated"] == [{"title": "Siddur Sefard", "opening_ref": "Siddur Sefard, Upon Arising, Modeh Ani",
                                          "text_bytes": 9000}]
        assert payload["still_removed"] == [{"title": "Jastrow", "why": "no text in the export"}]
        assert payload["machine_generated"] is True

    def test_main_writes_the_file(self, tmp_path, capsys):
        report = tmp_path / "report.json"
        report.write_text(json.dumps(REPORT), encoding="utf-8")
        out = tmp_path / "out.json"

        assert vr.main([], session=self.export(), report_path=report, output_path=out) == 0

        written = json.loads(out.read_text(encoding="utf-8"))
        assert written["stats"]["reinstated"] == 1
        assert "1 of 2 removals reinstated" in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["--report", "--output"])
    def test_main_takes_no_path_from_the_command_line(self, tmp_path, flag):
        # The command line cannot name a file to read or write: the paths are
        # fixed in the script, so a stray flag is refused rather than followed.
        elsewhere = tmp_path / "elsewhere.json"

        with pytest.raises(SystemExit):
            vr.main([flag, str(elsewhere)], session=self.export())

        assert not elsewhere.exists()
