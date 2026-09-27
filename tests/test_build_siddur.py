"""scripts/build_siddur.py (Sefaria-Export -> data/siddur/) and the integrity
of the snapshot it checked in."""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from backend.siddur_lines import LINE_TYPES, WHEN_KEYS

ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("build_siddur", ROOT / "scripts" / "build_siddur.py")
build_siddur = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(build_siddur)

DATA = ROOT / "data" / "siddur" / "edot-hamizrach"


def _sources(he_license="CC0", en_license="CC0", en_morning=None):
    schema = {"schema": {"key": "Siddur Edot HaMizrach", "nodes": [
        {"key": "Weekday Shacharit", "heTitle": "שחרית לימי החול", "nodes": [
            {"key": "The Shema", "title": "The Shema", "heTitle": "ק\"ש וברכותיה"},
            {"key": "Amida", "titles": [{"lang": "en", "primary": True, "text": "Amida"},
                                        {"lang": "he", "primary": True, "text": "עמידה"}]},
        ]},
        {"key": "Bedtime Shema", "title": "Bedtime Shema", "heTitle": "קריאת שמע שעל המיטה"},
    ]}}
    he = {"versionTitle": " Shaliehsaboo Edition", "license": he_license, "text": {
        "Weekday Shacharit": {"The Shema": ["<big>קריאת שמע</big>", "שְׁמַע יִשְׂרָאֵל"],
                              "Amida": ["<small>בקיץ:</small>", "בָּרְכֵנוּ"]},
        "Bedtime Shema": ["הַמַּפִּיל"],
    }}
    en = {"versionTitle": "Sefaria Community Translation", "license": en_license, "text": {
        "Weekday Shacharit": {"The Shema": ["Shema", "Hear, Israel"],
                              "Amida": en_morning if en_morning is not None else ["[In Summer]", "Bless us"]},
        "Bedtime Shema": ["Who casts"],
    }}
    return {"schema": schema, "he": he, "en": en}


@pytest.fixture
def small_curation(monkeypatch):
    monkeypatch.setattr(build_siddur, "OCCASIONS", [
        ("weekday", {"en": "Weekdays", "he": "ימות החול"}, [
            ("Weekday Shacharit", "shacharit", "Shacharit", "Weekday morning"),
            ("Bedtime Shema", "keriat-shema-al-hamita", "Keriat Shema Al HaMita", "Bedtime Shema"),
        ]),
    ])


class TestBuild:
    def test_toc_and_services(self, small_curation):
        toc, services = build_siddur.build(_sources())
        [occasion] = toc["occasions"]
        shacharit, bedtime = occasion["services"]
        assert [s["slug"] for s in shacharit["sections"]] == ["keriat-shema", "amida"]
        assert shacharit["sections"][1]["title"] == {"en": "Amida", "he": "עמידה"}
        assert shacharit["sections"][0]["ref"] == "Siddur Edot HaMizrach, Weekday Shacharit, The Shema"
        # A leaf service is one section under the service's own slug.
        assert bedtime["sections"] == [{
            "slug": "keriat-shema-al-hamita", "title": {"en": "Bedtime Shema", "he": "קריאת שמע שעל המיטה"},
            "ref": "Siddur Edot HaMizrach, Bedtime Shema", "lines": 1, "english": 1,
        }]
        assert toc["source"]["he"] == {"title": "Shaliehsaboo Edition", "license": "CC0", "source": ""}
        assert services["shacharit"]["sections"][1]["lines"][0]["when"] == ["barchenu"]
        assert services["shacharit"]["version"] == toc["version"] and re.fullmatch(r"[0-9a-f]{12}", toc["version"])

    def test_version_changes_with_content(self, small_curation):
        first, _ = build_siddur.build(_sources())
        second, _ = build_siddur.build(_sources(en_morning=["[In Summer]", "Bless us, L-rd"]))
        assert first["version"] != second["version"]

    def test_misaligned_english_is_dropped_and_flagged(self, small_curation):
        toc, services = build_siddur.build(_sources(en_morning=["a", "b", "c"]))
        amida = toc["occasions"][0]["services"][0]["sections"][1]
        assert amida["englishOmitted"] == "misaligned" and amida["english"] == 0
        assert all("en" not in line for line in services["shacharit"]["sections"][1]["lines"])

    @pytest.mark.parametrize("which", ["he", "en"])
    def test_unlicensed_versions_are_refused(self, small_curation, which):
        kwargs = {f"{which}_license": "unknown"}
        with pytest.raises(SystemExit, match="only"):
            build_siddur.build(_sources(**kwargs))

    def test_curation_must_cover_every_top_node(self, monkeypatch):
        monkeypatch.setattr(build_siddur, "OCCASIONS", [("weekday", {}, [
            ("Weekday Shacharit", "shacharit", "Shacharit", ""),
        ])])
        with pytest.raises(SystemExit, match="uncurated=\\['Bedtime Shema'\\]"):
            build_siddur.build(_sources())

    @pytest.mark.parametrize("slug", ["full", "Bad_Slug", "calendar"])
    def test_router_reserved_or_malformed_slugs_are_refused(self, monkeypatch, slug):
        monkeypatch.setattr(build_siddur, "OCCASIONS", [("weekday", {}, [
            ("Weekday Shacharit", "shacharit", "Shacharit", ""),
            ("Bedtime Shema", slug, "Bedtime", ""),
        ])])
        with pytest.raises(SystemExit, match="bad slug"):
            build_siddur.build(_sources())

    def test_duplicate_service_slugs_are_refused(self, monkeypatch):
        monkeypatch.setattr(build_siddur, "OCCASIONS", [("weekday", {}, [
            ("Weekday Shacharit", "shacharit", "Shacharit", ""),
            ("Bedtime Shema", "shacharit", "Bedtime", ""),
        ])])
        with pytest.raises(SystemExit, match="duplicate"):
            build_siddur.build(_sources())

    @pytest.mark.parametrize("title, slug", [("Hanna's Prayer", "hannas-prayer"), (" Shacharit ", "shacharit"), ("Song for Shemini Atzeret", "song-for-shemini-atzeret")])
    def test_slugify(self, title, slug):
        assert build_siddur.slugify(title) == slug


