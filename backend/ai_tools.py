"""
Agentic tool-use registry for the Sh'elah halachic AI (plan.md §9).

Exposes existing backend functions as Anthropic/Gemini tool-use JSON
schemas plus async executors, so the model can pull live Jewish-texts,
calendar/zmanim, and (last-resort only) web data instead of relying
solely on the pre-fetched RAG context. This module is pure exposure —
it wraps functions that already live in backend/*; the only genuinely
new code here is convert_measurements' deterministic shiurim table
(explicitly called for by plan.md §9.2b) and a small number of thin
handler-level compositions/fallbacks documented inline where the
plan's tool catalog named a function that turned out not to exist
verbatim (see docs/AI_TOOLS.md "Implementation notes").

Import discipline: backend.* only, never `app` -- this registry must
be callable from any transport (Flask sync, FastAPI async) and from
tests without booting the Flask app. Two tools in plan.md's tables
(get_community_profile, get_prayer_text) named app.py-only backing
functions (_build_trusted_custom_sources, SIDDUR_SECTION_MAP /
_get_prayer_refs); both are reimplemented here against backend-only
data sources instead (see each handler's docstring for the deviation
and its consequence).

Every handler has the uniform signature
    async def _h_xxx(arguments: dict, context: dict) -> dict
and never raises -- execute_tool() is the single place that applies
a timeout, a narrow exception catch, and health-circuit bookkeeping,
so a handler bug degrades to a tool-shaped error result, not a crash
of the whole agent loop (plan.md §9.5, fail-open).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date as date_lib
from typing import Any, Awaitable, Callable, Optional

from backend import calendar_service, customs, sefaria, sefaria_library, zmanim_engine
from backend.data_service import ShelahEngine
from backend.health_check import health
from backend.helpers import (
    COMMUNITIES,
    _canonicalize_community_name,
    _lookup_english_word_meaning,
    _lookup_hebrew_word_meaning,
    _translate_english_text_online,
    _translate_hebrew_text_online,
)
from backend.search import (
    async_search_halachipedia,
    async_search_hebrewbooks,
    async_search_wikipedia,
)
from backend.utils.text_engine import format_source_citation

logger = logging.getLogger(__name__)

# ── Shared enums / bounds ──────────────────────────────────────────────────

COMMUNITY_ENUM = ["standard"] + sorted(COMMUNITIES.keys())
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
MAX_QUERY_CHARS = 500
MAX_TEXT_CHARS = 2000


def _parse_date(value: Any) -> Optional[date_lib]:
    """Best-effort ISO 'YYYY-MM-DD' -> date, or None (never raises)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        y, m, d = (int(part) for part in text.split("-"))
        return date_lib(y, m, d)
    except (ValueError, TypeError):
        return None


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _truncate(text: Any, max_chars: int) -> str:
    s = str(text or "")
    return s if len(s) <= max_chars else s[:max_chars].rstrip() + "…"


# ── 1. search_judaic_texts ──────────────────────────────────────────────────

async def _h_search_judaic_texts(arguments: dict, context: dict) -> dict:
    query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "query is required"}
    max_results = _clamp_int(arguments.get("max_results"), 8, 1, 20)

    curated_refs, search_hits = await asyncio.gather(
        asyncio.to_thread(sefaria.find_refs_for_question, query),
        asyncio.to_thread(sefaria_library.search_library, query, max_results),
    )

    results = []
    seen_refs = set()
    for ref in curated_refs or []:
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        results.append({"ref": ref, "match_type": "curated_topic"})
    for hit in (search_hits or [])[:max_results]:
        ref = str((hit or {}).get("ref") or "")
        if not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        results.append({
            "ref": ref,
            "match_type": "search",
            "title": hit.get("title"),
            "categories": hit.get("categories"),
            "excerpt": _truncate(hit.get("text"), 300),
        })

    return {"query": query, "results": results[:max_results]}


# ── 2. get_text_by_ref ──────────────────────────────────────────────────────

async def _h_get_text_by_ref(arguments: dict, context: dict) -> dict:
    ref = str(arguments.get("ref") or "").strip()
    if not ref:
        return {"error": "ref is required"}
    engine = ShelahEngine()
    result = await asyncio.to_thread(engine.get_library_text, ref)
    return result if isinstance(result, dict) else {"error": "lookup failed"}


# ── 3. search_responsa_external ─────────────────────────────────────────────

async def _h_search_responsa_external(arguments: dict, context: dict) -> dict:
    query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "query is required"}
    max_results = _clamp_int(arguments.get("max_results"), 5, 1, 10)

    halachipedia, hebrewbooks = await asyncio.gather(
        async_search_halachipedia(query),
        async_search_hebrewbooks(query),
        return_exceptions=True,
    )
    results = []
    for hit in (halachipedia, hebrewbooks):
        if isinstance(hit, dict) and hit:
            results.append(hit)
    return {"query": query, "results": results[:max_results]}


