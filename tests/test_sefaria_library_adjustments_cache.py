"""Regression tests for _load_library_index_adjustments()'s "report absent"
handling (SonarCloud python:S1244).

The function used to detect "the crawl report does not exist" by comparing the
cached float mtime with ``== 0.0``. That sentinel collided with the stat()
fallback (``mtime = 0.0`` when ``path.stat()`` raises on a report that DOES
exist), so a report that was parsed under a failed stat() and later deleted was
mistaken for the already-cached "absent" state and its stale removal/fix keys
were served forever.
"""

import json

import pytest

import backend.sefaria_library as sl


class _FakeReportPath:
    """Minimal Path stand-in whose existence / stat() behavior the test drives."""

    def __init__(self, payload, *, exists=True, stat_raises=False, mtime=5.0):
        self._payload = payload
        self.present = exists
        self.stat_raises = stat_raises
        self.mtime = mtime

    def exists(self):
        return self.present

    def stat(self):
        if self.stat_raises:
            raise OSError("stat failed")
        return type("_Stat", (), {"st_mtime": self.mtime})()

    def read_text(self, encoding="utf-8"):
        return json.dumps(self._payload)


@pytest.fixture(autouse=True)
def _fresh_adjustments_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(sl, "_library_index_adjustments_cache", {
        "loaded": False, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })
    # The checked-in reinstatement file stays out of these fake-report runs.
    monkeypatch.setattr(sl, "_LIBRARY_REINSTATED_PATH", tmp_path / "reinstated.json")


def _install(monkeypatch, fake):
    monkeypatch.setattr(sl, "_LIBRARY_REPORT_PATH", fake)


def test_missing_report_yields_empty_state_and_is_cached(monkeypatch):
    _install(monkeypatch, _FakeReportPath({}, exists=False))

    first = sl._load_library_index_adjustments()
    second = sl._load_library_index_adjustments()

    assert first["remove_keys"] == set()
    assert first["fix_map"] == {}
    assert first["loaded"] is True
    # Second call is served from the cache: the very same object, not a rebuild.
    assert second is first


def test_report_appearing_after_absent_state_is_picked_up(monkeypatch):
    fake = _FakeReportPath(
        {"removals": [{"title": "Bogus Title"}], "fixes": []}, exists=False)
    _install(monkeypatch, fake)
    absent = sl._load_library_index_adjustments()
    assert absent["remove_keys"] == set()

    fake.present = True
    loaded = sl._load_library_index_adjustments()

    assert loaded is not absent
    assert loaded["mtime"] == pytest.approx(5.0)
    assert loaded["remove_keys"], "keys from the newly created report must load"


def test_report_deleted_after_stat_failure_does_not_serve_stale_keys(monkeypatch):
    """The bug: stat() failing on an existing report caches mtime 0.0 together
    with the parsed keys; deleting the report afterwards must reset to empty,
    but the float ``== 0.0`` check mistook that snapshot for "absent"."""
    fake = _FakeReportPath(
        {"removals": [{"title": "Bogus Title"}], "fixes": []}, stat_raises=True)
    _install(monkeypatch, fake)
    parsed = sl._load_library_index_adjustments()
    assert parsed["remove_keys"], "precondition: the report's keys were parsed"
    assert parsed["mtime"] == pytest.approx(0.0)

    fake.present = False
    after_delete = sl._load_library_index_adjustments()

    assert after_delete["remove_keys"] == set()
    assert after_delete["fix_map"] == {}


class TestReinstatedRemovals:
    """reports/library_leaf_reinstated.json (scripts/verify_library_removals.py)
    takes removals the crawl got wrong back out -- for the report run it
    names, and no other."""

    RUN = "2026-04-21T01:44:51+00:00"
    REPORT = {
        "generated_at_utc": RUN,
        "removals": [
            {"title": "Siddur Sefard", "name_ref": "Siddur Sefard", "initial_ref": "Siddur Sefard"},
            {"title": "Jastrow", "initial_ref": "Jastrow"},
        ],
        "fixes": [],
    }

    def _load(self, monkeypatch, tmp_path, reinstated):
        _install(monkeypatch, _FakeReportPath(self.REPORT))
        path = tmp_path / "reinstated.json"
        if reinstated is not None:
            path.write_text(reinstated if isinstance(reinstated, str) else json.dumps(reinstated), encoding="utf-8")
        return sl._load_library_index_adjustments()

    def test_a_reinstated_work_is_no_longer_removed(self, monkeypatch, tmp_path):
        state = self._load(monkeypatch, tmp_path, {
            "report_generated_at_utc": self.RUN,
            "reinstated": [{"title": "Siddur Sefard", "opening_ref": "Siddur Sefard, Upon Arising, Modeh Ani"}],
        })
        assert state["remove_keys"] == {"jastrow"}

    @pytest.mark.parametrize("reinstated", [
        None,                                                   # no file
        "{not json",                                            # unreadable
        ["Siddur Sefard"],                                      # wrong shape
        {"report_generated_at_utc": "2026-10-01T00:00:00+00:00",  # another crawl's
         "reinstated": [{"title": "Siddur Sefard"}]},
    ])
    def test_otherwise_the_report_stands(self, monkeypatch, tmp_path, reinstated):
        state = self._load(monkeypatch, tmp_path, reinstated)
        assert state["remove_keys"] == {"siddursefard", "jastrow"}

    def test_a_report_without_a_run_stamp_takes_no_reinstatements(self, monkeypatch, tmp_path):
        _install(monkeypatch, _FakeReportPath({"removals": [{"title": "Siddur Sefard"}]}))
        (tmp_path / "reinstated.json").write_text(json.dumps(
            {"report_generated_at_utc": None, "reinstated": [{"title": "Siddur Sefard"}]}), encoding="utf-8")
        assert sl._load_library_index_adjustments()["remove_keys"] == {"siddursefard"}

    def test_a_newer_reinstatement_file_refreshes_the_cached_keys(self, monkeypatch, tmp_path):
        before = self._load(monkeypatch, tmp_path, None)
        assert "siddursefard" in before["remove_keys"]

        path = tmp_path / "reinstated.json"
        path.write_text(json.dumps({"report_generated_at_utc": self.RUN,
                                    "reinstated": [{"title": "Siddur Sefard"}]}), encoding="utf-8")
        after = sl._load_library_index_adjustments()

        assert after is not before
        assert after["remove_keys"] == {"jastrow"}


def test_the_checked_in_reinstatements_apply_to_the_checked_in_report():
    """The shipped pair must line up, or every siddur, machzor and haggadah
    the April crawl mis-probed silently disappears from the library again."""
    root = sl._PROJECT_ROOT / "reports"
    report = json.loads((root / "library_leaf_remove_fix_report.full.json").read_text(encoding="utf-8"))
    reinstated = json.loads((root / "library_leaf_reinstated.json").read_text(encoding="utf-8"))
    assert reinstated["report_generated_at_utc"] == report["generated_at_utc"]

    removed = {sl._normalize_title_key(row["title"]) for row in report["removals"]}
    back = {sl._normalize_title_key(row["title"]) for row in reinstated["reinstated"]}
    assert back <= removed
    for title in ("Siddur Edot HaMizrach", "Siddur Sefard", "Siddur Ashkenaz", "Pesach Haggadah",
                  "Birkat Hamazon", "Machzor Yom Kippur Sefard"):
        assert sl._normalize_title_key(title) in back, title
    # A removal the export has no text for (Jastrow, a lexicon) stays out.
    assert sl._normalize_title_key("Jastrow") not in back
