"""
Behavioural coverage tests for the residual branches of backend/ai_tools.py,
backend/routes_library.py, backend/rag.py and backend/cache.py that the
neighbouring suites (test_ai_tools.py, test_routes_library*.py, test_rag.py,
test_shared_redis_cache.py) do not reach.

Every test asserts an observable result (return value, HTTP status/body,
arguments a collaborator received, or a logged event), so changing the
branch it covers changes the outcome rather than merely skipping a line.
"""

from __future__ import annotations

import json
import logging

import pytest

import app
import backend.cache as cache_module
import backend.rag as rag
import backend.routes_library as routes_library_module
import backend.sefaria_library as sl
from backend import ai_tools, calendar_service, customs


# ═════════════════════════════════════════════════════════════════════════════
# backend/ai_tools.py
# ═════════════════════════════════════════════════════════════════════════════

# ── _clamp_int ───────────────────────────────────────────────────────────────

class TestClampInt:
    def test_in_range_int_passes_through(self):
        assert ai_tools._clamp_int(5, 8, 1, 20) == 5

    def test_numeric_string_is_coerced(self):
        assert ai_tools._clamp_int("7", 8, 1, 20) == 7

    def test_above_upper_bound_is_clamped_down(self):
        assert ai_tools._clamp_int(999, 8, 1, 20) == 20

    def test_below_lower_bound_is_clamped_up(self):
        assert ai_tools._clamp_int(-3, 8, 1, 20) == 1

    def test_unparseable_value_falls_back_to_default(self):
        assert ai_tools._clamp_int("many", 8, 1, 20) == 8
        assert ai_tools._clamp_int(None, 8, 1, 20) == 8


async def test_search_library_honours_a_valid_max_results(monkeypatch):
    captured = {}

    def fake_search(query, max_results, filters=None):
        captured["max_results"] = max_results
        return [{"ref": f"Ref {i}"} for i in range(10)]

    monkeypatch.setattr(ai_tools.sefaria_library, "search_library", fake_search)
    result = await ai_tools.execute_tool("search_library", {"query": "blessing", "max_results": 3})
    assert captured["max_results"] == 3
    assert [hit["ref"] for hit in result["results"]] == ["Ref 0", "Ref 1", "Ref 2"]


async def test_search_library_clamps_an_oversized_max_results(monkeypatch):
    captured = {}

    def fake_search(query, max_results, filters=None):
        captured["max_results"] = max_results
        return []

    monkeypatch.setattr(ai_tools.sefaria_library, "search_library", fake_search)
    await ai_tools.execute_tool("search_library", {"query": "blessing", "max_results": 500})
    assert captured["max_results"] == 20


# ── 1. search_judaic_texts: de-duplication ───────────────────────────────────

async def test_search_judaic_texts_dedupes_curated_refs(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria, "find_refs_for_question",
                        lambda q: ["Berakhot 2a", "Berakhot 2a", "Shabbat 31a"])
    monkeypatch.setattr(ai_tools.sefaria_library, "search_library", lambda q, n: [])

    result = await ai_tools.execute_tool("search_judaic_texts", {"query": "shema"})
    assert result["results"] == [
        {"ref": "Berakhot 2a", "match_type": "curated_topic"},
        {"ref": "Shabbat 31a", "match_type": "curated_topic"},
    ]


async def test_search_judaic_texts_skips_blank_and_already_seen_search_hits(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria, "find_refs_for_question", lambda q: ["Berakhot 2a"])
    monkeypatch.setattr(ai_tools.sefaria_library, "search_library", lambda q, n: [
        {"ref": "Berakhot 2a", "title": "dup of curated"},
        {"ref": "", "title": "blank ref"},
        None,
        {"ref": "Shabbat 31a", "title": "Shabbat", "categories": ["Talmud"], "text": "Hillel said"},
        {"ref": "Shabbat 31a", "title": "dup of search"},
    ])

    result = await ai_tools.execute_tool("search_judaic_texts", {"query": "shema"})
    assert result["results"] == [
        {"ref": "Berakhot 2a", "match_type": "curated_topic"},
        {"ref": "Shabbat 31a", "match_type": "search", "title": "Shabbat",
         "categories": ["Talmud"], "excerpt": "Hillel said"},
    ]