# ── 4. get_zmanim ────────────────────────────────────────────────────────────

async def _h_get_zmanim(arguments: dict, context: dict) -> dict:
    lat = arguments.get("lat", context.get("lat"))
    lon = arguments.get("lon", context.get("lon"))
    if lat is None or lon is None:
        return {"error": "location required — lat/lon not provided and no stored user location is available"}
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"error": "lat/lon must be numeric"}
    if not (-90.0 <= lat_f <= 90.0) or not (-180.0 <= lon_f <= 180.0):
        return {"error": "lat must be in [-90, 90] and lon in [-180, 180]"}

    timezone_str = arguments.get("timezone") or context.get("timezone")
    community = str(arguments.get("community") or "standard")

    payload = await asyncio.to_thread(
        zmanim_engine.get_community_zmanim, lat_f, lon_f, timezone_str, community,
    )
    if not isinstance(payload, dict) or "error" in payload:
        return {"error": "zmanim computation failed for this location/date"}
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    return {
        "metadata": {k: v for k, v in metadata.items() if k != "zmanim_iso"},
        "zmanim": metadata.get("zmanim_iso") or payload.get("zmanim", {}),
    }


# ── 5. get_hebrew_date ──────────────────────────────────────────────────────

async def _h_get_hebrew_date(arguments: dict, context: dict) -> dict:
    hy, hm, hd = arguments.get("hebrew_year"), arguments.get("hebrew_month"), arguments.get("hebrew_day")
    if hy is not None and hm is not None and hd is not None:
        return await asyncio.to_thread(
            calendar_service.PyluachEngine.hebrew_to_gregorian, hy, hm, hd,
        )
    gregorian_date = arguments.get("gregorian_date")
    return await asyncio.to_thread(
        calendar_service.PyluachEngine.gregorian_to_hebrew, gregorian_date,
    )


# ── 6. get_parasha ──────────────────────────────────────────────────────────

async def _h_get_parasha(arguments: dict, context: dict) -> dict:
    target_date = arguments.get("date")
    parasha = await asyncio.to_thread(
        calendar_service.calendar_engine.get_parasha, target_date,
    )
    if parasha in ("No parasha for this date", "Parasha lookup unavailable"):
        return {"parasha": None, "note": parasha}
    return {"parasha": parasha}


# ── 7. get_omer ──────────────────────────────────────────────────────────────

async def _h_get_omer(arguments: dict, context: dict) -> dict:
    target_date = _parse_date(arguments.get("date")) or date_lib.today()
    info = await asyncio.to_thread(zmanim_engine._get_omer_info, target_date)
    if info is None:
        return {"in_omer_season": False}
    return {"in_omer_season": True, **info}


# ── 8. get_holidays ──────────────────────────────────────────────────────────

_HOLIDAY_TITLE_STRIP_RE = re.compile(r"^[^\w֐-׿]+")
# get_monthly_events() also emits daily solar (sunrise/sunset/nightfall)
# UI events tagged with exactly these three emoji prefixes
# (backend/zmanim_engine.py:401,409,417) -- only the Hebcal-sourced
# holiday/candle/havdalah items matter to this tool. Filtering on the
# emoji prefix (rather than English words like "sunset") is required
# because one of the three solar titles is Hebrew-transliterated
# ("Shkia", not "Sunset").
_SOLAR_EVENT_PREFIXES = ("🌅", "🌇", "🌃")


async def _h_get_holidays(arguments: dict, context: dict) -> dict:
    start = _parse_date(arguments.get("start_date"))
    end = _parse_date(arguments.get("end_date"))
    if not start or not end:
        return {"error": "start_date and end_date (YYYY-MM-DD) are both required"}
    if end < start or (end - start).days > 120:
        return {"error": "date range must be non-negative and at most 120 days"}

    lat = arguments.get("lat", context.get("lat")) or 31.7683
    lon = arguments.get("lon", context.get("lon")) or 35.2137
    try:
        events = await asyncio.to_thread(
            zmanim_engine.get_monthly_events, float(lat), float(lon), arguments.get("timezone"),
        )
    except (TypeError, ValueError):
        return {"error": "lat/lon must be numeric"}

    holidays = []
    for event in events or []:
        title = str(event.get("title") or "")
        if title.startswith(_SOLAR_EVENT_PREFIXES):
            continue
        start_str = str(event.get("start") or "")[:10]
        event_date = _parse_date(start_str)
        if not event_date or not (start <= event_date <= end):
            continue
        holidays.append({
            "title": _HOLIDAY_TITLE_STRIP_RE.sub("", title).strip(),
            "date": start_str,
        })
    return {"start_date": arguments.get("start_date"), "end_date": arguments.get("end_date"), "holidays": holidays}