class TestWriteAndMain:
    def test_write_round_trips_and_removes_stale_services(self, small_curation, tmp_path):
        toc, services = build_siddur.build(_sources())
        (tmp_path / "services").mkdir()
        (tmp_path / "services" / "gone.json").write_text("{}", encoding="utf-8")
        build_siddur.write(toc, services, tmp_path)
        assert not (tmp_path / "services" / "gone.json").exists()
        assert json.loads((tmp_path / "toc.json").read_text(encoding="utf-8")) == toc
        for slug, payload in services.items():
            assert json.loads((tmp_path / "services" / f"{slug}.json").read_text(encoding="utf-8")) == payload

    def test_main_reads_a_source_dir(self, small_curation, tmp_path, monkeypatch, capsys):
        for name, path in build_siddur.SOURCES.items():
            (tmp_path / Path(path).name).write_text(json.dumps(_sources()[name]), encoding="utf-8")
        out = tmp_path / "out"
        monkeypatch.setattr(build_siddur, "OUT_DIR", out)
        monkeypatch.setattr(build_siddur, "REPO_ROOT", tmp_path)
        assert build_siddur.main(["--source-dir", str(tmp_path)]) == 0
        assert (out / "services" / "shacharit.json").exists()
        assert "wrote 2 services, 5 lines" in capsys.readouterr().out


# ---- The checked-in snapshot --------------------------------------------------

_ALLOWED_TAG_RE = re.compile(r"</?(?:b|i|small)>|<br>")


def _toc():
    return json.loads((DATA / "toc.json").read_text(encoding="utf-8"))


class TestCheckedInSnapshot:
    def test_licenses_are_storable(self):
        source = _toc()["source"]
        assert source["he"]["license"] in build_siddur.ALLOWED_LICENSES
        assert source["en"]["license"] in build_siddur.ALLOWED_LICENSES

    def test_every_toc_section_is_in_its_service_file_with_matching_counts(self):
        toc = _toc()
        slugs = []
        for occasion in toc["occasions"]:
            for service in occasion["services"]:
                slugs.append(service["slug"])
                payload = json.loads((DATA / "services" / f"{service['slug']}.json").read_text(encoding="utf-8"))
                assert payload["version"] == toc["version"]
                assert [s["slug"] for s in payload["sections"]] == [s["slug"] for s in service["sections"]]
                for listed, stored in zip(service["sections"], payload["sections"]):
                    assert listed["lines"] == len(stored["lines"])
                    assert listed["ref"] == stored["ref"]
        assert sorted(slugs) == sorted(p.stem for p in (DATA / "services").glob("*.json"))
        assert len(slugs) == len(set(slugs))

    def test_every_line_is_typed_and_its_html_is_sanitized(self):
        for path in (DATA / "services").glob("*.json"):
            for section in json.loads(path.read_text(encoding="utf-8"))["sections"]:
                numbers = [line["n"] for line in section["lines"]]
                assert numbers == sorted(set(numbers)), path.name
                for line in section["lines"]:
                    assert line["t"] in LINE_TYPES
                    assert set(line.get("when", [])) <= set(WHEN_KEYS)
                    for lang in ("he", "en"):
                        leftover = _ALLOWED_TAG_RE.sub("", line.get(lang, ""))
                        assert "<" not in leftover and ">" not in leftover, (path.name, line["n"])

    def test_weekday_shacharit_has_every_sefaria_section(self):
        [shacharit] = [s for o in _toc()["occasions"] for s in o["services"] if s["slug"] == "shacharit"]
        assert len(shacharit["sections"]) == 18
        assert shacharit["sections"][9]["slug"] == "amida"