async def test_search_judaic_texts_truncates_to_max_results(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria, "find_refs_for_question", lambda q: ["A 1", "B 2"])
    monkeypatch.setattr(ai_tools.sefaria_library, "search_library",
                        lambda q, n: [{"ref": "C 3"}, {"ref": "D 4"}])

    result = await ai_tools.execute_tool("search_judaic_texts", {"query": "x", "max_results": 3})
    assert [r["ref"] for r in result["results"]] == ["A 1", "B 2", "C 3"]


# ── 4. get_zmanim: engine failure ────────────────────────────────────────────

@pytest.mark.parametrize("engine_payload", [{"error": "bad tz"}, None, ["not", "a", "dict"]])
async def test_get_zmanim_engine_failure_returns_stable_error(monkeypatch, engine_payload):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim",
                        lambda lat, lon, tz, community: engine_payload)
    result = await ai_tools.execute_tool("get_zmanim", {"lat": 40.0, "lon": -74.0})
    assert result == {"error": "zmanim computation failed for this location/date"}


# ── 8. get_holidays: non-numeric location ────────────────────────────────────

async def test_get_holidays_rejects_non_numeric_lat_without_calling_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_monthly_events",
                        lambda lat, lon, tz: calls.append((lat, lon, tz)) or [])

    result = await ai_tools.execute_tool("get_holidays", {
        "start_date": "2026-09-01", "end_date": "2026-09-30", "lat": "north", "lon": 35.0,
    })
    assert result == {"error": "lat/lon must be numeric"}
    assert calls == []


async def test_get_holidays_rejects_non_numeric_context_lon(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_monthly_events", lambda lat, lon, tz: [])
    result = await ai_tools.execute_tool(
        "get_holidays",
        {"start_date": "2026-09-01", "end_date": "2026-09-30"},
        context={"lat": 40.0, "lon": "east"},
    )
    assert result == {"error": "lat/lon must be numeric"}


# ── 12. translate_text: nothing translated ───────────────────────────────────

async def test_translate_text_reports_untranslated_when_hebrew_engine_returns_nothing(monkeypatch):
    monkeypatch.setattr(ai_tools, "_translate_hebrew_text_online", lambda t: (None, None))
    result = await ai_tools.execute_tool("translate_text", {"text": "ברכה", "direction": "he_to_en"})
    assert result == {"text": "ברכה", "translated": False}


async def test_translate_text_en_to_he_uses_english_engine_and_reports_untranslated(monkeypatch):
    seen = []

    def fake_english(text):
        seen.append(text)
        return ("", None)

    def fake_hebrew(text):
        raise AssertionError("he_to_en engine must not be used for en_to_he")

    monkeypatch.setattr(ai_tools, "_translate_english_text_online", fake_english)
    monkeypatch.setattr(ai_tools, "_translate_hebrew_text_online", fake_hebrew)

    result = await ai_tools.execute_tool("translate_text", {"text": "blessing", "direction": "en_to_he"})
    assert seen == ["blessing"]
    assert result == {"text": "blessing", "translated": False}


# ── 15. get_community_profile: _cap_nested / _read_json ─────────────────────

class TestCapNested:
    def test_scalars_pass_through_unchanged(self):
        assert ai_tools._cap_nested("text") == "text"
        assert ai_tools._cap_nested(42) == 42
        assert ai_tools._cap_nested(None) is None

    def test_list_is_capped_at_default_ten(self):
        assert ai_tools._cap_nested(list(range(25))) == list(range(10))

    def test_dict_values_are_capped_recursively_with_scalars_kept(self):
        value = {"codes": list(range(15)), "note": "keep me", "deep": {"inner": list(range(15))}}
        assert ai_tools._cap_nested(value) == {
            "codes": list(range(10)),
            "note": "keep me",
            "deep": {"inner": list(range(10))},
        }

    def test_dict_keys_are_capped_too(self):
        value = {f"k{i}": i for i in range(20)}
        assert list(ai_tools._cap_nested(value, max_items=3)) == ["k0", "k1", "k2"]


def test_read_json_reads_utf8_file(tmp_path):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"identity": "קהילה תימנית", "n": 3}, ensure_ascii=False), encoding="utf-8")
    assert ai_tools._read_json(str(path)) == {"identity": "קהילה תימנית", "n": 3}