# ── 9. get_daily_study ──────────────────────────────────────────────────────

async def _h_get_daily_study(arguments: dict, context: dict) -> dict:
    return await asyncio.to_thread(sefaria.get_daily_study)


# ── 10. web_search (last-resort only; see §9.3 gating in ask_pipeline.py) ──

async def _h_web_search(arguments: dict, context: dict) -> dict:
    query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "query is required"}
    result = await async_search_wikipedia(query)
    if not result:
        return {"error": "no Wikipedia result found", "query": query}
    return {"query": query, "source": "wikipedia", **result}


# ── 11. lookup_word_meaning ─────────────────────────────────────────────────

async def _h_lookup_word_meaning(arguments: dict, context: dict) -> dict:
    word = str(arguments.get("word") or "").strip()
    if not word:
        return {"error": "word is required"}
    language = str(arguments.get("language") or "hebrew").lower()
    fn = _lookup_hebrew_word_meaning if language == "hebrew" else _lookup_english_word_meaning
    meaning, source = await asyncio.to_thread(fn, word)
    if not meaning:
        return {"word": word, "found": False}
    return {"word": word, "found": True, "meaning": meaning, "source": source}


# ── 12. translate_text ──────────────────────────────────────────────────────

async def _h_translate_text(arguments: dict, context: dict) -> dict:
    text = str(arguments.get("text") or "").strip()[:MAX_TEXT_CHARS]
    if not text:
        return {"error": "text is required"}
    direction = str(arguments.get("direction") or "he_to_en")
    fn = _translate_hebrew_text_online if direction == "he_to_en" else _translate_english_text_online
    translated, source = await asyncio.to_thread(fn, text)
    if not translated:
        return {"text": text, "translated": False}
    return {"text": text, "translated": True, "result": translated, "source": source}


# ── 13. get_commentaries ────────────────────────────────────────────────────

async def _h_get_commentaries(arguments: dict, context: dict) -> dict:
    ref = str(arguments.get("ref") or "").strip()
    if not ref:
        return {"error": "ref is required"}
    links = await asyncio.to_thread(sefaria_library.get_linked_texts, ref)
    if not links:
        return {"ref": ref, "commentaries": {}}
    # Cap each category so one heavily-annotated verse can't blow the
    # tool-result token budget.
    capped = {category: entries[:6] for category, entries in list(links.items())[:8]}
    return {"ref": ref, "commentaries": capped}


# ── 14. search_community_customs ────────────────────────────────────────────

async def _h_search_community_customs(arguments: dict, context: dict) -> dict:
    """Backs onto customs.search_customs. plan.md §9.2 also names
    backend.rag._retrieve_community_knowledge as a source for this
    tool, but that function does `import app as _app` internally
    (backend/rag.py:215, documented there as a deliberate circular-
    import dodge) -- calling it would force-load app.py from a module
    whose entire contract is "backend.* only, never app" (see this
    file's module docstring). Deliberately not wrapped here; flagged
    as a follow-up in docs/AI_TOOLS.md rather than silently skipped.
    """
    query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "query is required"}
    results = await asyncio.to_thread(customs.search_customs, query)
    community_filter = arguments.get("community")
    if community_filter:
        canonical = _canonicalize_community_name(str(community_filter))
        if canonical:
            results = [r for r in results if r.get("community") == canonical]
    return {"query": query, "results": (results or [])[:12]}


# ── 15. get_community_profile ───────────────────────────────────────────────

async def _h_get_community_profile(arguments: dict, context: dict) -> dict:
    """Backs onto the same customs/<slug>.json files
    backend/routes_community.py reads, but reimplemented against
    backend-only data instead of that route's actual handler --
    the route imports `_build_trusted_custom_sources` from app.py
    (app.py:245), which this module's "backend.* only, never app"
    contract forbids. The trusted-sources synthesis is therefore not
    reproduced; this returns the community's own identity/history/
    core-authorities/minhagim fields directly instead, which covers
    plan.md §9.2b's stated purpose ("background on a community's
    halachic tradition and history for lens answers").
    """
    canonical = _canonicalize_community_name(str(arguments.get("community") or ""))
    if not canonical:
        return {"error": f"unknown community: {arguments.get('community')!r}"}

    slug = COMMUNITIES[canonical]
    path = os.path.join(customs.CUSTOMS_DIR, f"{slug}.json")
    try:
        data = await asyncio.to_thread(_read_json, path)
    except (OSError, ValueError) as exc:
        return {"error": f"could not load profile for {canonical}: {exc}"}

    return {
        "community": canonical,
        "heritage_id": data.get("heritage_id"),
        "identity": data.get("identity"),
        "languages": data.get("languages"),
        "historical_background": _truncate(data.get("historical_background"), 1200),
        # These two fields are dicts-of-lists in the community JSON schema
        # (e.g. {"primary_codes": [...], "later_yemenite_poskim": [...]}),
        # not flat lists -- cap each inner list rather than the outer dict.
        "core_halachic_authorities": _cap_nested(data.get("core_halachic_authorities")),
        "unique_minhagim": _cap_nested(data.get("unique_minhagim")),
    }


