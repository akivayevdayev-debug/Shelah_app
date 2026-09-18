"""
Concurrency-safety tests for backend/sefaria_library.py's module-level caches
(plan.md §5.2).

Covers the two race classes the phase targets:
  - check-then-set windows on the TTLCache-backed caches (_cache,
    _resolved_title_ref_cache, _resolved_query_ref_cache, _search_query_cache)
  - torn multi-key reads on the atomic-swap caches (_title_catalog_cache,
    _library_index_view_cache, _library_index_adjustments_cache)

These tests drive real threads against the real module functions with the
network layer stubbed out (no real HTTP), so they exercise the actual lock
discipline rather than a simplified model of it.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import backend.sefaria_library as sefaria_library_module


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _reset_sefaria_library_caches():
    """Module-level caches are process-wide singletons — isolate every test
    in this file (and protect other test files) from cross-test pollution."""
    def _clear():
        sefaria_library_module._cache.clear()
        sefaria_library_module._resolved_title_ref_cache.clear()
        sefaria_library_module._resolved_query_ref_cache.clear()
        sefaria_library_module._search_query_cache.clear()
        sefaria_library_module._title_catalog_cache = {
            "ts": 0, "report_mtime": 0.0, "data": []}
        sefaria_library_module._library_index_view_cache = {
            "ts": 0.0, "report_mtime": 0.0, "data": None}
        sefaria_library_module._library_index_adjustments_cache = {
            "loaded": False, "mtime": 0.0, "remove_keys": set(), "fix_map": {},
        }
    _clear()
    yield
    _clear()


class TestHammerConcurrency:
    """N-thread hammer tests: no exceptions, consistent final state, bounded
    duplicate work under concurrent misses (some duplication under a
    concurrent miss is explicitly accepted by plan.md §5.2 point 1 — the
    guarantee is safety, not zero-duplication)."""

    def test_cached_get_sixteen_threads_no_exceptions_consistent_result(self, monkeypatch):
        call_count = {"n": 0}
        count_lock = threading.Lock()

        def fake_get(url, timeout=12):
            with count_lock:
                call_count["n"] += 1
            time.sleep(0.01)  # widen the race window across the miss path
            return _FakeResponse({"ref": url, "data": "payload"})

        monkeypatch.setattr(
            sefaria_library_module._http_session, "get", fake_get)
        monkeypatch.setattr(
            sefaria_library_module, "_disk_cache_get", lambda url: None)
        monkeypatch.setattr(
            sefaria_library_module, "_disk_cache_set", lambda url, data: None)

        url = f"{sefaria_library_module.SEFARIA_API}/hammer-endpoint"
        errors = []
        results = []
        results_lock = threading.Lock()

        def worker():
            try:
                r = sefaria_library_module._cached_get(url)
                with results_lock:
                    results.append(r)
            except Exception as exc:  # pragma: no cover - failure path
                with results_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert len(results) == 16
        assert all(r == results[0] for r in results)
        # At most one network call per thread — sanity bound against a
        # runaway/duplicate-fetch bug, not a claim of zero duplication.
        assert 1 <= call_count["n"] <= 16
        # Final cache state must reflect exactly one consistent value.
        assert sefaria_library_module._cache.get(url) == results[0]

    def test_title_catalog_sixteen_threads_no_exceptions_never_torn(self, monkeypatch):
        fake_index = {
            "categories": ["Tanakh"],
            "contents": [
                {"title": "Genesis", "heTitle": "בראשית", "categories": ["Tanakh", "Torah"]},
                {"title": "Exodus", "heTitle": "שמות", "categories": ["Tanakh", "Torah"]},
            ],
        }

        def fake_cached_get(url, ttl=None):
            time.sleep(0.01)  # widen the race window
            return fake_index

        monkeypatch.setattr(
            sefaria_library_module, "_cached_get", fake_cached_get)
        monkeypatch.setattr(
            sefaria_library_module,
            "_load_library_index_adjustments",
            lambda: {"loaded": True, "mtime": 0.0,
                      "remove_keys": set(), "fix_map": {}},
        )

        errors = []
        results = []
        results_lock = threading.Lock()

        def worker():
            try:
                rows = sefaria_library_module._get_title_catalog()
                with results_lock:
                    results.append(rows)
            except Exception as exc:  # pragma: no cover - failure path
                with results_lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        assert len(results) == 16
        # Every reader must observe a *complete* catalog — never a torn
        # partial one caused by a concurrent writer swap.
        for rows in results:
            titles = {row["title"] for row in rows}
            assert titles == {"Genesis", "Exodus"}
        final = sefaria_library_module._title_catalog_cache
        assert {row["title"] for row in final["data"]} == {
            "Genesis", "Exodus"}


class TestTornReadRegression:
    def test_title_catalog_invariant_never_violated_under_concurrent_swaps(self, monkeypatch):
        """A reader thread continuously snapshots _title_catalog_cache while
        a writer thread forces repeated rebuild-and-swap cycles. The
        invariant (report_mtime fresh ⇒ data belongs to that same version)
        must never be violated (plan.md §5.2 point 2)."""
        current_version = {"v": 0}

        def fake_load_adjustments():
            v = current_version["v"]
            return {"loaded": True, "mtime": float(v), "remove_keys": set(), "fix_map": {}}

        def fake_cached_get(url, ttl=None):
            v = current_version["v"]
            return {
                "categories": [],
                "contents": [{"title": f"Book-v{v}", "heTitle": "", "categories": []}],
            }

        monkeypatch.setattr(
            sefaria_library_module, "_load_library_index_adjustments", fake_load_adjustments)
        monkeypatch.setattr(
            sefaria_library_module, "_cached_get", fake_cached_get)

        stop = threading.Event()
        violations = []

        def writer():
            for i in range(1, 51):
                current_version["v"] = i
                sefaria_library_module._get_title_catalog(ttl=0)
            stop.set()

        def reader():
            while not stop.is_set():
                snapshot = sefaria_library_module._title_catalog_cache
                mtime = snapshot.get("report_mtime", 0.0)
                data = snapshot.get("data", [])
                if mtime and data:
                    expected_title = f"Book-v{int(mtime)}"
                    titles = {row["title"] for row in data}
                    if titles != {expected_title}:
                        violations.append((mtime, titles))

        reader_threads = [threading.Thread(target=reader) for _ in range(8)]
        writer_thread = threading.Thread(target=writer)
        for t in reader_threads:
            t.start()
        writer_thread.start()
        writer_thread.join(timeout=15)
        stop.set()
        for t in reader_threads:
            t.join(timeout=5)

        assert not violations


class TestImmutabilityContract:
    def test_title_catalog_row_mutation_is_visible_on_cache_hit(self, monkeypatch):
        """Plan.md §5.2 point 4 explicitly rejects blanket deepcopy in the
        critical section (it would serialize every reader behind an
        O(payload) copy on the hottest read path). Cached rows are
        contractually immutable-after-insert instead of defensively copied
        — callers must not mutate what _get_title_catalog() returns. This
        pins that contract precisely: a within-TTL cache hit returns the
        exact same row objects (no copy), so a mutation would leak to every
        future reader until the next rebuild. (Audited: no real caller
        mutates this return value — backend/routes_library.py only reads it
        via jsonify().)"""
        fake_index = {
            "categories": [],
            "contents": [{"title": "Genesis", "heTitle": "", "categories": []}],
        }
        monkeypatch.setattr(
            sefaria_library_module, "_cached_get", lambda url, ttl=None: fake_index)
        monkeypatch.setattr(
            sefaria_library_module,
            "_load_library_index_adjustments",
            lambda: {"loaded": True, "mtime": 0.0,
                      "remove_keys": set(), "fix_map": {}},
        )

        first = sefaria_library_module._get_title_catalog()
        assert first
        assert first[0]["title"] == "Genesis"
        first[0]["title"] = "MUTATED-BY-TEST"

        second = sefaria_library_module._get_title_catalog()
        assert first is second
        assert second[0]["title"] == "MUTATED-BY-TEST"


class TestLockScope:
    def test_cache_hit_never_blocks_behind_another_keys_inflight_miss(self, monkeypatch):
        slow_url = f"{sefaria_library_module.SEFARIA_API}/slow-endpoint"
        fast_url = f"{sefaria_library_module.SEFARIA_API}/fast-endpoint"

        # Prime fast_url as an existing cache hit.
        sefaria_library_module._cache.set(
            fast_url, {"ref": fast_url}, ttl=3600)

        release = threading.Event()
        entered_network_call = threading.Event()

        def fake_get(url, timeout=12):
            if url == slow_url:
                entered_network_call.set()
                release.wait(timeout=5)
            return _FakeResponse({"ref": url})

        monkeypatch.setattr(
            sefaria_library_module._http_session, "get", fake_get)
        monkeypatch.setattr(
            sefaria_library_module, "_disk_cache_get", lambda url: None)
        monkeypatch.setattr(
            sefaria_library_module, "_disk_cache_set", lambda url, data: None)

        miss_thread = threading.Thread(
            target=lambda: sefaria_library_module._cached_get(slow_url))
        miss_thread.start()
        assert entered_network_call.wait(
            timeout=5), "miss thread never reached the network call"

        start = time.monotonic()
        hit_result = sefaria_library_module._cached_get(fast_url)
        elapsed = time.monotonic() - start

        release.set()
        miss_thread.join(timeout=5)

        assert hit_result == {"ref": fast_url}
        assert elapsed < 0.5, (
            f"cache hit took {elapsed:.3f}s — appears to have blocked "
            "behind another key's in-flight miss"
        )


class TestAtomicSwapFreshnessRace:
    def test_slower_thread_never_clobbers_a_fresher_concurrent_write(self, monkeypatch):
        """Regression test for a confirmed Phase 5 concurrency-review
        finding: the writer-side re-check before each atomic dict-swap
        previously used strict equality (current mtime == mine) instead of
        an at-least-as-fresh comparison (>=). A thread that captured an
        OLDER file mtime and then lost the race for the lock would still
        unconditionally overwrite a FRESHER concurrent write with its own
        stale result — silently discarding the winner's data (bounded, not
        a torn read, but a real logic bug against the code's own stated
        "equal-or-newer" intent). This pins the exact race: thread A reads
        mtime=1.0 and starts a slow build; mid-build, the file changes to
        mtime=2.0 and thread B wins the lock first, publishing 2.0; thread A
        then finishes and must defer to B's fresher state instead of
        clobbering it with its own mtime=1.0 result."""
        mtime_holder = {"v": 1.0}
        block_event = threading.Event()
        release_event = threading.Event()

        class _FakeReportPath:
            def __init__(self):
                self._stat_calls = 0

            def exists(self):
                return True

            def stat(self):
                self._stat_calls += 1
                captured_mtime = mtime_holder["v"]
                if self._stat_calls == 1:
                    # Thread A: simulate a slow build after reading the
                    # (soon to be stale) mtime — block until the test lets
                    # it resume.
                    block_event.set()
                    release_event.wait(timeout=5)
                return SimpleNamespace(st_mtime=captured_mtime)

            def read_text(self, encoding="utf-8"):
                return json.dumps({"removals": [], "fixes": []})

        monkeypatch.setattr(
            sefaria_library_module, "_LIBRARY_REPORT_PATH", _FakeReportPath())

        a_result = {}

        def thread_a():
            a_result["state"] = sefaria_library_module._load_library_index_adjustments()

        a = threading.Thread(target=thread_a)
        a.start()
        assert block_event.wait(timeout=5), "thread A never reached stat()"

        # Thread B: the "file" is now at a newer mtime; build and publish
        # while thread A is still blocked mid-build on its stale read.
        mtime_holder["v"] = 2.0
        b_state = sefaria_library_module._load_library_index_adjustments()
        assert b_state["mtime"] == 2.0

        # Let thread A finish its stale (mtime=1.0) build and attempt to
        # publish.
        release_event.set()
        a.join(timeout=5)

        # A correctly-fixed re-check makes the losing thread defer to the
        # fresher state — both in what it returns to its own caller and in
        # what ends up published globally.
        assert a_result["state"]["mtime"] == 2.0
        final = sefaria_library_module._library_index_adjustments_cache
        assert final["mtime"] == 2.0, (
            "a slower thread's stale write clobbered a fresher concurrent "
            "write — the writer-side re-check must use >= (at-least-as-"
            "fresh), not == (exactly-equal)"
        )
