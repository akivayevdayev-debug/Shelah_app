"""
Tests for backend/ai_tools.py -- the agentic tool-use registry (plan.md
§9.2/§9.2b, Prompt 20).

Per plan.md §9.6: each tool wrapper returns the right shape from mocked
engines/APIs (offline, via monkeypatch on each handler's direct backend
dependency -- more precise than relying on generic HTTP-layer mocking for
functions whose exact response shape matters to the assertions here); and
param validation (bad lat/lon, bad dates, missing required fields) is
rejected with a stable {"error": ...} shape rather than raising.

execute_tool() is the single choke point for timeout / exception / circuit
bookkeeping (per its own docstring), so those cross-cutting behaviors are
tested once against a trivial fake tool rather than duplicated per handler.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from backend import ai_tools


def _patch_handler(monkeypatch, tool_name, handler=None, timeout_seconds=None):
    """ToolSpec is a frozen dataclass -- can't monkeypatch a field on the
    live instance. Swap the whole _TOOLS_BY_NAME entry for a
    dataclasses.replace()'d copy instead; monkeypatch.setitem restores the
    original entry automatically at teardown.
    """
    original = ai_tools._TOOLS_BY_NAME[tool_name]
    kwargs = {}
    if handler is not None:
        kwargs["handler"] = handler
    if timeout_seconds is not None:
        kwargs["timeout_seconds"] = timeout_seconds
    monkeypatch.setitem(ai_tools._TOOLS_BY_NAME, tool_name, dataclasses.replace(original, **kwargs))


# ── Registry-level behavior ──────────────────────────────────────────────────

def test_get_tool_schemas_excludes_web_search_by_default():
    names = [t["name"] for t in ai_tools.get_tool_schemas()]
    assert "web_search" not in names
    assert "search_judaic_texts" in names
    assert len(names) == len(ai_tools.TOOLS) - 1


def test_get_tool_schemas_includes_web_search_when_unlocked():
    names = [t["name"] for t in ai_tools.get_tool_schemas(include_web_search=True)]
    assert "web_search" in names
    assert len(names) == len(ai_tools.TOOLS)


def test_get_tool_schemas_shape_matches_anthropic_tools_param():
    for schema in ai_tools.get_tool_schemas(include_web_search=True):
        assert set(schema) == {"name", "description", "input_schema"}
        assert schema["input_schema"]["type"] == "object"
        assert "properties" in schema["input_schema"]


def test_every_tool_name_is_unique():
    names = [t.name for t in ai_tools.TOOLS]
    assert len(names) == len(set(names))


async def test_execute_tool_unknown_name_returns_error_not_raise():
    result = await ai_tools.execute_tool("not_a_real_tool", {})
    assert result == {"error": "unknown tool: not_a_real_tool"}


async def test_execute_tool_none_context_defaults_to_empty_dict():
    result = await ai_tools.execute_tool("get_hebrew_date", {"gregorian_date": "2026-08-21"}, context=None)
    assert "hebrew_date" in result


async def test_execute_tool_timeout_degrades_to_error(monkeypatch):
    async def _hang(arguments, context):
        await asyncio.sleep(10)
        return {}

    _patch_handler(monkeypatch, "get_daily_study", handler=_hang, timeout_seconds=0.05)

    result = await ai_tools.execute_tool("get_daily_study", {})
    assert result == {"error": "get_daily_study timed out"}


async def test_execute_tool_handler_exception_degrades_to_error(monkeypatch):
    async def _boom(arguments, context):
        raise ValueError("kaboom")

    _patch_handler(monkeypatch, "get_daily_study", handler=_boom)

    result = await ai_tools.execute_tool("get_daily_study", {})
    assert result == {"error": "get_daily_study failed: ValueError"}


async def test_execute_tool_non_dict_return_is_coerced_to_error(monkeypatch):
    async def _returns_a_list(arguments, context):
        return ["not", "a", "dict"]

    _patch_handler(monkeypatch, "get_daily_study", handler=_returns_a_list)

    result = await ai_tools.execute_tool("get_daily_study", {})
    assert result == {"error": "get_daily_study returned a non-dict result"}


async def test_execute_tool_circuit_open_hides_the_tool(monkeypatch):
    import backend.health_check as health_check_module
    monkeypatch.setattr(health_check_module.health, "is_healthy", lambda service: False)

    result = await ai_tools.execute_tool("search_judaic_texts", {"query": "Shabbat"})
    assert result == {"error": "search_judaic_texts is temporarily unavailable (circuit open for sefaria)"}


async def test_execute_tool_records_health_success(monkeypatch):
    calls = []
    import backend.health_check as health_check_module
    monkeypatch.setattr(health_check_module.health, "record_success", lambda service: calls.append(service))

    await ai_tools.execute_tool("get_hebrew_date", {"gregorian_date": "2026-08-21"})
    assert calls == []  # get_hebrew_date has service=None -- no external I/O, nothing to record

    monkeypatch.setattr(ai_tools, "health", health_check_module.health)


async def test_web_search_is_the_only_last_resort_tool():
    last_resort = [t.name for t in ai_tools.TOOLS if t.last_resort]
    assert last_resort == ["web_search"]


# ── 1. search_judaic_texts ───────────────────────────────────────────────────

async def test_search_judaic_texts_merges_curated_and_search_hits(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria, "find_refs_for_question", lambda q: ["Shulchan Arukh, Orach Chayim 263:1"])
    monkeypatch.setattr(ai_tools.sefaria_library, "search_library",
                         lambda q, n: [{"ref": "Mishnah Berurah 263:1", "title": "MB", "categories": ["Halakha"], "text": "..."}])

    result = await ai_tools.execute_tool("search_judaic_texts", {"query": "candle lighting"})
    refs = [r["ref"] for r in result["results"]]
    assert "Shulchan Arukh, Orach Chayim 263:1" in refs
    assert "Mishnah Berurah 263:1" in refs


async def test_search_judaic_texts_requires_query():
    result = await ai_tools.execute_tool("search_judaic_texts", {})
    assert result == {"error": "query is required"}


# ── 2. get_text_by_ref ───────────────────────────────────────────────────────

async def test_get_text_by_ref_requires_ref():
    result = await ai_tools.execute_tool("get_text_by_ref", {})
    assert result == {"error": "ref is required"}


async def test_get_text_by_ref_returns_engine_result(monkeypatch):
    monkeypatch.setattr(ai_tools.ShelahEngine, "get_library_text", lambda self, ref: {"ref": ref, "he": ["א"], "en": ["A"]})
    result = await ai_tools.execute_tool("get_text_by_ref", {"ref": "Genesis 1:1"})
    assert result["ref"] == "Genesis 1:1"


# ── 3. search_responsa_external ──────────────────────────────────────────────

async def test_search_responsa_external_requires_query():
    result = await ai_tools.execute_tool("search_responsa_external", {})
    assert result == {"error": "query is required"}


async def test_search_responsa_external_gathers_both_sources(monkeypatch):
    async def fake_halachipedia(q):
        return {"title": "Kashrut", "summary": "..."}

    async def fake_hebrewbooks(q):
        return {}  # falsy -> excluded

    monkeypatch.setattr(ai_tools, "async_search_halachipedia", fake_halachipedia)
    monkeypatch.setattr(ai_tools, "async_search_hebrewbooks", fake_hebrewbooks)

    result = await ai_tools.execute_tool("search_responsa_external", {"query": "kashrut"})
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Kashrut"


# ── 4. get_zmanim ────────────────────────────────────────────────────────────

async def test_get_zmanim_requires_location():
    result = await ai_tools.execute_tool("get_zmanim", {})
    assert "location required" in result["error"]


async def test_get_zmanim_rejects_out_of_range_lat():
    result = await ai_tools.execute_tool("get_zmanim", {"lat": 999, "lon": 0})
    assert "lat must be in" in result["error"]


async def test_get_zmanim_rejects_non_numeric_lat():
    result = await ai_tools.execute_tool("get_zmanim", {"lat": "not-a-number", "lon": 0})
    assert result == {"error": "lat/lon must be numeric"}


async def test_get_zmanim_uses_context_location_as_fallback(monkeypatch):
    captured = {}

    def fake_get_community_zmanim(lat, lon, timezone_str, community):
        captured.update(lat=lat, lon=lon, timezone_str=timezone_str, community=community)
        return {"metadata": {"zmanim_iso": {"Candle Lighting": "19:00"}}}

    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim", fake_get_community_zmanim)

    result = await ai_tools.execute_tool(
        "get_zmanim", {}, context={"lat": 40.0, "lon": -74.0, "timezone": "America/New_York"})
    assert captured["lat"] == 40.0
    assert result["zmanim"]["Candle Lighting"] == "19:00"


# ── 5. get_hebrew_date ───────────────────────────────────────────────────────

async def test_get_hebrew_date_gregorian_to_hebrew_direction():
    result = await ai_tools.execute_tool("get_hebrew_date", {"gregorian_date": "2026-08-21"})
    assert "hebrew_date" in result and result.get("error") is None


async def test_get_hebrew_date_hebrew_to_gregorian_direction():
    result = await ai_tools.execute_tool(
        "get_hebrew_date", {"hebrew_year": 5786, "hebrew_month": 5, "hebrew_day": 1})
    assert result.get("gregorian_date")


# ── 6. get_parasha ───────────────────────────────────────────────────────────

async def test_get_parasha_maps_no_parasha_note(monkeypatch):
    monkeypatch.setattr(ai_tools.calendar_service.calendar_engine, "get_parasha", lambda d: "No parasha for this date")
    result = await ai_tools.execute_tool("get_parasha", {"date": "2026-08-21"})
    assert result == {"parasha": None, "note": "No parasha for this date"}


async def test_get_parasha_returns_parasha_name(monkeypatch):
    monkeypatch.setattr(ai_tools.calendar_service.calendar_engine, "get_parasha", lambda d: "Parashat Ki Teitzei")
    result = await ai_tools.execute_tool("get_parasha", {"date": "2026-08-21"})
    assert result == {"parasha": "Parashat Ki Teitzei"}


# ── 7. get_omer ──────────────────────────────────────────────────────────────

async def test_get_omer_not_in_season(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "_get_omer_info", lambda d: None)
    result = await ai_tools.execute_tool("get_omer", {"date": "2026-08-21"})
    assert result == {"in_omer_season": False}


async def test_get_omer_in_season(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "_get_omer_info", lambda d: {"day": 12, "label": "Day 12"})
    result = await ai_tools.execute_tool("get_omer", {"date": "2026-04-30"})
    assert result == {"in_omer_season": True, "day": 12, "label": "Day 12"}


# ── 8. get_holidays ──────────────────────────────────────────────────────────

async def test_get_holidays_requires_both_dates():
    result = await ai_tools.execute_tool("get_holidays", {"start_date": "2026-09-01"})
    assert "required" in result["error"]


async def test_get_holidays_rejects_range_over_120_days():
    result = await ai_tools.execute_tool(
        "get_holidays", {"start_date": "2026-01-01", "end_date": "2026-12-31"})
    assert "date range" in result["error"]


async def test_get_holidays_filters_out_solar_events(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_monthly_events", lambda lat, lon, tz: [
        {"title": "🌅 Sunrise 6:12 AM", "start": "2026-09-12T06:12:00"},
        {"title": "🌇 Shkia 7:01 PM", "start": "2026-09-12T19:01:00"},
        {"title": "Rosh Hashana", "start": "2026-09-12"},
    ])
    result = await ai_tools.execute_tool(
        "get_holidays", {"start_date": "2026-09-01", "end_date": "2026-09-30"})
    assert [h["title"] for h in result["holidays"]] == ["Rosh Hashana"]


# ── 9. get_daily_study ───────────────────────────────────────────────────────

async def test_get_daily_study_returns_sefaria_result(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria, "get_daily_study", lambda: {"daf_yomi": "Berakhot 2a"})
    result = await ai_tools.execute_tool("get_daily_study", {})
    assert result == {"daf_yomi": "Berakhot 2a"}


# ── 10. web_search ───────────────────────────────────────────────────────────

async def test_web_search_requires_query():
    result = await ai_tools.execute_tool("web_search", {}, context={})
    assert result == {"error": "query is required"}


async def test_web_search_no_result_found(monkeypatch):
    async def fake_wiki(q):
        return None
    monkeypatch.setattr(ai_tools, "async_search_wikipedia", fake_wiki)
    result = await ai_tools.execute_tool("web_search", {"query": "obscure topic"})
    assert result == {"error": "no Wikipedia result found", "query": "obscure topic"}


async def test_web_search_returns_wikipedia_result(monkeypatch):
    async def fake_wiki(q):
        return {"title": "Shabbat", "summary": "The Jewish day of rest."}
    monkeypatch.setattr(ai_tools, "async_search_wikipedia", fake_wiki)
    result = await ai_tools.execute_tool("web_search", {"query": "Shabbat"})
    assert result["source"] == "wikipedia"
    assert result["title"] == "Shabbat"


# ── 11. lookup_word_meaning ──────────────────────────────────────────────────

async def test_lookup_word_meaning_requires_word():
    result = await ai_tools.execute_tool("lookup_word_meaning", {})
    assert result == {"error": "word is required"}


async def test_lookup_word_meaning_not_found(monkeypatch):
    monkeypatch.setattr(ai_tools, "_lookup_hebrew_word_meaning", lambda w: (None, None))
    result = await ai_tools.execute_tool("lookup_word_meaning", {"word": "asdfgh"})
    assert result == {"word": "asdfgh", "found": False}


async def test_lookup_word_meaning_found(monkeypatch):
    monkeypatch.setattr(ai_tools, "_lookup_hebrew_word_meaning", lambda w: ("Sabbath", "Jastrow"))
    result = await ai_tools.execute_tool("lookup_word_meaning", {"word": "שבת"})
    assert result == {"word": "שבת", "found": True, "meaning": "Sabbath", "source": "Jastrow"}


# ── 12. translate_text ───────────────────────────────────────────────────────

async def test_translate_text_requires_text():
    result = await ai_tools.execute_tool("translate_text", {})
    assert result == {"error": "text is required"}


async def test_translate_text_he_to_en(monkeypatch):
    monkeypatch.setattr(ai_tools, "_translate_hebrew_text_online", lambda t: ("Blessing", "google"))
    result = await ai_tools.execute_tool("translate_text", {"text": "ברכה", "direction": "he_to_en"})
    assert result == {"text": "ברכה", "translated": True, "result": "Blessing", "source": "google"}


# ── 13. get_commentaries ─────────────────────────────────────────────────────

async def test_get_commentaries_requires_ref():
    result = await ai_tools.execute_tool("get_commentaries", {})
    assert result == {"error": "ref is required"}


async def test_get_commentaries_caps_categories_and_entries(monkeypatch):
    links = {f"cat{i}": [f"entry{j}" for j in range(10)] for i in range(12)}
    monkeypatch.setattr(ai_tools.sefaria_library, "get_linked_texts", lambda ref: links)
    result = await ai_tools.execute_tool("get_commentaries", {"ref": "Genesis 1:1"})
    assert len(result["commentaries"]) == 8
    assert all(len(v) == 6 for v in result["commentaries"].values())


async def test_get_commentaries_no_links(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria_library, "get_linked_texts", lambda ref: {})
    result = await ai_tools.execute_tool("get_commentaries", {"ref": "Genesis 1:1"})
    assert result == {"ref": "Genesis 1:1", "commentaries": {}}


# ── 14. search_community_customs ─────────────────────────────────────────────

async def test_search_community_customs_requires_query():
    result = await ai_tools.execute_tool("search_community_customs", {})
    assert result == {"error": "query is required"}


async def test_search_community_customs_filters_by_canonical_community(monkeypatch):
    monkeypatch.setattr(ai_tools.customs, "search_customs", lambda q: [
        {"community": "Yemenite", "text": "..."},
        {"community": "Ashkenaz", "text": "..."},
    ])
    result = await ai_tools.execute_tool(
        "search_community_customs", {"query": "candle", "community": "Yemenite"})
    assert len(result["results"]) == 1
    assert result["results"][0]["community"] == "Yemenite"


# ── 15. get_community_profile ────────────────────────────────────────────────

async def test_get_community_profile_unknown_community():
    result = await ai_tools.execute_tool("get_community_profile", {"community": "Atlantis"})
    assert "unknown community" in result["error"]


async def test_get_community_profile_caps_nested_dict_fields(monkeypatch):
    fake_data = {
        "heritage_id": "yemenite",
        "identity": "Yemenite Jewish community",
        "core_halachic_authorities": {"primary_codes": ["Rambam"] * 20},
        "unique_minhagim": {"prayer": ["x"] * 20},
    }
    monkeypatch.setattr(ai_tools, "_read_json", lambda path: fake_data)
    result = await ai_tools.execute_tool("get_community_profile", {"community": "Yemenite"})
    assert result["community"] == "Yemenite"
    assert len(result["core_halachic_authorities"]["primary_codes"]) == 10


async def test_get_community_profile_read_error_degrades_gracefully(monkeypatch):
    def _boom(path):
        raise OSError("no such file")
    monkeypatch.setattr(ai_tools, "_read_json", _boom)
    result = await ai_tools.execute_tool("get_community_profile", {"community": "Yemenite"})
    assert "could not load profile" in result["error"]


# ── 16. browse_library ───────────────────────────────────────────────────────

async def test_browse_library_with_category_path(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria_library, "get_category_contents", lambda path: [{"title": "Berakhot"}])
    result = await ai_tools.execute_tool("browse_library", {"category_path": "Talmud/Bavli"})
    assert result["category_path"] == "Talmud/Bavli"
    assert result["contents"] == [{"title": "Berakhot"}]


async def test_browse_library_top_level_index(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria_library, "get_library_index",
                         lambda: [{"category": "Tanakh"}, {"title": "Mishnah"}])
    result = await ai_tools.execute_tool("browse_library", {})
    assert result["top_level_categories"] == [{"category": "Tanakh"}, {"category": "Mishnah"}]


# ── 17. search_library ───────────────────────────────────────────────────────

async def test_search_library_requires_query():
    result = await ai_tools.execute_tool("search_library", {})
    assert result == {"error": "query is required"}


async def test_search_library_passes_category_filters(monkeypatch):
    captured = {}

    def fake_search(query, max_results, filters=None):
        captured.update(query=query, max_results=max_results, filters=filters)
        return [{"ref": "Mishnah Berakhot 1:1"}]

    monkeypatch.setattr(ai_tools.sefaria_library, "search_library", fake_search)
    result = await ai_tools.execute_tool(
        "search_library", {"query": "blessing", "categories": ["Mishnah"]})
    assert captured["filters"] == ["Mishnah"]
    assert result["results"] == [{"ref": "Mishnah Berakhot 1:1"}]


# ── 18. get_prayer_text ──────────────────────────────────────────────────────

async def test_get_prayer_text_requires_name():
    result = await ai_tools.execute_tool("get_prayer_text", {})
    assert result == {"error": "prayer_name is required"}


async def test_get_prayer_text_not_found(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria_library, "get_index_leaf_refs", lambda name, n: [])
    result = await ai_tools.execute_tool("get_prayer_text", {"prayer_name": "Not A Real Prayer"})
    assert result == {"prayer_name": "Not A Real Prayer", "found": False}


async def test_get_prayer_text_found(monkeypatch):
    monkeypatch.setattr(ai_tools.sefaria_library, "get_index_leaf_refs", lambda name, n: ["Siddur, Shema 1"])
    monkeypatch.setattr(ai_tools.sefaria_library, "get_text",
                         lambda ref: {"ref": ref, "he": ["א"] * 10, "en": ["A"] * 10})
    result = await ai_tools.execute_tool("get_prayer_text", {"prayer_name": "Shema"})
    assert result["found"] is True
    assert len(result["sections"][0]["he"]) == 6


# ── 19. get_daily_zmanim_summary ─────────────────────────────────────────────

async def test_get_daily_zmanim_summary_requires_location():
    result = await ai_tools.execute_tool("get_daily_zmanim_summary", {})
    assert "location required" in result["error"]


async def test_get_daily_zmanim_summary_shapes_key_times(monkeypatch):
    monkeypatch.setattr(ai_tools.zmanim_engine, "get_community_zmanim", lambda lat, lon, tz, community: {
        "metadata": {
            "date": "2026-08-21",
            "hebrew_date": "8 Elul 5786",
            "parasha": "Ki Teitzei",
            "is_holiday": False,
            "omer_day": None,
            "zmanim_iso": {"Candle Lighting": "19:00", "Sunset": "19:20", "Unrelated": "x"},
        }
    })
    result = await ai_tools.execute_tool("get_daily_zmanim_summary", {"lat": 40.0, "lon": -74.0})
    assert result["key_times"] == {"Candle Lighting": "19:00", "Sunset": "19:20"}
    assert result["omer"] is None
    assert result["holiday"] is None


# ── 20. convert_measurements ─────────────────────────────────────────────────

async def test_convert_measurements_unknown_measure():
    result = await ai_tools.execute_tool("convert_measurements", {"measure": "cubit-of-nonsense"})
    assert "unknown measure" in result["error"]


async def test_convert_measurements_both_opinions_default():
    result = await ai_tools.execute_tool("convert_measurements", {"measure": "kezayit"})
    assert "chazon_ish" in result and "chaim_naeh" in result
    assert result["chazon_ish"]["metric_unit"] == "ml"


async def test_convert_measurements_single_opinion_and_quantity_scaling():
    result = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "revi_it", "opinion": "chazon_ish", "quantity": 2})
    assert "chaim_naeh" not in result
    assert result["chazon_ish"]["metric"] == 300.0  # 150ml * 2


async def test_convert_measurements_quantity_clamped_to_bounds():
    result = await ai_tools.execute_tool(
        "convert_measurements", {"measure": "tefach", "quantity": 999999})
    assert result["quantity"] == 1000.0


# ── 21. calculate_hebrew_date_math ───────────────────────────────────────────

async def test_calculate_hebrew_date_math_unknown_operation():
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {"operation": "levitate"})
    assert "unknown operation" in result["error"]


async def test_calculate_hebrew_date_math_add_days():
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "add_days", "hebrew_year": 5786, "hebrew_month": 1, "hebrew_day": 1, "days": 10,
    })
    assert result.get("gregorian_date")


async def test_calculate_hebrew_date_math_next_occurrence():
    result = await ai_tools.execute_tool("calculate_hebrew_date_math", {
        "operation": "next_occurrence", "hebrew_month": 7, "hebrew_day": 1,
        "after_gregorian_date": "2026-01-01",
    })
    assert result.get("gregorian_date")


# ── 22. format_source_citation ───────────────────────────────────────────────

async def test_format_source_citation_requires_ref():
    result = await ai_tools.execute_tool("format_source_citation", {})
    assert result == {"error": "ref is required"}


async def test_format_source_citation_normalizes_underscores():
    result = await ai_tools.execute_tool("format_source_citation", {"ref": "Orach_Chayim.242.1"})
    assert result["ref"] == "Orach_Chayim.242.1"
    assert "242:1" in result["citation"]