def _cap_nested(value: Any, max_items: int = 10) -> Any:
    if isinstance(value, list):
        return value[:max_items]
    if isinstance(value, dict):
        return {k: _cap_nested(v, max_items) for k, v in list(value.items())[:max_items]}
    return value


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── 16. browse_library ──────────────────────────────────────────────────────

async def _h_browse_library(arguments: dict, context: dict) -> dict:
    category_path = arguments.get("category_path")
    if category_path:
        contents = await asyncio.to_thread(sefaria_library.get_category_contents, str(category_path))
        return {"category_path": category_path, "contents": contents}
    index = await asyncio.to_thread(sefaria_library.get_library_index)
    # Top-level index can be large; return only category names/titles
    # one level deep so the model can decide where to drill in next.
    top_level = []
    for node in (index or [])[:30]:
        if isinstance(node, dict):
            top_level.append({"category": node.get("category") or node.get("title")})
    return {"top_level_categories": top_level}


# ── 17. search_library ──────────────────────────────────────────────────────

async def _h_search_library(arguments: dict, context: dict) -> dict:
    query = str(arguments.get("query") or "").strip()[:MAX_QUERY_CHARS]
    if not query:
        return {"error": "query is required"}
    max_results = _clamp_int(arguments.get("max_results"), 10, 1, 20)
    categories = arguments.get("categories")
    filters = [str(c) for c in categories] if isinstance(categories, list) else None

    hits = await asyncio.to_thread(sefaria_library.search_library, query, max_results, filters)
    return {"query": query, "results": (hits or [])[:max_results]}


# ── 18. get_prayer_text ─────────────────────────────────────────────────────

async def _h_get_prayer_text(arguments: dict, context: dict) -> dict:
    """Backs onto sefaria_library.get_index_leaf_refs +
    sefaria_library.get_text, not app.py's SIDDUR_SECTION_MAP /
    _get_prayer_refs (app.py-only, forbidden by this module's import
    contract). This means curated friendly-name -> ref mappings for
    common prayer names are not available here; resolution falls back
    to Sefaria's own index-title search, which is less precise for
    prayer names that don't match a Sefaria index title closely. See
    docs/AI_TOOLS.md for the tracked follow-up.
    """
    prayer_name = str(arguments.get("prayer_name") or "").strip()
    if not prayer_name:
        return {"error": "prayer_name is required"}
    max_sections = _clamp_int(arguments.get("max_sections"), 6, 1, 20)

    refs = await asyncio.to_thread(sefaria_library.get_index_leaf_refs, prayer_name, max_sections)
    if not refs:
        return {"prayer_name": prayer_name, "found": False}

    texts = await asyncio.gather(
        *(asyncio.to_thread(sefaria_library.get_text, ref) for ref in refs[:max_sections])
    )
    sections = [
        {"ref": t.get("ref"), "he": t.get("he", [])[:6], "en": t.get("en", [])[:6]}
        for t in texts if isinstance(t, dict) and not t.get("error")
    ]
    return {"prayer_name": prayer_name, "found": bool(sections), "sections": sections}


# ── 19. get_daily_zmanim_summary ────────────────────────────────────────────

async def _h_get_daily_zmanim_summary(arguments: dict, context: dict) -> dict:
    lat = arguments.get("lat", context.get("lat"))
    lon = arguments.get("lon", context.get("lon"))
    if lat is None or lon is None:
        return {"error": "location required — lat/lon not provided and no stored user location is available"}
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return {"error": "lat/lon must be numeric"}

    timezone_str = arguments.get("timezone") or context.get("timezone")
    community = str(arguments.get("community") or "standard")
    payload = await asyncio.to_thread(
        zmanim_engine.get_community_zmanim, lat_f, lon_f, timezone_str, community,
    )
    if not isinstance(payload, dict) or "error" in payload:
        return {"error": "zmanim computation failed for this location/date"}

    metadata = payload.get("metadata", {})
    zmanim_iso = metadata.get("zmanim_iso") or {}
    key_times = {
        k: zmanim_iso.get(k)
        for k in ("Candle Lighting", "Latest Shema (GRA)", "Sunset", "Havdalah")
        if zmanim_iso.get(k)
    }
    return {
        "date": metadata.get("date"),
        "hebrew_date": metadata.get("hebrew_date"),
        "parasha": metadata.get("parasha") or metadata.get("weekly_shabbat_parasha"),
        "holiday": metadata.get("holiday") if metadata.get("is_holiday") else None,
        "omer": {"day": metadata.get("omer_day"), "label": metadata.get("omer_label")} if metadata.get("omer_day") else None,
        "key_times": key_times,
        "shabbat_warning": metadata.get("shabbat_warning") or None,
    }