def test_read_json_propagates_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        ai_tools._read_json(str(tmp_path / "nope.json"))


async def test_get_community_profile_reads_the_slug_file_from_customs_dir(monkeypatch, tmp_path):
    (tmp_path / "yemenite.json").write_text(json.dumps({
        "heritage_id": "yemenite",
        "identity": "Yemenite Jewish community",
        "languages": ["Hebrew", "Arabic"],
        "historical_background": "x" * 1500,
        "core_halachic_authorities": {"primary_codes": ["Rambam"] * 20},
        "unique_minhagim": {"prayer": ["nusach"] * 3},
    }), encoding="utf-8")
    monkeypatch.setattr(customs, "CUSTOMS_DIR", str(tmp_path))

    result = await ai_tools.execute_tool("get_community_profile", {"community": "Yemenite"})
    assert result["community"] == "Yemenite"
    assert result["heritage_id"] == "yemenite"
    assert result["languages"] == ["Hebrew", "Arabic"]
    assert len(result["historical_background"]) == 1201  # 1200 chars + ellipsis
    assert result["historical_background"].endswith("…")
    assert result["core_halachic_authorities"] == {"primary_codes": ["Rambam"] * 10}
    assert result["unique_minhagim"] == {"prayer": ["nusach"] * 3}


async def test_get_community_profile_missing_file_degrades_to_error(monkeypatch, tmp_path):
    monkeypatch.setattr(customs, "CUSTOMS_DIR", str(tmp_path))  # no yemenite.json inside
    result = await ai_tools.execute_tool("get_community_profile", {"community": "Yemenite"})
    assert result["error"].startswith("could not load profile for Yemenite:")


async def test_get_community_profile_corrupt_json_degrades_to_error(monkeypatch, tmp_path):
    (tmp_path / "yemenite.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(customs, "CUSTOMS_DIR", str(tmp_path))
    result = await ai_tools.execute_tool("get_community_profile", {"community": "Yemenite"})
    assert result["error"].startswith("could not load profile for Yemenite:")


# ── 19. get_daily_zmanim_summary ─────────────────────────────────────────────

async def test_get_daily_zmanim_summary_rejects_non_numeric_lat_without_calling_engine(monkeypatch):
    calls = []
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim",
                        lambda *args: calls.append(args) or {})
    result = await ai_tools.execute_tool("get_daily_zmanim_summary", {"lat": "abc", "lon": -74.0})
    assert result == {"error": "lat/lon must be numeric"}
    assert calls == []


async def test_get_daily_zmanim_summary_rejects_non_numeric_lon_from_arguments(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim", lambda *args: {})
    result = await ai_tools.execute_tool("get_daily_zmanim_summary", {"lat": 40.0, "lon": "west"})
    assert result == {"error": "lat/lon must be numeric"}


@pytest.mark.parametrize("engine_payload", [{"error": "bad tz"}, None, "oops"])
async def test_get_daily_zmanim_summary_engine_failure_returns_stable_error(monkeypatch, engine_payload):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim",
                        lambda lat, lon, tz, community: engine_payload)
    result = await ai_tools.execute_tool("get_daily_zmanim_summary", {"lat": 40.0, "lon": -74.0})
    assert result == {"error": "zmanim computation failed for this location/date"}


# ── 20. convert_measurements ─────────────────────────────────────────────────

