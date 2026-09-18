"""Tests for scripts/crawl_library_leaves.py.

The crawler decides, for every leaf of Sefaria's index, whether the library
should KEEP its ref, FIX it to a different loadable ref, or REMOVE it -- and
backend/sefaria_library.py acts on that report. So the tests pin the decision
(keep / fix / remove, and which phase found the ref) against a fake Sefaria
that serves chosen refs, plus the tree walk, dedupe and caching rules that feed
it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import requests

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "crawl_library_leaves.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("crawl_library_leaves", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cl = _load_script()
API = cl.SEFARIA_API


class _Resp:
    def __init__(self, status=200, payload=None, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    """Serves /texts/<ref> for the refs in `texts`, /name/<title> from `names`
    and /index from `index`; anything else is a 404. Records every request."""

    def __init__(self, texts=None, names=None, index=None, errors=()):
        self.texts = texts or {}      # encoded ref path -> payload
        self.names = names or {}      # encoded title path -> payload
        self.index = index
        self.errors = set(errors)     # url suffixes that raise a network error
        self.requests = []

    def get(self, url, params=None, timeout=None):
        self.requests.append((url, params, timeout))
        if any(url.endswith(suffix) for suffix in self.errors):
            raise requests.ConnectionError("down")
        if url == f"{API}/index":
            return _Resp(200, self.index)
        if url.startswith(f"{API}/name/"):
            payload = self.names.get(url[len(f"{API}/name/"):])
            return _Resp(200, payload) if payload is not None else _Resp(404)
        if url.startswith(f"{API}/texts/"):
            payload = self.texts.get(url[len(f"{API}/texts/"):])
            return _Resp(200, payload) if payload is not None else _Resp(404)
        return _Resp(404)


TEXT = {"text": ["In the beginning"], "he": []}


class TestSmallHelpers:
    @pytest.mark.parametrize("raw, key", [("Mishnah Berakhot 1:1", "mishnahberakhot11"),
                                          ("  Tanya! ", "tanya"), (None, ""), ("---", "")])
    def test_normalize_key(self, raw, key):
        assert cl.normalize_key(raw) == key

    @pytest.mark.parametrize("raw, key", [
        ("בראשית", "בראשית"),                         # Hebrew letters are kept
        ("בראשית א:א", "בראשיתאא"),                   # punctuation/space dropped, letters kept
        ("Génesis", "génesis"),                        # accented Latin is not stripped to "gnesis"
        ("שמות 2", "שמות2"),                           # mixed Hebrew + digits
    ])
    def test_normalize_key_keeps_non_ascii_letters(self, raw, key):
        assert cl.normalize_key(raw) == key

    def test_normalize_key_ignores_hebrew_vowel_points_and_unicode_composition(self):
        # Niqqud/cantillation are combining marks, not part of the title's identity.
        assert cl.normalize_key("בְּרֵאשִׁית") == cl.normalize_key("בראשית")
        # "é" precomposed (U+00E9) and "e" + combining acute (U+0301) are the same title.
        assert cl.normalize_key("G\u00e9nesis") == cl.normalize_key("Ge\u0301nesis") == "génesis"

    def test_distinct_non_ascii_titles_get_distinct_keys(self):
        assert cl.normalize_key("בראשית") != cl.normalize_key("שמות") != ""

    @pytest.mark.parametrize("value, expected", [
        ("text", True), ("   ", False), ([], False), (["", "  "], False),
        (["", ["", "deep"]], True), (None, False), (5, False),
    ])
    def test_has_nonempty_text_looks_through_nesting(self, value, expected):
        assert cl.has_nonempty_text(value) is expected

    def test_name_paths_use_underscores_and_keep_url_safe_punctuation(self):
        assert cl.encode_name_path(" Rabbeinu Bahya, Kad HaKemach ") == "Rabbeinu_Bahya,_Kad_HaKemach"
        assert cl.encode_name_path("Sefer HaChinukh (Minchat)") == "Sefer_HaChinukh_(Minchat)"
        assert cl.encode_name_path(None) == ""

    def test_text_ref_paths_translate_separators_and_escape_ampersands(self):
        assert cl.encode_text_ref_path("Berakhot 2a:3") == "Berakhot_2a.3"
        assert cl.encode_text_ref_path("A/B & C") == "A_B_%26_C"

    def test_first_nonempty_skips_blanks(self):
        assert cl.first_nonempty(["", "  ", None, " x ", "y"]) == "x"
        assert cl.first_nonempty([]) == ""


class TestTreeWalk:
    TREE = [
        {"category": "Tanakh", "contents": [
            {"category": "Torah", "contents": [
                {"title": "Genesis", "heTitle": "בראשית", "categories": ["Tanakh", "Torah"],
                 "firstSectionRef": "Genesis 1"},
                {"title": "Exodus", "ref": "Exodus 1:1", "categories": "not a list"},
            ]},
        ]},
        {"title": "No Categories Here", "children": [{"title": "Nested Leaf"}]},
        {"category": "Empty Branch", "contents": [{"category": "still no title"}]},
        "not a dict",
    ]

    def test_get_children_prefers_contents_and_drops_non_dicts(self):
        assert cl.get_children({"contents": [{"a": 1}, "x"], "children": [{"b": 2}]}) == [{"a": 1}]
        assert cl.get_children({"contents": [], "children": [{"b": 2}]}) == [{"b": 2}]
        assert cl.get_children({"title": "leaf"}) == []

    def test_leaves_carry_their_metadata_and_full_path(self):
        leaves = cl.collect_leaf_nodes(self.TREE)

        genesis = next(leaf for leaf in leaves if leaf["title"] == "Genesis")
        assert genesis == {
            "title": "Genesis", "he_title": "בראשית", "categories": ["Tanakh", "Torah"],
            "first_section_ref": "Genesis 1", "ref": "", "path": ["Tanakh", "Torah", "Genesis"],
        }

    def test_untitled_and_non_dict_nodes_are_not_leaves_and_bad_categories_become_empty(self):
        leaves = cl.collect_leaf_nodes(self.TREE)

        assert [leaf["title"] for leaf in leaves] == ["Genesis", "Exodus", "Nested Leaf"]
        assert next(leaf for leaf in leaves if leaf["title"] == "Exodus")["categories"] == []

    def test_a_list_at_the_root_and_a_scalar_are_both_handled(self):
        assert cl.collect_leaf_nodes("garbage") == []
        assert cl.collect_leaf_nodes([{"title": "A"}])[0]["path"] == ["A"]


class TestDedupe:
    def test_titles_are_compared_by_normalised_key_first_wins(self):
        leaves = [{"title": "Mishnah Berakhot"}, {"title": "mishnah  berakhot!"},
                  {"title": "Other"}, {"title": "  "}, {"title": None}]

        deduped, duplicates = cl.dedupe_leaf_titles(leaves)

        assert [leaf["title"] for leaf in deduped] == ["Mishnah Berakhot", "Other"]
        assert duplicates == 1

    def test_non_ascii_titles_are_kept_not_silently_dropped(self):
        leaves = [{"title": "בראשית"}, {"title": "שמות"}, {"title": "בְּרֵאשִׁית!"},
                  {"title": "Génesis"}]

        deduped, duplicates = cl.dedupe_leaf_titles(leaves)

        assert [leaf["title"] for leaf in deduped] == ["בראשית", "שמות", "Génesis"]
        assert duplicates == 1


class TestResolveNameRef:
    def test_returns_the_ref_only_when_sefaria_says_it_is_a_ref(self):
        session = FakeSession(names={"Genesis": {"is_ref": True, "ref": " Genesis "},
                                     "Torah_Ohr": {"is_ref": False, "ref": "Torah Ohr"}})
        cache = {}

        assert cl.resolve_name_ref(session, "Genesis", 5, cache) == "Genesis"
        assert cl.resolve_name_ref(session, "Torah Ohr", 5, cache) == ""

    @pytest.mark.parametrize("session", [
        FakeSession(),                                                   # 404
        FakeSession(names={"Bad": ["not", "a", "dict"]}),                 # non-dict payload
        FakeSession(errors=["/name/Bad"]),                                # network error
    ])
    def test_failures_resolve_to_empty_string_and_are_cached(self, session):
        cache = {}
        assert cl.resolve_name_ref(session, "Bad", 5, cache) == ""
        assert cl.resolve_name_ref(session, "Bad", 5, cache) == ""
        assert len(session.requests) == 1, "a failed lookup must not be retried"

    def test_invalid_json_is_an_empty_result(self):
        class _S:
            def get(self, url, timeout):
                return _Resp(200, bad_json=True)

        assert cl.resolve_name_ref(_S(), "X", 5, {}) == ""

    def test_hebrew_title_is_looked_up_not_treated_as_blank(self):
        hebrew = "בראשית"
        session = FakeSession(names={cl.encode_name_path(hebrew): {"is_ref": True, "ref": "Genesis"}})

        assert cl.resolve_name_ref(session, hebrew, 5, {}) == "Genesis"
        assert len(session.requests) == 1

    def test_blank_title_makes_no_request(self):
        session = FakeSession()
        assert cl.resolve_name_ref(session, "  ", 5, {}) == ""
        assert session.requests == []

    def test_hits_are_cached_by_normalised_title(self):
        session = FakeSession(names={"Genesis": {"is_ref": True, "ref": "Genesis"}})
        cache = {}
        cl.resolve_name_ref(session, "Genesis", 5, cache)
        cl.resolve_name_ref(session, "genesis!", 5, cache)
        assert len(session.requests) == 1


class TestProbeRef:
    def test_text_in_either_language_counts_as_loadable(self):
        session = FakeSession(texts={"Genesis_1": TEXT, "Tanya_1": {"text": [], "he": ["שלום"]}})
        assert cl.probe_ref(session, "Genesis 1", 5, {}) == (True, "ok")
        assert cl.probe_ref(session, "Tanya 1", 5, {}) == (True, "ok")

    def test_requests_ask_for_a_bilingual_padless_slice(self):
        session = FakeSession(texts={"Genesis_1": TEXT})
        cl.probe_ref(session, "Genesis 1", 7, {})
        url, params, timeout = session.requests[0]
        assert url == f"{API}/texts/Genesis_1"
        assert params == {"lang": "bi", "context": 0, "pad": 0}
        assert timeout == 7

    @pytest.mark.parametrize("session, ref, expected", [
        (FakeSession(), "Nope 1", (False, "status_404")),
        (FakeSession(texts={"E_1": {"error": "No book"}}), "E 1", (False, "api_error")),
        (FakeSession(texts={"Empty_1": {"text": [], "he": [""]}}), "Empty 1", (False, "no_text")),
        (FakeSession(texts={"L_1": ["a list"]}), "L 1", (False, "no_text")),
        (FakeSession(errors=["/texts/Down_1"]), "Down 1", (False, "request_error:ConnectionError")),
    ])
    def test_each_failure_mode_gets_its_own_reason(self, session, ref, expected):
        assert cl.probe_ref(session, ref, 5, {}) == expected

    def test_invalid_json_and_blank_ref(self):
        class _S:
            def get(self, url, params, timeout):
                return _Resp(200, bad_json=True)

        assert cl.probe_ref(_S(), "X 1", 5, {}) == (False, "invalid_json")
        assert cl.probe_ref(FakeSession(), "  ", 5, {}) == (False, "empty_ref")

    def test_results_are_cached_per_ref(self):
        session = FakeSession(texts={"Genesis_1": TEXT})
        cache = {}
        cl.probe_ref(session, "Genesis 1", 5, cache)
        cl.probe_ref(session, "Genesis 1", 5, cache)
        cl.probe_ref(session, "Missing 1", 5, cache)
        cl.probe_ref(session, "Missing 1", 5, cache)
        assert len(session.requests) == 2


class TestCandidates:
    def test_add_candidate_trims_and_dedupes(self):
        out = []
        for value in (" A ", "A", "", None, "B"):
            cl.add_candidate(out, value)
        assert out == ["A", "B"]

    def test_primary_order_is_section_ref_ref_name_ref_then_title(self):
        leaf = {"first_section_ref": "S 1", "ref": "R 1", "title": "T"}
        assert cl.build_primary_candidates(leaf, "N 1") == ["S 1", "R 1", "N 1", "T"]
        assert cl.build_primary_candidates({"title": "T", "ref": "T"}, "") == ["T"]

    def test_heuristics_start_with_chapter_guesses(self):
        assert cl.build_heuristic_candidates({"title": "Foo"}, []) == ["Foo 1", "Foo 1:1", "Foo, 1", "Foo, 1:1"]

    def test_talmud_mishnah_commentary_and_targum_add_their_own_guesses(self):
        talmud = cl.build_heuristic_candidates({"title": "Berakhot", "categories": ["Talmud"]}, [])
        assert "Berakhot 2a" in talmud

        mishnah = cl.build_heuristic_candidates({"title": "Berakhot", "categories": ["Mishnah"]}, [])
        assert "Mishnah Berakhot 1" in mishnah
        already = cl.build_heuristic_candidates({"title": "Mishnah Berakhot", "categories": ["Mishnah"]}, [])
        assert not any(c.startswith("Mishnah Mishnah") for c in already)

        commentary = cl.build_heuristic_candidates({"title": "Rashi on X", "categories": []}, [])
        assert "Rashi on X, Genesis 1" in commentary

        targum = cl.build_heuristic_candidates({"title": "Onkelos", "categories": ["Targum"]}, [])
        assert "Onkelos, Genesis 1:1" in targum

    def test_candidates_already_tried_are_excluded_and_blank_titles_have_none(self):
        assert "Foo 1" not in cl.build_heuristic_candidates({"title": "Foo"}, ["Foo 1", " "])
        assert cl.build_heuristic_candidates({"title": "  "}, []) == []


class TestProbeCandidates:
    def test_stops_at_the_first_success_and_records_each_attempt(self):
        session = FakeSession(texts={"B_1": TEXT, "C_1": TEXT})
        attempts = []

        found = cl._probe_candidates(session, ["A 1", "B 1", "C 1"], "primary", 5, {}, attempts)

        assert found == "B 1"
        assert [(a["ref"], a["ok"], a["phase"]) for a in attempts] == [
            ("A 1", False, "primary"), ("B 1", True, "primary")]

    def test_no_success_returns_empty_string(self):
        attempts = []
        assert cl._probe_candidates(FakeSession(), ["A 1"], "heuristic", 5, {}, attempts) == ""
        assert attempts[0]["reason"] == "status_404"


class TestAnalyzeLeaf:
    def analyze(self, session, leaf):
        return cl.analyze_leaf(session, leaf, 5, {}, {})

    def test_keep_when_the_original_ref_loads(self):
        session = FakeSession(texts={"Genesis_1": TEXT})
        result = self.analyze(session, {"title": "Genesis", "first_section_ref": "Genesis 1"})

        assert result["action"] == "keep"
        assert result["suggested_ref"] == "Genesis 1"
        assert result["resolution_phase"] == "primary"

    def test_fix_when_only_a_different_ref_loads(self):
        session = FakeSession(
            names={"Sefer_X": {"is_ref": True, "ref": "Sefer X 1"}},
            texts={"Sefer_X_1": TEXT})
        result = self.analyze(session, {"title": "Sefer X", "first_section_ref": "Sefer X Intro"})

        assert result["action"] == "fix"
        assert result["initial_ref"] == "Sefer X Intro"
        assert result["name_ref"] == "Sefer X 1"
        assert result["suggested_ref"] == "Sefer X 1"

    def test_heuristics_are_only_tried_after_the_primary_candidates_fail(self):
        session = FakeSession(texts={"Foo_1": TEXT})
        result = self.analyze(session, {"title": "Foo"})

        assert result["resolution_phase"] == "heuristic"
        assert result["action"] == "fix"
        assert [a["phase"] for a in result["attempts"]] == ["primary", "heuristic"]
        assert result["attempts"][-1] == {"phase": "heuristic", "ref": "Foo 1", "ok": True, "reason": "ok"}

    def test_remove_when_nothing_loads(self):
        result = self.analyze(FakeSession(), {"title": "Ghost", "he_title": "רוח", "categories": ["X"],
                                              "path": ["A", "Ghost"]})

        assert result["action"] == "remove"
        assert result["suggested_ref"] == ""
        assert result["resolution_phase"] == ""
        assert (result["he_title"], result["categories"], result["path"]) == ("רוח", ["X"], ["A", "Ghost"])
        assert len(result["attempts"]) >= 5

    def test_initial_ref_falls_back_to_the_title(self):
        assert self.analyze(FakeSession(), {"title": "Ghost"})["initial_ref"] == "Ghost"


class TestFetchIndexPayload:
    def test_unwraps_a_contents_list(self):
        assert cl.fetch_index_payload(FakeSession(index={"contents": [1, 2]}), 5) == [1, 2]

    def test_returns_other_payloads_unchanged(self):
        assert cl.fetch_index_payload(FakeSession(index=[{"title": "A"}]), 5) == [{"title": "A"}]
        assert cl.fetch_index_payload(FakeSession(index={"contents": "str"}), 5) == {"contents": "str"}

    def test_http_failure_propagates(self):
        class _S:
            def get(self, url, timeout):
                return _Resp(500)

        with pytest.raises(requests.HTTPError):
            cl.fetch_index_payload(_S(), 5)


class TestParseArgs:
    def test_defaults(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["crawl"])
        args = cl.parse_args()
        assert (args.output, args.max_leaves, args.timeout, args.sleep, args.progress_every,
                args.verbose, args.include_kept) == (
            "reports/library_leaf_remove_fix_report.json", 0, 8.0, 0.0, 100, False, False)


class TestMain:
    INDEX = {"contents": [
        {"category": "Tanakh", "contents": [
            {"title": "Genesis", "firstSectionRef": "Genesis 1"},         # keep
            {"title": "Sefer X", "firstSectionRef": "Sefer X Intro"},      # fix (via name lookup)
            {"title": "Ghost"},                                            # remove
            {"title": "genesis"},                                          # duplicate title
        ]},
    ]}

    @pytest.fixture
    def session(self, monkeypatch):
        fake = FakeSession(
            index=self.INDEX,
            names={"Sefer_X": {"is_ref": True, "ref": "Sefer X 1"}},
            texts={"Genesis_1": TEXT, "Sefer_X_1": TEXT})
        monkeypatch.setattr(cl.requests, "Session", lambda: fake)
        monkeypatch.setattr(cl.time, "sleep", lambda s: None)
        return fake

    def run(self, monkeypatch, tmp_path, *extra):
        out = tmp_path / "reports" / "r.json"
        monkeypatch.setattr(sys, "argv", ["crawl", "--output", str(out), *extra])
        rc = cl.main()
        return rc, json.loads(out.read_text(encoding="utf-8"))

    def test_report_classifies_every_unique_leaf_and_counts_add_up(self, session, monkeypatch, tmp_path, capsys):
        rc, report = self.run(monkeypatch, tmp_path)

        assert rc == 0
        assert [f["title"] for f in report["fixes"]] == ["Sefer X"]
        assert [r["title"] for r in report["removals"]] == ["Ghost"]
        assert "kept" not in report
        stats = report["stats"]
        assert (stats["leaf_count_raw"], stats["leaf_count_unique"], stats["duplicate_titles_removed"]) == (4, 3, 1)
        assert (stats["keep_count"], stats["fix_count"], stats["remove_count"]) == (1, 1, 1)
        assert report["machine_generated"] is True
        assert "Summary -> keep=1, fix=1, remove=1" in capsys.readouterr().out

    def test_include_kept_adds_the_kept_entries(self, session, monkeypatch, tmp_path):
        _, report = self.run(monkeypatch, tmp_path, "--include-kept")
        assert [k["title"] for k in report["kept"]] == ["Genesis"]

    def test_max_leaves_limits_the_crawl(self, session, monkeypatch, tmp_path):
        _, report = self.run(monkeypatch, tmp_path, "--max-leaves", "1")
        assert report["stats"]["leaf_count_unique"] == 1
        assert report["fixes"] == [] and report["removals"] == []

    def test_verbose_logs_every_leaf_and_sleep_is_applied_between_probes(self, session, monkeypatch,
                                                                       tmp_path, capsys):
        sleeps = []
        monkeypatch.setattr(cl.time, "sleep", sleeps.append)

        self.run(monkeypatch, tmp_path, "--verbose", "--sleep", "0.5")

        out = capsys.readouterr().out
        assert out.count("[crawler] ") >= 3 + 3
        assert "title='Ghost' action=remove" in out
        assert sleeps == [0.5, 0.5, 0.5]

    def test_report_is_ascii_json_so_hebrew_titles_survive_any_terminal(self, session, monkeypatch, tmp_path):
        session.index = {"contents": [{"title": "Ghost שלום"}]}
        out = tmp_path / "r.json"
        monkeypatch.setattr(sys, "argv", ["crawl", "--output", str(out)])
        cl.main()
        assert "שלום" not in out.read_text(encoding="utf-8")
        assert json.loads(out.read_text(encoding="utf-8"))["removals"][0]["title"] == "Ghost שלום"


def test_running_the_file_as_a_script_exits_with_mains_return_code(monkeypatch, tmp_path):
    import runpy

    fake = FakeSession(index={"contents": []})
    monkeypatch.setattr(requests, "Session", lambda: fake)
    monkeypatch.setattr(sys, "argv", ["crawl", "--output", str(tmp_path / "r.json")])

    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SCRIPT_PATH), run_name="__main__")

    assert exc.value.code == 0