# ── 20. convert_measurements ────────────────────────────────────────────────
#
# New deterministic table (plan.md §9.2b explicitly calls for this --
# no prior implementation exists to wrap). Base figures are the
# commonly-published approximations for Chazon Ish and Rav Chaim
# Naeh's shiurim; kav and mil are derived by multiplication from
# revi'it/amah so the table stays internally consistent rather than
# citing separately-rounded external figures for every unit. This is
# educational reference data, not a p'sak — practical application
# should go through a rav, matching this app's overall stance.

SHIURIM_TABLE = {
    "amah": {"unit": "cm", "chazon_ish": 57.6, "chaim_naeh": 48.0,
             "description": "Cubit — lengths in construction/ritual-object halacha (e.g. sukkah walls, mezuzah placement)."},
    "tefach": {"unit": "cm", "chazon_ish": 9.6, "chaim_naeh": 8.0,
               "description": "Handbreadth — 1/6 of an amah."},
    "kezayit": {"unit": "ml", "chazon_ish": 50.0, "chaim_naeh": 27.0,
                "description": "Olive-bulk — the standard 'eating' shiur (matzah, bread). Volume approximate; mass varies by food density."},
    "revi_it": {"unit": "ml", "chazon_ish": 150.0, "chaim_naeh": 86.0,
                "description": "Quarter-log — the standard 'drinking'/netilat yadayim shiur (kiddush, havdalah, handwashing)."},
    "kav": {"unit": "ml", "chazon_ish": 150.0 * 16, "chaim_naeh": 86.0 * 16,
            "description": "4 logs (= 16 revi'iyot) — a larger dry/liquid volume measure.", "derived_from": "revi_it × 16"},
    "mil": {"unit": "m", "chazon_ish": 57.6 / 100 * 2000, "chaim_naeh": 48.0 / 100 * 2000,
            "description": "A distance of 2000 amot — travel-distance and time-of-day halachot.", "derived_from": "amah × 2000"},
}

_CM_TO_IN = 1 / 2.54
_ML_TO_FLOZ = 1 / 29.5735
_M_TO_FT = 1 / 0.3048


async def _h_convert_measurements(arguments: dict, context: dict) -> dict:
    measure = str(arguments.get("measure") or "").strip().lower()
    row = SHIURIM_TABLE.get(measure)
    if not row:
        return {"error": f"unknown measure {measure!r}; choose one of {sorted(SHIURIM_TABLE)}"}

    opinion = str(arguments.get("opinion") or "both").lower()
    try:
        quantity = float(arguments.get("quantity", 1) or 1)
    except (TypeError, ValueError):
        quantity = 1.0
    quantity = max(0.0, min(quantity, 1000.0))

    unit = row["unit"]

    def _values(base_value: float) -> dict:
        scaled = base_value * quantity
        if unit == "cm":
            return {"metric": round(scaled, 2), "metric_unit": "cm", "imperial": round(scaled * _CM_TO_IN, 2), "imperial_unit": "in"}
        if unit == "ml":
            return {"metric": round(scaled, 2), "metric_unit": "ml", "imperial": round(scaled * _ML_TO_FLOZ, 2), "imperial_unit": "fl oz"}
        return {"metric": round(scaled, 2), "metric_unit": "m", "imperial": round(scaled * _M_TO_FT, 2), "imperial_unit": "ft"}

    out = {"measure": measure, "quantity": quantity, "description": row["description"]}
    if row.get("derived_from"):
        out["derived_from"] = row["derived_from"]
    if opinion in ("chazon_ish", "both"):
        out["chazon_ish"] = _values(row["chazon_ish"])
    if opinion in ("chaim_naeh", "both"):
        out["chaim_naeh"] = _values(row["chaim_naeh"])
    return out


# ── 21. calculate_hebrew_date_math ──────────────────────────────────────────