async def test_convert_measurements_non_numeric_quantity_defaults_to_one():
    result = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "revi_it", "opinion": "chazon_ish", "quantity": "lots"})
    assert result["quantity"] == 1.0
    assert result["chazon_ish"]["metric"] == 150.0


async def test_convert_measurements_negative_quantity_clamps_to_zero():
    result = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "revi_it", "opinion": "chaim_naeh", "quantity": -5})
    assert result["quantity"] == 0.0
    assert result["chaim_naeh"]["metric"] == 0.0


async def test_convert_measurements_mil_reports_metres_and_feet():
    result = await ai_tools.execute_tool("convert_measurements", {"measure": "mil"})
    assert result["chazon_ish"] == {
        "metric": 1152.0, "metric_unit": "m", "imperial": 3779.53, "imperial_unit": "ft",
    }
    assert result["chaim_naeh"] == {
        "metric": 960.0, "metric_unit": "m", "imperial": 3149.61, "imperial_unit": "ft",
    }


async def test_convert_measurements_length_and_volume_units_use_their_own_imperial_unit():
    amah = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "amah", "opinion": "chazon_ish"})
    assert amah["chazon_ish"] == {
        "metric": 57.6, "metric_unit": "cm", "imperial": 22.68, "imperial_unit": "in",
    }
    kezayit = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "kezayit", "opinion": "chazon_ish"})
    assert kezayit["chazon_ish"] == {
        "metric": 50.0, "metric_unit": "ml", "imperial": 1.69, "imperial_unit": "fl oz",
    }


@pytest.mark.parametrize("measure, derived_from", [
    ("kav", "revi_it × 16"),
    ("mil", "amah × 2000"),
])
async def test_convert_measurements_derived_measures_report_their_derivation(measure, derived_from):
    result = await ai_tools.execute_tool("convert_measurements", {"measure": measure})
    assert result["derived_from"] == derived_from


async def test_convert_measurements_base_measure_has_no_derivation():
    result = await ai_tools.execute_tool("convert_measurements", {"measure": "amah"})
    assert "derived_from" not in result


async def test_convert_measurements_kav_is_sixteen_revi_it():
    kav = await ai_tools.execute_tool("convert_measurements", {"measure": "kav"})
    revi_it = await ai_tools.execute_tool("convert_measurements", {"measure": "revi_it"})
    assert kav["chazon_ish"]["metric"] == revi_it["chazon_ish"]["metric"] * 16
    assert kav["chaim_naeh"]["metric"] == revi_it["chaim_naeh"]["metric"] * 16


# ── 21. calculate_hebrew_date_math ───────────────────────────────────────────

async def test_calculate_hebrew_date_math_gregorian_to_hebrew():
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "gregorian_to_hebrew", "gregorian_date": "2026-08-21",
    })
    assert result["hebrew_date"] == "8 Elul 5786"
    assert result["gregorian_date"] == "2026-08-21"


async def test_calculate_hebrew_date_math_gregorian_to_hebrew_forwards_the_date(monkeypatch):
    seen = []
    monkeypatch.setattr(calendar_service.PyluachEngine, "gregorian_to_hebrew",
                        staticmethod(lambda d=None: seen.append(d) or {"hebrew_date": "stub"}))
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "gregorian_to_hebrew", "gregorian_date": "2026-01-02",
    })
    assert seen == ["2026-01-02"]
    assert result == {"hebrew_date": "stub"}


async def test_calculate_hebrew_date_math_hebrew_to_gregorian():
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "hebrew_to_gregorian", "hebrew_year": 5786, "hebrew_month": 5, "hebrew_day": 1,
    })
    assert result["gregorian_date"] == "2026-07-15"
    assert result["hebrew_date"] == "1 Av 5786"


async def test_calculate_hebrew_date_math_hebrew_to_gregorian_forwards_year_month_day_in_order(monkeypatch):
    seen = []
    monkeypatch.setattr(calendar_service.PyluachEngine, "hebrew_to_gregorian",
                        staticmethod(lambda y, m, d: seen.append((y, m, d)) or {"gregorian_date": "stub"}))
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "hebrew_to_gregorian", "hebrew_year": 5790, "hebrew_month": 7, "hebrew_day": 15,
    })
    assert seen == [(5790, 7, 15)]
    assert result == {"gregorian_date": "stub"}


