"""
Tests for backend/sidebar_bundle.py and GET /api/sidebar/<ref>.

Covers:
  - slim_links: only commentary/targum/midrash, ordered, de-duplicated, capped
  - _author_base / pick_preload_refs: which commentators' text is preloaded
  - build_sidebar_links: caching, no caching of empty (transient) results
  - build_sidebar_texts: translation only for the first passages, size caps,
    one failing commentator not sinking the rest, partial results kept briefly
  - _single_flight: concurrent callers share one computation
  - the route: stages, 400s, rate-limit class
  - COMMENTARY_PRIORITY stays in step with the client's list
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import pytest

from backend import sidebar_bundle as sb
from backend.rate_limit import classify_route

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _fresh_caches():
    sb._links_cache.clear()
    sb._texts_cache.clear()
    sb._flights.clear()
    yield
    sb._links_cache.clear()
    sb._texts_cache.clear()
    sb._flights.clear()


def link(ref, **extra):
    return {"ref": ref, **extra}


def grouped(**categories):
    return {name.replace("_", " "): items for name, items in categories.items()}


# ── slim_links ────────────────────────────────────────────────────────────────


class TestSlimLinks:
    def test_keeps_commentary_targum_midrash_in_that_order_and_drops_the_rest(self):
        slim = sb.slim_links({
            "Midrash": [link("Bereshit Rabbah 1:1")],
            "Quoting Commentary": [link("Some Work on Exodus 1:1")],
            "Targum": [link("Onkelos Genesis 1:1")],
            "Reference": [link("Strong's H7225")],
            "Commentary": [link("Rashi on Genesis 1:1:1")],
            "Liturgy": [link("Siddur 1")],
        })
        assert list(slim) == ["Commentary", "Targum", "Midrash"]

    def test_other_commentary_like_categories_follow_plain_commentary(self):
        slim = sb.slim_links({
            "Targum": [link("Targum Jonathan on Genesis 1:1")],
            "Supercommentary": [link("Siftei Chakhamim on Genesis 1:1:1")],
            "Commentary": [link("Rashi on Genesis 1:1:1")],
        })
        assert list(slim) == ["Commentary", "Supercommentary", "Targum"]

    def test_keeps_only_the_fields_the_sidebar_reads(self):
        slim = sb.slim_links({"Commentary": [{
            "ref": "Rashi on Genesis 1:1:1",
            "heRef": 'רש"י על בראשית א:א:א',
            "collectiveTitle": {"en": "Rashi", "he": 'רש"י', "extra": "x"},
            "anchorRef": "Genesis 1:1",
            "sourceHasEn": True,
            "category": "Commentary",
        }]})
        assert slim == {"Commentary": [{
            "ref": "Rashi on Genesis 1:1:1",
            "heRef": 'רש"י על בראשית א:א:א',
            "collectiveTitle": {"en": "Rashi", "he": 'רש"י'},
        }]}

    def test_deduplicates_across_categories_case_insensitively(self):
        slim = sb.slim_links({
            "Commentary": [link("Rashi on Genesis 1:1:1"), link("rashi on genesis 1:1:1")],
            "Midrash": [link("RASHI ON GENESIS 1:1:1"), link("Bereshit Rabbah 1:1")],
        })
        assert slim == {
            "Commentary": [{"ref": "Rashi on Genesis 1:1:1"}],
            "Midrash": [{"ref": "Bereshit Rabbah 1:1"}],
        }

    def test_skips_malformed_items_and_empty_categories(self):
        slim = sb.slim_links({
            "Commentary": [None, "x", {}, {"ref": "  "}, link("Rashi on Genesis 1:1:1")],
            "Targum": [{"ref": ""}],
            "Midrash": "not a list",
        })
        assert slim == {"Commentary": [{"ref": "Rashi on Genesis 1:1:1"}]}

    def test_not_a_dict_is_empty(self):
        assert sb.slim_links(None) == {}
        assert sb.slim_links([]) == {}

    def test_total_is_capped(self, monkeypatch):
        monkeypatch.setattr(sb, "LINK_REF_LIMIT", 3)
        slim = sb.slim_links({
            "Commentary": [link(f"Work {i} on Genesis 1:1") for i in range(5)],
            "Targum": [link("Onkelos Genesis 1:1")],
        })
        assert sum(len(items) for items in slim.values()) == 3
        assert "Targum" not in slim


# ── Which commentators to preload ─────────────────────────────────────────────


class TestAuthorBase:
    @pytest.mark.parametrize("entry, expected", [
        ({"ref": "Rashi on Genesis 1:1:1", "collectiveTitle": {"en": "Rashi"}}, "Rashi"),
        ({"ref": "Onkelos Genesis 1:1", "collectiveTitle": {"en": "Onkelos Genesis"}}, "Onkelos"),
        ({"ref": "Rashi on Genesis 1:1:1"}, "Rashi"),
        ({"ref": "Ibn Ezra on Genesis 1:1:2"}, "Ibn Ezra"),
        ({"ref": "Onkelos Genesis 1:1"}, "Onkelos Genesis"),
        ({"ref": "Bartenura on Mishnah Berakhot 1:1:2"}, "Bartenura"),
        ({"ref": "Tosafot on Berakhot 2a:1:1"}, "Tosafot"),
        ({"ref": "Mishnah Berurah 1:1"}, "Mishnah Berurah"),
        ({"ref": "Kli Yakar on Genesis 1:1:1, Introduction"}, "Kli Yakar"),
    ])
    def test_groups_every_passage_of_a_work_together(self, entry, expected):
        assert sb._author_base(entry) == expected


class TestPickPreloadRefs:
    def test_most_read_commentators_first_and_capped(self):
        links = {"Commentary": [
            link("Sforno on Genesis 1:1:1"),
            link("Ramban on Genesis 1:1:1"),
            link("Rashi on Genesis 1:1:1"),
            link("Ibn Ezra on Genesis 1:1:1"),
            link("Radak on Genesis 1:1:1"),
        ]}
        assert sb.pick_preload_refs(links, authors=3) == [
            "Rashi on Genesis 1:1:1", "Ramban on Genesis 1:1:1", "Ibn Ezra on Genesis 1:1:1",
        ]

    def test_passages_of_one_commentator_in_natural_order_up_to_the_cap(self):
        links = {"Commentary": [link(f"Rashi on Genesis 1:1:{n}") for n in (10, 2, 1, 3, 11)]}
        assert sb.pick_preload_refs(links, per_author=3) == [
            "Rashi on Genesis 1:1:1", "Rashi on Genesis 1:1:2", "Rashi on Genesis 1:1:3",
        ]

    def test_commentators_outside_the_priority_list_are_left_to_load_on_demand(self):
        links = {"Commentary": [link("Some Obscure Work on Genesis 1:1:1")]}
        assert sb.pick_preload_refs(links) == []

    def test_targum_ranks_with_its_priority_entry(self):
        links = {
            "Commentary": [link("Rashi on Genesis 1:1:1")],
            "Targum": [link("Onkelos Genesis 1:1", collectiveTitle={"en": "Onkelos Genesis"})],
        }
        assert sb.pick_preload_refs(links) == ["Rashi on Genesis 1:1:1", "Onkelos Genesis 1:1"]

    def test_no_links(self):
        assert sb.pick_preload_refs({}) == []


# ── Links stage ───────────────────────────────────────────────────────────────


class TestBuildSidebarLinks:
    def test_slims_caches_and_serves_the_second_call_from_memory(self, monkeypatch):
        calls = []

        def fake_links(ref):
            calls.append(ref)
            return {"Commentary": [link("Rashi on Genesis 1:1:1")], "Reference": [link("x")]}

        monkeypatch.setattr("backend.sefaria_library.get_linked_texts", fake_links)
        first = sb.build_sidebar_links("Genesis 1:1")
        assert first == {"ref": "Genesis 1:1", "links": {"Commentary": [{"ref": "Rashi on Genesis 1:1:1"}]}}
        assert sb.build_sidebar_links("genesis 1:1") == first
        assert calls == ["Genesis 1:1"]

    def test_an_empty_result_is_not_pinned(self, monkeypatch):
        answers = [{}, {"Commentary": [link("Rashi on Genesis 1:1:1")]}]
        monkeypatch.setattr("backend.sefaria_library.get_linked_texts", lambda ref: answers.pop(0))
        assert sb.build_sidebar_links("Genesis 1:1")["links"] == {}
        assert sb.build_sidebar_links("Genesis 1:1")["links"] != {}


# ── Texts stage ───────────────────────────────────────────────────────────────


def _text_payload(ref, **extra):
    return {
        "ref": ref, "title": ref, "heTitle": "כותרת", "heRef": "מקור",
        "lines": [{"he": "עברית", "en": ""}, {"he": "עוד", "en": ""}],
        "he": ["עברית", "עוד"], "en": ["", ""],
        **extra,
    }


@pytest.fixture
def stub_upstream(monkeypatch):
    """Sefaria returns six Rashi/Ramban/... passages; translation is recorded
    and marks each line so tests can see whether it ran."""
    refs = [
        "Rashi on Genesis 1:1:1", "Rashi on Genesis 1:1:2", "Rashi on Genesis 1:1:3",
        "Ramban on Genesis 1:1:1", "Ramban on Genesis 1:1:2", "Ramban on Genesis 1:1:3",
        "Ibn Ezra on Genesis 1:1:1",
    ]
    state = {"fetched": [], "translated": [], "payloads": {}}

    def fake_links(ref):
        return {"Commentary": [link(r) for r in refs]}

    def fake_get_text(ref):
        state["fetched"].append(ref)
        payload = state["payloads"].setdefault(ref, _text_payload(ref))
        return payload

    def fake_translate(payload, max_lines=None, max_runtime_seconds=None):
        state["translated"].append(payload["ref"])
        for line in payload["lines"]:
            line["en"] = "translated"
        payload["translation_generated"] = True
        return payload

    monkeypatch.setattr("backend.sefaria_library.get_linked_texts", fake_links)
    monkeypatch.setattr("backend.sefaria_library.get_text", fake_get_text)
    monkeypatch.setattr("backend.helpers._fill_missing_english_lines", fake_translate)
    state["refs"] = refs
    return state


class TestBuildSidebarTexts:
    def test_preloads_the_picked_passages_keyed_by_their_own_ref(self, stub_upstream):
        result = sb.build_sidebar_texts("Genesis 1:1")
        assert result["ref"] == "Genesis 1:1"
        # 4 authors max but only 3 are present; 3 refs per author max.
        assert set(result["texts"]) == set(stub_upstream["refs"])
        assert sorted(stub_upstream["fetched"]) == sorted(stub_upstream["refs"])

    def test_only_the_first_passages_are_translated_and_flagged(self, stub_upstream):
        result = sb.build_sidebar_texts("Genesis 1:1")
        picked = sb.pick_preload_refs({"Commentary": [link(r) for r in stub_upstream["refs"]]})
        translated = set(picked[: sb.TRANSLATE_REFS])
        assert set(stub_upstream["translated"]) == translated
        for ref, text in result["texts"].items():
            if ref in translated:
                assert text["translation_attempted"] is True
                assert text["lines"][0]["en"] == "translated"
            else:
                assert "translation_attempted" not in text
                assert text["lines"][0]["en"] == ""

    def test_the_cached_upstream_payload_is_not_mutated_by_translation(self, stub_upstream):
        sb.build_sidebar_texts("Genesis 1:1")
        original = stub_upstream["payloads"]["Rashi on Genesis 1:1:1"]
        assert original["lines"][0]["en"] == ""
        assert "translation_generated" not in original

    def test_payload_is_slimmed(self, stub_upstream):
        text = sb.build_sidebar_texts("Genesis 1:1")["texts"]["Rashi on Genesis 1:1:1"]
        assert set(text) <= {
            "ref", "title", "heTitle", "heRef", "lines", "translation_generated",
            "translation_generated_count", "translation_source", "translation_note",
            "translation_attempted",
        }
        assert "he" not in text and "en" not in text

    def test_flat_lists_are_kept_when_there_are_no_lines(self, stub_upstream):
        stub_upstream["payloads"]["Rashi on Genesis 1:1:1"] = {
            "ref": "Rashi on Genesis 1:1:1", "he": ["א"], "en": ["a"], "lines": [],
        }
        text = sb.build_sidebar_texts("Genesis 1:1")["texts"]["Rashi on Genesis 1:1:1"]
        assert text["he"] == ["א"] and text["en"] == ["a"]

    def test_a_failing_or_errored_commentator_does_not_sink_the_rest(self, stub_upstream, monkeypatch):
        real = stub_upstream["payloads"]

        def flaky(ref):
            if ref.startswith("Ramban"):
                raise RuntimeError("upstream down")
            if ref == "Rashi on Genesis 1:1:2":
                return {"error": "not found"}
            return real.setdefault(ref, _text_payload(ref))

        monkeypatch.setattr("backend.sefaria_library.get_text", flaky)
        texts = sb.build_sidebar_texts("Genesis 1:1")["texts"]
        assert "Rashi on Genesis 1:1:1" in texts
        assert "Rashi on Genesis 1:1:2" not in texts
        assert not any(ref.startswith("Ramban") for ref in texts)

    def test_oversized_passages_are_left_out(self, stub_upstream, monkeypatch):
        normal = len(sb.json.dumps(sb._slim_text(_text_payload("Rashi on Genesis 1:1:2")), ensure_ascii=False))
        monkeypatch.setattr(sb, "MAX_TEXT_BYTES", normal + 100)
        stub_upstream["payloads"]["Rashi on Genesis 1:1:1"] = _text_payload(
            "Rashi on Genesis 1:1:1", lines=[{"he": "א" * 500, "en": ""}])
        texts = sb.build_sidebar_texts("Genesis 1:1")["texts"]
        assert "Rashi on Genesis 1:1:1" not in texts
        assert "Rashi on Genesis 1:1:2" in texts

    def test_the_bundle_total_is_capped_keeping_priority_order(self, stub_upstream, monkeypatch):
        one = len(sb.json.dumps(sb._slim_text(_text_payload("Rashi on Genesis 1:1:1")), ensure_ascii=False))
        monkeypatch.setattr(sb, "MAX_BUNDLE_TEXT_BYTES", one * 2 + 200)
        texts = sb.build_sidebar_texts("Genesis 1:1")["texts"]
        assert 1 <= len(texts) < len(stub_upstream["refs"])
        assert "Rashi on Genesis 1:1:1" in texts

    def test_second_call_is_served_from_cache(self, stub_upstream):
        sb.build_sidebar_texts("Genesis 1:1")
        fetched = len(stub_upstream["fetched"])
        sb.build_sidebar_texts("Genesis 1:1")
        assert len(stub_upstream["fetched"]) == fetched

    def test_no_commentary_is_an_empty_result_and_not_cached(self, monkeypatch):
        monkeypatch.setattr("backend.sefaria_library.get_linked_texts", lambda ref: {})
        assert sb.build_sidebar_texts("Genesis 1:1") == {"ref": "Genesis 1:1", "texts": {}}
        assert sb._texts_cache.get("genesis 1:1") is None

    def test_a_partial_result_is_served_but_kept_only_briefly(self, stub_upstream, monkeypatch):
        monkeypatch.setattr(sb, "TEXTS_DEADLINE_SECONDS", 0.2)
        real = stub_upstream["payloads"]

        def slow(ref):
            if ref.startswith("Ramban"):
                time.sleep(1.0)
            return real.setdefault(ref, _text_payload(ref))

        monkeypatch.setattr("backend.sefaria_library.get_text", slow)
        stored = {}
        original_set = sb._texts_cache.set
        monkeypatch.setattr(
            sb._texts_cache, "set",
            lambda key, value, ttl=None: (stored.update(ttl=ttl), original_set(key, value, ttl=ttl))[1],
        )
        started = time.monotonic()
        texts = sb.build_sidebar_texts("Genesis 1:1")["texts"]
        assert time.monotonic() - started < 0.9, "must not wait for the slow commentator"
        assert "Rashi on Genesis 1:1:1" in texts
        assert not any(ref.startswith("Ramban") for ref in texts)
        assert stored["ttl"] == 60

    def test_a_complete_result_uses_the_default_ttl(self, stub_upstream, monkeypatch):
        stored = {}
        original_set = sb._texts_cache.set
        monkeypatch.setattr(
            sb._texts_cache, "set",
            lambda key, value, ttl=None: (stored.update(ttl=ttl), original_set(key, value, ttl=ttl))[1],
        )
        sb.build_sidebar_texts("Genesis 1:1")
        assert stored["ttl"] is None


# ── Single flight ─────────────────────────────────────────────────────────────


class TestSingleFlight:
    def test_concurrent_callers_share_one_computation(self):
        started = threading.Event()
        release = threading.Event()
        runs = []

        def compute():
            runs.append(1)
            started.set()
            release.wait(2)
            return "value"

        results = []
        threads = [threading.Thread(target=lambda: results.append(sb._single_flight("k", compute)))
                   for _ in range(5)]
        threads[0].start()
        assert started.wait(2)
        for thread in threads[1:]:
            thread.start()
        time.sleep(0.05)
        release.set()
        for thread in threads:
            thread.join(2)
        assert results == ["value"] * 5
        assert len(runs) == 1
        assert sb._flights == {}

    def test_followers_compute_for_themselves_when_the_leader_fails(self):
        started = threading.Event()
        release = threading.Event()
        attempts = []

        def compute():
            attempts.append(threading.current_thread().name)
            if len(attempts) == 1:
                started.set()
                release.wait(2)
                raise RuntimeError("leader blew up")
            return "recovered"

        outcome = {}

        def leader():
            try:
                sb._single_flight("k", compute)
            except RuntimeError as exc:
                outcome["leader"] = str(exc)

        def follower():
            outcome["follower"] = sb._single_flight("k", compute)

        lead = threading.Thread(target=leader, name="leader")
        lead.start()
        assert started.wait(2)
        follow = threading.Thread(target=follower, name="follower")
        follow.start()
        time.sleep(0.05)
        release.set()
        lead.join(2)
        follow.join(2)
        assert outcome == {"leader": "leader blew up", "follower": "recovered"}
        assert sb._flights == {}

    def test_different_keys_do_not_wait_for_each_other(self):
        assert sb._single_flight("a", lambda: 1) == 1
        assert sb._single_flight("b", lambda: 2) == 2


# ── Route ─────────────────────────────────────────────────────────────────────


class TestSidebarRoute:
    def test_links_is_the_default_stage(self, test_client, monkeypatch):
        monkeypatch.setattr(
            "backend.sefaria_library.get_linked_texts",
            lambda ref: {"Commentary": [link("Rashi on Genesis 1:1:1")], "Reference": [link("x")]},
        )
        response = test_client.get("/api/sidebar/Genesis%201:1")
        assert response.status_code == 200
        assert response.get_json() == {
            "ref": "Genesis 1:1", "links": {"Commentary": [{"ref": "Rashi on Genesis 1:1:1"}]},
        }

    def test_texts_stage(self, test_client, stub_upstream):
        response = test_client.get("/api/sidebar/Genesis%201:1?stage=texts")
        assert response.status_code == 200
        body = response.get_json()
        assert body["ref"] == "Genesis 1:1"
        assert "Rashi on Genesis 1:1:1" in body["texts"]

    def test_unknown_stage_is_a_400(self, test_client):
        response = test_client.get("/api/sidebar/Genesis%201:1?stage=everything")
        assert response.status_code == 400

    def test_overlong_ref_is_a_400(self, test_client):
        response = test_client.get("/api/sidebar/" + "a" * (sb.MAX_REF_LENGTH + 1))
        assert response.status_code == 400

    def test_has_its_own_rate_limit_class(self):
        assert classify_route("/api/sidebar/Genesis%201:1") == "sidebar"
        assert classify_route("/api/text/Genesis%201:1") == "fanout"


# ── Client/server list drift ──────────────────────────────────────────────────


def test_commentary_priority_matches_the_client_list():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    block = re.search(r"const COMMENTARY_PRIORITY = \[(.*?)\];", html, re.DOTALL)
    assert block, "COMMENTARY_PRIORITY not found in templates/index.html"
    client = [a or b for a, b in re.findall(r"'([^']*)'|\"([^\"]*)\"", block.group(1))]
    assert tuple(client) == sb.COMMENTARY_PRIORITY