async def _h_calculate_hebrew_date_math(arguments: dict, context: dict) -> dict:
    operation = str(arguments.get("operation") or "").strip()
    engine = calendar_service.PyluachEngine

    if operation == "gregorian_to_hebrew":
        return await asyncio.to_thread(engine.gregorian_to_hebrew, arguments.get("gregorian_date"))

    if operation == "hebrew_to_gregorian":
        return await asyncio.to_thread(
            engine.hebrew_to_gregorian,
            arguments.get("hebrew_year"), arguments.get("hebrew_month"), arguments.get("hebrew_day"),
        )

    if operation == "add_days":
        return await asyncio.to_thread(
            engine.add_days_to_hebrew_date,
            arguments.get("hebrew_year"), arguments.get("hebrew_month"),
            arguments.get("hebrew_day"), arguments.get("days", 0),
        )

    if operation == "next_occurrence":
        return await asyncio.to_thread(
            engine.next_occurrence_of_hebrew_date,
            arguments.get("hebrew_month"), arguments.get("hebrew_day"),
            arguments.get("after_gregorian_date"),
        )

    return {"error": f"unknown operation {operation!r}; choose gregorian_to_hebrew, hebrew_to_gregorian, add_days, or next_occurrence"}


# ── 22. format_source_citation ──────────────────────────────────────────────

async def _h_format_source_citation(arguments: dict, context: dict) -> dict:
    ref = str(arguments.get("ref") or "").strip()
    if not ref:
        return {"error": "ref is required"}
    citation = format_source_citation(ref, arguments.get("title"))
    return {"ref": ref, "citation": citation}