# ═════════════════════════════════════════════════════════════════════════════
# backend/routes_library.py
# ═════════════════════════════════════════════════════════════════════════════

class TestParseTopicAltNodeOversized:
    def test_oversized_ref_body_is_rejected_even_though_it_holds_a_range(self):
        node = {"title": "Laws", "heTitle": "הלכות",
                "wholeRef": "Shulchan Arukh, Orach Chayim 1-7 " + "x" * 600}
        assert routes_library_module._parse_topic_alt_node(node) is None

    def test_ref_at_the_limit_is_still_parsed(self):
        prefix = "Shulchan Arukh, Orach Chayim 1-7 "
        ref = prefix + "x" * (routes_library_module._MAX_REF_SEGMENT_LEN - len(prefix))
        assert len(ref) == routes_library_module._MAX_REF_SEGMENT_LEN
        result = routes_library_module._parse_topic_alt_node({"title": "Laws", "wholeRef": ref})
        assert result == {"label": "Laws", "heLabel": "", "fromSection": 1, "toSection": 7}


class TestExtractIndexSectionsWithoutUsableAlts:
    def test_alts_with_neither_chapters_nor_topic_returns_empty(self):
        entry = {"alts": {"Parasha": {"nodes": [{"title": "Bereshit", "wholeRef": "Genesis 1-6"}]}}}
        assert routes_library_module._extract_index_sections("Genesis", entry) == []

    def test_empty_alts_returns_empty(self):
        assert routes_library_module._extract_index_sections("Genesis", {"alts": {}}) == []

    def test_non_dict_topic_alt_returns_empty(self):
        entry = {"alts": {"Topic": ["not", "a", "dict"]}}
        assert routes_library_module._extract_index_sections("Genesis", entry) == []

    def test_non_dict_chapters_alt_falls_through_to_empty(self):
        entry = {"alts": {"Chapters": "nope"}}
        assert routes_library_module._extract_index_sections("Berakhot", entry) == []


class TestParseSectionSchemaUnparseableLength:
    @pytest.mark.parametrize("lengths", [["many"], [None], [[6]]])
    def test_non_integer_first_length_returns_none(self, lengths):
        entry = {"schema": {"lengths": lengths, "sectionNames": ["Chapter"]}}
        assert routes_library_module._parse_section_schema_for_synthesis(entry) is None

    def test_numeric_string_first_length_is_accepted(self):
        entry = {"schema": {"lengths": ["6"], "sectionNames": ["Daf"], "addressTypes": ["Talmud"]}}
        assert routes_library_module._parse_section_schema_for_synthesis(entry) == (6, "daf", "talmud")


class TestLibraryLeafRefsEdgeCases:
    def test_non_list_leaf_refs_result_falls_back_to_synthesis(self, test_client, monkeypatch):
        monkeypatch.setattr(sl, "get_index_leaf_refs", lambda title, max_refs=120: None)
        monkeypatch.setattr(sl, "get_index_entry", lambda title: {
            "schema": {"lengths": [3], "sectionNames": ["Chapter"], "addressTypes": ["Integer"]},
        })
        response = test_client.get("/api/library/leaf-refs?title=Pirkei Avot")
        assert response.status_code == 200
        assert response.get_json() == {
            "title": "Pirkei Avot",
            "refs": ["Pirkei Avot 1", "Pirkei Avot 2", "Pirkei Avot 3"],
            "sections": [],
        }

    def test_index_entry_failure_keeps_leaf_refs_and_yields_no_sections(self, test_client, monkeypatch):
        monkeypatch.setattr(
            sl, "get_index_leaf_refs",
            lambda title, max_refs=120: ["Some Work 1", "Some Work 2", "Some Work 3"],
        )

        def _raise(title):
            raise RuntimeError("index unavailable")

        monkeypatch.setattr(sl, "get_index_entry", _raise)
        response = test_client.get("/api/library/leaf-refs?title=Some Work")
        assert response.status_code == 200
        assert response.get_json() == {
            "title": "Some Work",
            "refs": ["Some Work 1", "Some Work 2", "Some Work 3"],
            "sections": [],
        }


