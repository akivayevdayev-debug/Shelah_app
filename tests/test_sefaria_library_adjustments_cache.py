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
def _fresh_adjustments_cache(monkeypatch):
    monkeypatch.setattr(sl, "_library_index_adjustments_cache", {
        "loaded": False, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
    })


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