# ── Registry ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[[dict, dict], Awaitable[dict]]
    service: Optional[str] = None          # health_check service name to gate on; None = no external I/O
    timeout_seconds: float = 10.0
    last_resort: bool = False              # True only for web_search (§9.3 orchestrator gate)


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="search_judaic_texts",
        description=(
            "PRIMARY tool for halachic/text questions. Find Sefaria primary sources and "
            "commentaries matching a topic or keyword. Use this FIRST before any other tool "
            "and before web_search."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Topic or keyword to search for, e.g. 'lighting Shabbat candles'.", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            },
            "required": ["query"],
        },
        handler=_h_search_judaic_texts,
        service="sefaria",
    ),
    ToolSpec(
        name="get_text_by_ref",
        description="Fetch the full Hebrew and English text of a specific Sefaria reference the model already cited (e.g. 'Shulchan Arukh, Orach Chayim 242:1').",
        input_schema={
            "type": "object",
            "properties": {"ref": {"type": "string", "description": "A Sefaria ref string.", "minLength": 1}},
            "required": ["ref"],
        },
        handler=_h_get_text_by_ref,
        service="sefaria",
    ),
    ToolSpec(
        name="search_responsa_external",
        description="Search whitelisted external halachic sources (Halachipedia, HebrewBooks) — tier 2, use only after search_judaic_texts has been tried.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
        },
        handler=_h_search_responsa_external,
        service="web",
    ),
    ToolSpec(
        name="get_zmanim",
        description="Get full halachic times (zmanim) for a date/location/community — candle-lighting, sof zman shema, plag, tzeit, etc. Deterministic, computed — never estimate these yourself.",
        input_schema={
            "type": "object",
            "properties": {
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
                "timezone": {"type": "string", "description": "IANA timezone name, e.g. 'America/New_York'. Optional — inferred from lat/lon if omitted."},
                "community": {"type": "string", "enum": COMMUNITY_ENUM, "default": "standard"},
            },
            "required": ["lat", "lon"],
        },
        handler=_h_get_zmanim,
        service="hebcal",
    ),
    ToolSpec(
        name="get_hebrew_date",
        description="Convert Gregorian↔Hebrew date. Pass gregorian_date for Gregorian→Hebrew, or hebrew_year/hebrew_month/hebrew_day for Hebrew→Gregorian.",
        input_schema={
            "type": "object",
            "properties": {
                "gregorian_date": {"type": "string", "pattern": DATE_PATTERN},
                "hebrew_year": {"type": "integer", "minimum": 3760, "maximum": 6000},
                "hebrew_month": {"type": "integer", "minimum": 1, "maximum": 13},
                "hebrew_day": {"type": "integer", "minimum": 1, "maximum": 30},
            },
        },
        handler=_h_get_hebrew_date,
        service=None,
    ),
    ToolSpec(
        name="get_parasha",
        description="Get the weekly Torah portion (parasha) in effect for a given Gregorian date (defaults to today).",
        input_schema={
            "type": "object",
            "properties": {"date": {"type": "string", "pattern": DATE_PATTERN}},
        },
        handler=_h_get_parasha,
        service="hebcal",
    ),
    ToolSpec(
        name="get_omer",
        description="Get the Sefirat HaOmer day/week count for a date (defaults to today). Returns in_omer_season=false outside the counting period.",
        input_schema={
            "type": "object",
            "properties": {"date": {"type": "string", "pattern": DATE_PATTERN}},
        },
        handler=_h_get_omer,
        service=None,
    ),
    ToolSpec(
        name="get_holidays",
        description="List Jewish holidays/candle-lighting/havdalah events in a date range (max 120 days).",
        input_schema={
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "pattern": DATE_PATTERN},
                "end_date": {"type": "string", "pattern": DATE_PATTERN},
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
                "timezone": {"type": "string"},
            },
            "required": ["start_date", "end_date"],
        },
        handler=_h_get_holidays,
        service="hebcal",
    ),
    ToolSpec(
        name="get_daily_study",
        description="Get today's daily-learning cycles — Daf Yomi, Mishnah Yomit, Rambam.",
        input_schema={"type": "object", "properties": {}},
        handler=_h_get_daily_study,
        service="sefaria",
    ),
    ToolSpec(
        name="web_search",
        description=(
            "LAST RESORT ONLY. Search Wikipedia for general-knowledge facts NOT found in Judaic "
            "texts or calendar tools (e.g. real-world context, definitions). Never a substitute for "
            "texts/poskim, and never the sole basis for a halachic ruling. Only call this after "
            "search_judaic_texts (and, for location/date questions, a calendar/zmanim tool) has "
            "already been tried and was insufficient."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS}},
            "required": ["query"],
        },
        handler=_h_web_search,
        service="web",
        last_resort=True,
    ),
    ToolSpec(
        name="lookup_word_meaning",
        description="Look up the precise lexicon meaning of a specific Hebrew or English term (Sefaria lexicon/BDB/Jastrow for Hebrew).",
        input_schema={
            "type": "object",
            "properties": {
                "word": {"type": "string", "minLength": 1},
                "language": {"type": "string", "enum": ["hebrew", "english"], "default": "hebrew"},
            },
            "required": ["word"],
        },
        handler=_h_lookup_word_meaning,
        service="sefaria",
    ),
    ToolSpec(
        name="translate_text",
        description="On-demand Hebrew↔English translation of a phrase or passage that lacks a translation.",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1, "maxLength": MAX_TEXT_CHARS},
                "direction": {"type": "string", "enum": ["he_to_en", "en_to_he"], "default": "he_to_en"},
            },
            "required": ["text"],
        },
        handler=_h_translate_text,
        service="translate_google",
    ),
    ToolSpec(
        name="get_commentaries",
        description="Get linked commentaries and cross-references for a ref (Rashi, Tosafot, Mishnah Berurah, etc.) to deepen or contrast a ruling.",
        input_schema={
            "type": "object",
            "properties": {"ref": {"type": "string", "minLength": 1}},
            "required": ["ref"],
        },
        handler=_h_get_commentaries,
        service="sefaria",
    ),
    ToolSpec(
        name="search_community_customs",
        description="Retrieve minhag (custom) for a specific community from the local customs corpus.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "community": {"type": "string", "enum": sorted(COMMUNITIES.keys())},
            },
            "required": ["query"],
        },
        handler=_h_search_community_customs,
        service=None,
    ),
    ToolSpec(
        name="get_community_profile",
        description="Background on a community's halachic tradition, history, and core authorities — for community-lens answers.",
        input_schema={
            "type": "object",
            "properties": {"community": {"type": "string", "enum": sorted(COMMUNITIES.keys())}},
            "required": ["community"],
        },
        handler=_h_get_community_profile,
        service=None,
    ),
    ToolSpec(
        name="browse_library",
        description="Navigate the Sefaria category tree — find what texts exist in a topic area before fetching. Omit category_path for the top-level index.",
        input_schema={
            "type": "object",
            "properties": {"category_path": {"type": "string", "description": "e.g. 'Tanakh' or 'Tanakh/Torah'."}},
        },
        handler=_h_browse_library,
        service="sefaria",
    ),
    ToolSpec(
        name="search_library",
        description="Full-text Sefaria search with optional category filters, beyond curated ref mapping.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                "categories": {"type": "array", "items": {"type": "string"}, "description": "Category filters, e.g. ['Halakhah']."},
            },
            "required": ["query"],
        },
        handler=_h_search_library,
        service="sefaria",
    ),
    ToolSpec(
        name="get_prayer_text",
        description="Retrieve liturgy text for a specific prayer by name.",
        input_schema={
            "type": "object",
            "properties": {
                "prayer_name": {"type": "string", "minLength": 1},
                "max_sections": {"type": "integer", "minimum": 1, "maximum": 20, "default": 6},
            },
            "required": ["prayer_name"],
        },
        handler=_h_get_prayer_text,
        service="sefaria",
    ),
    ToolSpec(
        name="get_daily_zmanim_summary",
        description="One-shot 'today at this location' digest: candle-lighting, key sof-zman times, parasha, omer, holiday flags. Use this instead of get_zmanim for a general 'what do I need to know today' question.",
        input_schema={
            "type": "object",
            "properties": {
                "lat": {"type": "number", "minimum": -90, "maximum": 90},
                "lon": {"type": "number", "minimum": -180, "maximum": 180},
                "timezone": {"type": "string"},
                "community": {"type": "string", "enum": COMMUNITY_ENUM, "default": "standard"},
            },
            "required": ["lat", "lon"],
        },
        handler=_h_get_daily_zmanim_summary,
        service="hebcal",
    ),
    ToolSpec(
        name="convert_measurements",
        description="Convert a halachic measure (kezayit, revi_it, amah, tefach, mil, kav) to metric/imperial under the Chazon Ish and/or Rav Chaim Naeh shitot. Deterministic reference table, not a p'sak.",
        input_schema={
            "type": "object",
            "properties": {
                "measure": {"type": "string", "enum": sorted(SHIURIM_TABLE.keys())},
                "opinion": {"type": "string", "enum": ["chazon_ish", "chaim_naeh", "both"], "default": "both"},
                "quantity": {"type": "number", "minimum": 0, "maximum": 1000, "default": 1},
            },
            "required": ["measure"],
        },
        handler=_h_convert_measurements,
        service=None,
    ),
    ToolSpec(
        name="calculate_hebrew_date_math",
        description="Hebrew-calendar date arithmetic: convert, add days, or find the next occurrence of a Hebrew month/day (e.g. an upcoming yahrzeit or Hebrew birthday).",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["gregorian_to_hebrew", "hebrew_to_gregorian", "add_days", "next_occurrence"]},
                "gregorian_date": {"type": "string", "pattern": DATE_PATTERN},
                "hebrew_year": {"type": "integer", "minimum": 3760, "maximum": 6000},
                "hebrew_month": {"type": "integer", "minimum": 1, "maximum": 13},
                "hebrew_day": {"type": "integer", "minimum": 1, "maximum": 30},
                "days": {"type": "integer", "minimum": -36500, "maximum": 36500},
                "after_gregorian_date": {"type": "string", "pattern": DATE_PATTERN},
            },
            "required": ["operation"],
        },
        handler=_h_calculate_hebrew_date_math,
        service=None,
    ),
    ToolSpec(
        name="format_source_citation",
        description="Normalize a ref into the house citation format so a cited source renders consistently.",
        input_schema={
            "type": "object",
            "properties": {
                "ref": {"type": "string", "minLength": 1},
                "title": {"type": "string"},
            },
            "required": ["ref"],
        },
        handler=_h_format_source_citation,
        service=None,
    ),
]