class TestGetTextInlineBlockedUpstream:
    def test_sefaria_blocked_error_type_returns_503_with_payload(self, test_client, monkeypatch):
        payload = {"error": "Upstream refused", "error_type": "sefaria_blocked"}
        monkeypatch.setattr(sl, "get_text", lambda ref: dict(payload))
        response = test_client.get("/api/text/Genesis%201:1")
        assert response.status_code == 503
        assert response.get_json() == payload

    def test_blocked_wording_in_error_message_returns_503(self, test_client, monkeypatch):
        payload = {"error": "Request was BLOCKED by upstream firewall"}
        monkeypatch.setattr(sl, "get_text", lambda ref: dict(payload))
        response = test_client.get("/api/text/Genesis%201:1")
        assert response.status_code == 503
        assert response.get_json() == payload

    def test_other_errors_are_not_promoted_to_503(self, test_client, monkeypatch):
        payload = {"error": "Ref not found", "error_type": "not_found"}
        monkeypatch.setattr(sl, "get_text", lambda ref: dict(payload))
        response = test_client.get("/api/text/Nowhere%201:1")
        assert response.status_code == 200
        assert response.get_json() == payload

    def test_blocked_response_skips_english_gap_filling(self, test_client, monkeypatch):
        monkeypatch.setattr(sl, "get_text", lambda ref: {"error": "blocked", "error_type": "sefaria_blocked"})

        def _boom(data):
            raise AssertionError("gap filling must not run on a blocked response")

        monkeypatch.setattr(routes_library_module, "_fill_missing_english_lines", _boom)
        response = test_client.get("/api/text/Genesis%201:1")
        assert response.status_code == 503


class TestNormalizeExportLinesSkipsNonDicts:
    def test_non_dict_entries_are_skipped_but_still_consume_a_segment_number(self):
        lines = ["junk", None, 7, ["x"], {"he": "אבג", "en": "abc"}, {"he": "", "en": ""}, {"en": "only en"}]
        assert routes_library_module._normalize_export_lines(lines) == [
            {"segment": "5", "he": "אבג", "en": "abc"},
            {"segment": "7", "he": "", "en": "only en"},
        ]

    def test_explicit_segment_wins_over_position(self):
        result = routes_library_module._normalize_export_lines(
            ["junk", {"segment": " 12 ", "he": "א", "en": ""}])
        assert result == [{"segment": "12", "he": "א", "en": ""}]


class TestTextGraphSkipsBlankTargets:
    def test_items_without_a_ref_add_no_nodes_or_edges(self, test_client, monkeypatch):
        monkeypatch.setattr(sl, "get_linked_texts", lambda ref: {
            "Commentary": [{"ref": ""}, None, {"title": "no ref key"}, {"ref": "Rashi on Genesis 1:1"}],
            "Targum": [{"ref": "Onkelos Genesis 1:1"}],
        })
        response = test_client.get("/api/text/Genesis%201:1/graph")
        assert response.status_code == 200
        body = response.get_json()
        assert [n["id"] for n in body["nodes"]] == [
            "Genesis 1:1", "Rashi on Genesis 1:1", "Onkelos Genesis 1:1"]
        assert body["edges"] == [
            {"source": "Genesis 1:1", "target": "Rashi on Genesis 1:1", "label": "Commentary"},
            {"source": "Genesis 1:1", "target": "Onkelos Genesis 1:1", "label": "Targum"},
        ]


# ═════════════════════════════════════════════════════════════════════════════
# backend/rag.py
# ═════════════════════════════════════════════════════════════════════════════