_TOOLS_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}


def get_tool_schemas(*, include_web_search: bool = False) -> list[dict]:
    """Anthropic/Gemini tool-use schema list. web_search is excluded by
    default — the orchestrator (backend/ask_pipeline.py) only passes
    include_web_search=True once the last-resort gate (§9.3) has
    actually been satisfied for the current turn.
    """
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in TOOLS
        if include_web_search or not t.last_resort
    ]


async def execute_tool(name: str, arguments: dict, *, context: Optional[dict] = None) -> dict:
    """Run one tool call: timeout + narrow exception catch + health
    record_success/record_failure, fail-open (plan.md §9.5). Never
    raises — a failure degrades to {"error": ...}, so a single dead
    tool never crashes the agent loop.
    """
    spec = _TOOLS_BY_NAME.get(name)
    if spec is None:
        return {"error": f"unknown tool: {name}"}

    context = context if isinstance(context, dict) else {}
    if spec.service and not await asyncio.to_thread(health.is_healthy, spec.service):
        return {"error": f"{name} is temporarily unavailable (circuit open for {spec.service})"}

    try:
        result = await asyncio.wait_for(
            spec.handler(arguments or {}, context), timeout=spec.timeout_seconds,
        )
    except asyncio.TimeoutError:
        if spec.service:
            health.record_failure(spec.service)
        logger.warning("ai_tools: %s timed out after %.1fs", name, spec.timeout_seconds)
        return {"error": f"{name} timed out"}
    except Exception as exc:  # noqa: BLE001 - a tool bug must never crash the agent loop
        if spec.service:
            health.record_failure(spec.service)
        logger.warning("ai_tools: %s raised %s: %s", name, type(exc).__name__, exc)
        return {"error": f"{name} failed: {type(exc).__name__}"}

    if spec.service:
        health.record_success(spec.service)
    return result if isinstance(result, dict) else {"error": f"{name} returned a non-dict result"}