class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Chainable Supabase query stub recording every call made on it."""

    def __init__(self, data=None):
        self._data = data if data is not None else []
        self.calls = []

    def table(self, name):
        self.calls.append(("table", name))
        return self

    def select(self, *a, **k):
        self.calls.append(("select", a, k))
        return self

    def insert(self, *a, **k):
        self.calls.append(("insert", a, k))
        return self

    def ilike(self, *a, **k):
        self.calls.append(("ilike", a, k))
        return self

    def or_(self, *a, **k):
        self.calls.append(("or_", a, k))
        return self

    def eq(self, *a, **k):
        self.calls.append(("eq", a, k))
        return self

    def order(self, *a, **k):
        self.calls.append(("order", a, k))
        return self

    def limit(self, *a, **k):
        self.calls.append(("limit", a, k))
        return self

    def execute(self):
        return _FakeResult(self._data)


class TestBuildKnowledgeTextOrFilterSkipsEmptyKeywords:
    def test_symbol_only_keyword_is_dropped(self):
        assert rag._build_knowledge_text_or_filter(["!!!", "shabbat"]) == (
            "topic.ilike.%shabbat%,content.ilike.%shabbat%"
        )

    def test_blank_and_none_keywords_are_dropped(self):
        assert rag._build_knowledge_text_or_filter(["", "   ", None, "kashrut"]) == (
            "topic.ilike.%kashrut%,content.ilike.%kashrut%"
        )

    def test_all_keywords_empty_after_cleaning_yields_empty_filter(self):
        assert rag._build_knowledge_text_or_filter(["!!!", "'", "%%"]) == ""


class TestRankCommunityKnowledgeRowsSkipsNonDicts:
    def test_non_dict_rows_are_ignored(self):
        good = {"topic": "shabbat candles", "halakhic_source": "", "content": "", "community_name": ""}
        ranked = rag._rank_community_knowledge_rows(
            ["junk", None, 42, ["list"], good], ["shabbat"], None, "All")
        assert ranked == [(8, good)]

    def test_only_non_dict_rows_yields_nothing(self):
        assert rag._rank_community_knowledge_rows(["a", None], ["shabbat"], None, "All") == []


class TestRetrieveCommunityKnowledgeIgnoresMalformedRows:
    def test_non_dict_rows_from_the_database_do_not_break_ranking(self, monkeypatch):
        rows = [
            "garbage",
            None,
            {"id": "1", "community_name": "Ashkenaz", "topic": "shabbat candles",
             "halakhic_source": "SA", "content": "lighting"},
        ]

        class _Client:
            def table(self, name):
                return _FakeQuery(data=rows)

        monkeypatch.setattr(app, "_get_supabase_client", lambda: _Client())
        monkeypatch.setattr(app, "RAG_TOP_KNOWLEDGE_ROWS", 5)
        monkeypatch.setattr(app, "_extract_query_keywords", lambda q, max_keywords=10: ["shabbat"])
        monkeypatch.setattr(app, "SUPABASE_COMMUNITY_KNOWLEDGE_TABLE", "community_knowledge")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text: str(text or ""))
        monkeypatch.setattr(app, "_detect_community_in_text", lambda q: None)

        result = rag._retrieve_community_knowledge("shabbat candles", canonical_lens="All")
        assert [r["id"] for r in result] == ["1"]


class TestFetchUserMemorySummariesSkipsNonDictRows:
    def test_non_dict_rows_are_ignored(self, monkeypatch):
        rows = [
            "garbage",
            None,
            {"summary": "Asked about kashrut.", "created_at": "2026-01-01"},
            12,
        ]
        client = type("C", (), {"table": lambda self, name: _FakeQuery(data=rows)})()
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda bearer_token=None: client)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")
        monkeypatch.setattr(app, "_normalize_rag_text", lambda text, max_chars=260: str(text or ""))

        assert rag._fetch_user_memory_summaries("user-1") == [
            {"summary": "Asked about kashrut.", "created_at": "2026-01-01"},
        ]


class TestStoreUserMemorySummaryClientFallback:
    def test_falls_back_to_service_client_when_scoped_client_missing_and_rls_not_strict(self, monkeypatch):
        service = _FakeQuery()

        class _Client:
            def table(self, name):
                service.table(name)
                return service

        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", False)
        monkeypatch.setattr(app, "_get_supabase_client", lambda: _Client())
        monkeypatch.setattr(app, "_build_interaction_summary", lambda q, a: "User asked about kashrut.")
        monkeypatch.setattr(app, "SUPABASE_USER_MEMORIES_TABLE", "user_memories")

        rag._store_user_memory_summary("user-1", "q", "a")

        assert ("table", "user_memories") in service.calls
        inserts = [c for c in service.calls if c[0] == "insert"]
        assert len(inserts) == 1
        payload = inserts[0][1][0]
        assert payload["user_id"] == "user-1"
        assert payload["summary"] == "User asked about kashrut."

    def test_strict_rls_never_falls_back_to_the_service_client(self, monkeypatch):
        def _service_client():
            raise AssertionError("service-role client must not be used under STRICT_SUPABASE_RLS")

        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", True)
        monkeypatch.setattr(app, "_get_supabase_client", _service_client)
        monkeypatch.setattr(app, "_build_interaction_summary", lambda q, a: "summary")
        rag._store_user_memory_summary("user-1", "q", "a")  # must not raise or write

    def test_no_client_at_all_writes_nothing(self, monkeypatch):
        monkeypatch.setattr(app, "_get_user_scoped_supabase_client", lambda: None)
        monkeypatch.setattr(app, "STRICT_SUPABASE_RLS", False)
        monkeypatch.setattr(app, "_get_supabase_client", lambda: None)

        def _summary(q, a):
            raise AssertionError("summary must not be built without a client")

        monkeypatch.setattr(app, "_build_interaction_summary", _summary)
        rag._store_user_memory_summary("user-1", "q", "a")


# ═════════════════════════════════════════════════════════════════════════════
# backend/cache.py
# ═════════════════════════════════════════════════════════════════════════════

class TestBuildSharedRedisClientFailureFallback:
    def test_malformed_url_falls_back_to_none_and_logs_a_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "not-a-valid-redis-url")
        with caplog.at_level(logging.WARNING, logger=cache_module.logger.name):
            client = cache_module._build_shared_redis_client()

        assert client is None
        records = [r for r in caplog.records if r.name == cache_module.logger.name]
        assert len(records) == 1
        record = records[0]
        assert record.levelno == logging.WARNING
        message = record.getMessage()
        assert "RATE_LIMIT_REDIS_URL is set but invalid" in message
        assert "falling back to in-process-only caching" in message
        assert "ValueError" in message  # exception type is surfaced
        # The traceback itself must be attached (exc_info=False would leave a
        # falsy value here, which `is not None` alone would not catch).
        assert record.exc_info
        assert record.exc_info[0] is ValueError

    def test_client_construction_error_is_swallowed_and_reported(self, monkeypatch, caplog):
        def _boom(url):
            raise RuntimeError("redis lib exploded")

        monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "redis://example.invalid:6379")
        monkeypatch.setattr(cache_module, "_SyncRedisClient", _boom)
        with caplog.at_level(logging.WARNING, logger=cache_module.logger.name):
            client = cache_module._build_shared_redis_client()

        assert client is None
        messages = [r.getMessage() for r in caplog.records if r.name == cache_module.logger.name]
        assert any("RuntimeError: redis lib exploded" in m for m in messages)

    def test_url_is_stripped_before_being_handed_to_the_client(self, monkeypatch):
        seen = []

        class _Recorder:
            def __init__(self, url):
                seen.append(url)

        monkeypatch.setenv("RATE_LIMIT_REDIS_URL", "  redis://example.invalid:6379  ")
        monkeypatch.setattr(cache_module, "_SyncRedisClient", _Recorder)
        client = cache_module._build_shared_redis_client()
        assert isinstance(client, _Recorder)
        assert seen == ["redis://example.invalid:6379"]
