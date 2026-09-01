"""
Main Flask application for Sh'elah.

What this file owns:
- App bootstrapping, environment wiring, and cache/session policy.
- Public web routes (/, manifest, service worker) and all JSON API routes.
- Integration glue for Supabase preferences, Clerk auth checks, and Sefaria-backed text/prayer/community APIs.
- Calendar and zmanim delivery used by the dashboard (including Hebcal-backed holiday/parasha endpoints).

How to navigate this file:
1) Configuration and helper utilities near the top.
2) Auth and Supabase client helpers.
3) Route handlers grouped by feature (health/devtools, preferences, library/text, prayers, communities, calendar/zmanim).
"""

import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, has_request_context, render_template, request, jsonify, session, g, send_from_directory, Response
from dotenv import load_dotenv
import time
import os
from datetime import date as greg_date, timedelta
from urllib.parse import unquote
from pathlib import Path

try:
    from supabase import create_client
    try:
        from supabase.lib.client_options import SyncClientOptions
    except Exception:
        SyncClientOptions = None
except Exception:
    create_client = None
    SyncClientOptions = None

from pyluach import dates as pyluach_dates

from backend.data_service import ShelahEngine
from backend import sefaria
from backend import claude
from backend import ask_pipeline
from backend.logging_setup import (
    setup_logging,
    _capture_backend_error,
    bind_request_id,
    get_logger,
    submit_with_context,
)
from backend.customs import validate_all_customs_at_startup
from backend.health_check import health as api_health  # noqa: F401 -- re-export shim, consumed by routes_devtools.py's `from app import api_health`
from backend.helpers import extract_ai_cited
from backend.helpers import (
    _sanitize_answer_mode,
    _resolve_client_ip,
    _compact_ai_sources,
    _coarse_ai_error_reason,
    SECURITY_RESPONSE_HEADERS,
)
from backend.helpers import (
    _bounded_cache_set,
    _canonicalize_community_name,
)
# WEB_LAST_RESORT_WARNING and _extract_query_keywords below are not called
# directly in this file, but are consumed as `app.<name>` attributes by
# backend/rag.py's lazy `import app as _app` (see rag.py's module docstring) —
# do not remove even though static unused-import checks flag them.
from backend.utils.text_engine import WEB_LAST_RESORT_WARNING  # noqa: F401
from backend.utils.search_provider import _extract_query_keywords, get_halakhic_sources  # noqa: F401
from backend.cache_policy import classify_cache_tier, CACHE_TIER_PRIVATE

# Module-level bounded executor — avoids creating/destroying a pool per request.
# max_workers capped so Vercel serverless invocations don't spawn unbounded threads.
# Sits below the import block rather than inside it (moved 2026-08-20, plan.md
# §26.3): as the first non-import statement at module level it made ruff flag
# every subsequent import in this file as E402. Nothing imported here depends
# on the pool existing, so the position carried no meaning.
_THREAD_POOL = ThreadPoolExecutor(max_workers=8)


# Maps each prayer name to its constituent Sefaria "Siddur Sefard" refs for full text
SIDDUR_SECTION_MAP = {
    "Upon Arising": [
        "Siddur Sefard, Upon Arising, Modeh Ani",
        "Siddur Sefard, Upon Arising, Introductory Prayers",
        "Siddur Sefard, Upon Arising, Upon Entering Synagogue",
    ],
    "Weekday Shacharit": [
        "Siddur Sefard, Weekday Shacharit, Morning Blessings",
        "Siddur Sefard, Weekday Shacharit, Blessings on Torah",
        "Siddur Sefard, Weekday Shacharit, Morning Prayer",
        "Siddur Sefard, Weekday Shacharit, The Shema",
        "Siddur Sefard, Weekday Shacharit, Amidah",
        "Siddur Sefard, Weekday Shacharit, Tachanun",
        "Siddur Sefard, Weekday Shacharit, Aleinu",
    ],
    "Weekday Mincha": [
        "Siddur Sefard, Weekday Mincha, Amidah",
        "Siddur Sefard, Weekday Mincha, Tachanun",
    ],
    "Weekday Maariv": [
        "Siddur Sefard, Weekday Maariv, The Shema",
        "Siddur Sefard, Weekday Maariv, Amidah",
    ],
    "Shabbat Shacharit": [
        "Siddur Sefard, Shabbat Morning Services, Pesukei D'Zimrah",
        "Siddur Sefard, Shabbat Morning Services, Amidah",
        "Siddur Sefard, Shabbat Morning Services, Shabbat Torah Reading",
    ],
    "Shabbat Mincha": [
        "Siddur Sefard, Shabbat Mincha, Amidah",
    ],
    "Kiddush": [
        "Siddur Sefard, Shabbat Evening Meal, Shabbat Eve Kiddush",
        "Siddur Sefard, Shabbat Day Meal, Shabbat Day Kiddush",
    ],
    "Havdalah": [
        "Siddur Sefard, Motzaei Shabbat , Havdala",
    ],
    "Bedtime Shema": [
        "Siddur Sefard, Bedtime Shema",
    ],
    "Kiddush Levanah": [
        "Siddur Sefard, Kiddush Levanah",
    ],
    "Holiday Prayers": [
        "Siddur Sefard, Holidays, Yom Tov Eve Kiddush",
        "Siddur Sefard, Holidays, Yizkor",
        "Siddur Sefard, Rosh Chodesh, Hallel",
    ],
}

DEVTOOLS_STATS = {
    "answers_total": 0,
    "fallback_answers": 0,
    "strict_blocks": 0,
    "segment_reports": 0,
}

# _bounded_cache_set / _CACHE_MAX_SIZE: reconciled to backend/helpers.py as
# part of the Phase 4 Finding A cleanup above -- re-imported as a back-compat
# shim (search: "Re-import shims"). Still used below against this file's own
# ASK_RESPONSE_CACHE (a plain function taking `cache` as a parameter, so which
# module defines it is irrelevant to callers).

ASK_RESPONSE_CACHE: dict = {}
ASK_RESPONSE_CACHE_TTL_SECONDS = 90

# QUICK_TEXT_ALIASES, TRANSLATION_CACHE, TRANSLATION_SOURCE_CACHE, and
# HEBREW_INTERPRETIVE_GLOSSARY: reconciled to backend/helpers.py as part of
# the Phase 4 Finding A cleanup above. Unused elsewhere in this file (only the
# now-deleted local translation/lookup functions read them) -- no shim import
# needed here; backend/routes_library.py already imports QUICK_TEXT_ALIASES
# directly from backend.helpers.


def _get_cached_ask_payload(cache_key):
    entry = ASK_RESPONSE_CACHE.get(cache_key)
    if not entry:
        return None
    ts = float(entry.get("ts") or 0.0)
    if (time.time() - ts) > ASK_RESPONSE_CACHE_TTL_SECONDS:
        ASK_RESPONSE_CACHE.pop(cache_key, None)
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return None
    try:
        # Return a detached copy so callers can mutate response payload safely.
        return json.loads(json.dumps(payload))
    except Exception:
        return None


def _set_cached_ask_payload(cache_key, payload):
    if not isinstance(payload, dict):
        return
    try:
        _bounded_cache_set(ASK_RESPONSE_CACHE, cache_key, {
            "ts": time.time(),
            "payload": json.loads(json.dumps(payload)),
        })
    except Exception:
        return


# Backend refactor cleanup (plan.md section 2): _join_with_and and
# _build_source_attribution_note were live duplicates between app.py and
# backend/helpers.py. Canonical implementations now live in
# backend/helpers.py (also fixes _join_with_and to defensively filter
# blank/falsy items before joining, matching this file's former behavior) --
# re-imported here as back-compat shims (search: "Re-import shims").


# _compose_answer_with_prefixes moved to backend/rag.py; re-imported via the
# RAG shim further below (search: "Re-import shims").


# Phase 2 backend refactor (plan.md): retrieval, lemmatization, and
# corpus-matching layer (Hebrew keyword extraction, discovery-query
# building, Sefaria/external global source collection, local-JSON custom
# matching, get_halakhic_sources) moved to backend/utils/search_provider.py.
# Re-imported here as back-compat shims (search: "Re-import shims").


# _build_ask_tool_context moved to backend/rag.py; re-imported via the
# RAG shim further below (search: "Re-import shims").


# Backend refactor cleanup (plan.md section 2): _compact_ai_sources was a
# live, DIVERGED duplicate -- this file's copy used an HTML-tag regex that
# left bare "<>" fragments unstripped, while backend/helpers.py's
# test-anchored copy (already used by asgi.py's FastAPI path) fixed that.
# Canonical implementation now lives in backend/helpers.py -- re-imported
# here as a back-compat shim.


# _strip_hebrew_diacritics, _contains_hebrew_letters, _normalize_lookup_word,
# _translate_hebrew_text_google, _translate_hebrew_text_mymemory,
# _lookup_sefaria_lexicon, _translate_hebrew_text_online,
# _translate_english_text_online, _fill_missing_english_lines: reconciled to
# backend/helpers.py as part of the Phase 4 Finding A cleanup above. Their
# back-compat shim imports were removed 2026-08-01 -- nothing outside app.py
# consumed them via `from app import`. Their local-only constants
# (_SEFARIA_LEXICON_BASE, _PREFERRED_LEXICONS, _HTML_TAG_RE,
# TRANSLATION_CACHE, TRANSLATION_SOURCE_CACHE) were unused elsewhere in this
# file and removed with them.

# _decode_route_ref: reconciled to backend/helpers.py as part of the cleanup
# above. Unused elsewhere in this file -- no shim import needed here;
# blueprints already import it directly from backend.helpers.

# Phase 2 backend refactor (plan.md): _translate_text_google /
# _translate_text_mymemory (previously diverged, duplicated in both app.py
# and backend/helpers.py -- plan.md section 2) and their pure helpers
# _is_translation_echo / _extract_google_translated_text moved to
# backend/utils/search_provider.py as the single canonical implementation.
# Re-imported here as back-compat shims (search: "Re-import shims").


def _build_trusted_custom_sources(data):
    """Build a stable source list from trusted halachic authorities in community files."""
    if not isinstance(data, dict):
        return []

    candidates = []

    source_registry = data.get("source_registry", {}) if isinstance(
        data.get("source_registry"), dict) else {}
    candidates.extend(source_registry.get("primary", []) if isinstance(
        source_registry.get("primary"), list) else [])

    authorities = data.get("core_halachic_authorities", {}) if isinstance(
        data.get("core_halachic_authorities"), dict) else {}
    for key in (
        "primary_codes",
        "major_rishonim_base",
        "later_ashkenazi_poskim",
        "later_sephardi_poskim",
        "later_moroccan_poskim",
        "later_turkish_poskim",
    ):
        value = authorities.get(key)
        if isinstance(value, list):
            candidates.extend(value)

    deduped = []
    seen = set()
    for item in candidates:
        label = str(item or "").strip()
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(label)

    return deduped[:6]


# _lookup_english_word_meaning, _normalize_glossary_meaning,
# _looks_like_transliteration, _hebrew_word_variant_candidates,
# _parse_meaning_candidates, _collect_word_meaning_alternatives,
# _lookup_hebrew_word_meaning: reconciled to backend/helpers.py as part of the
# Phase 4 Finding A cleanup above. None of these had any internal caller left
# in app.py (backend/routes_library.py already called the backend.helpers
# canonical copies directly), so their back-compat shim imports were removed
# outright 2026-08-01 rather than kept as re-exports.


# _sanitize_answer_mode (and its ANSWER_MODES constant, deleted above):
# reconciled to backend/helpers.py as part of the cleanup above -- re-imported
# here as a back-compat shim. asgi.py already imports helpers' copy directly.


DETAIL_REQUEST_RE = re.compile(
    r"(\bexplain\b|\bfull\s+explanation\b|\bin\s+depth\b|\bdetailed\b|\bdetail\b|"
    r"\belaborate\b|\bexpand\b|\bbreak\s+down\b|\bwalk\s+me\s+through\b|\bwhy\b|\bhow\b|"
    r"הסבר|למה|כיצד|בפירוט|הרחב|נמק|פרט)",
    re.IGNORECASE,
)


def _is_detail_requested(question, mode):
    mode_value = str(mode or "").strip().lower()
    if mode_value in {"sources", "strict"}:
        return True
    return bool(DETAIL_REQUEST_RE.search(str(question or "")))


def _summarize_ruling_text(ruling_text, max_sentences=3, max_chars=380):
    clean = re.sub(r"\s+", " ", str(ruling_text or "").strip())
    if not clean:
        return ""

    sentence_candidates = [
        segment.strip()
        for segment in re.split(r"(?<=[.!?])\s+", clean)
        if segment.strip()
    ]
    summary = " ".join(sentence_candidates[:max_sentences]).strip(
    ) if sentence_candidates else clean
    if len(summary) > max_chars:
        summary = f"{summary[:max_chars].rstrip()}..."
    return summary


def _extract_numbered_ruling_steps(clean, max_steps):
    """Strategy 1: extract "(1) ..." style numbered clauses. Returns [] if
    none found. Split out of _extract_action_steps_from_ruling() to keep
    this loop out of that function's own complexity count (SonarCloud
    python:S3776).
    """
    numbered_clauses = [
        match.strip(" ;.")
        for match in re.findall(r"\(\d+\)\s*([^;]+)", clean)
        if match and match.strip()
    ]
    normalized_steps = []
    for clause in numbered_clauses[:max_steps]:
        clause_clean = clause[0].upper() + clause[1:] if clause else clause
        if clause_clean and clause_clean not in normalized_steps:
            normalized_steps.append(clause_clean)
    return normalized_steps


def _extract_keyword_ruling_steps(fragments, max_steps):
    """Strategy 2: pick sentence fragments containing action-guidance
    keywords (consult/verify/follow/etc). Split out of
    _extract_action_steps_from_ruling() (SonarCloud python:S3776) -- see
    _extract_numbered_ruling_steps.
    """
    steps = []
    for fragment in fragments:
        lowered = fragment.lower()
        if any(token in lowered for token in (
            "consult",
            "verify",
            "follow",
            "avoid",
            "wait",
            "check",
            "ask",
            "review",
            "custom",
            "practice",
            "מנהג",
            "בדוק",
            "התייעץ",
        )):
            step = fragment[:180].strip()
            if step and step not in steps:
                steps.append(step)
        if len(steps) >= max_steps:
            break
    return steps


def _extract_action_steps_from_ruling(ruling_text, max_steps=5):
    clean = re.sub(r"\s+", " ", str(ruling_text or "").strip())
    if not clean:
        return []

    numbered_steps = _extract_numbered_ruling_steps(clean, max_steps)
    if numbered_steps:
        return numbered_steps

    fragments = [
        fragment.strip(" ;")
        for fragment in re.split(r"(?<=[.!?;:])\s+", clean)
        if fragment and fragment.strip()
    ]

    keyword_steps = _extract_keyword_ruling_steps(fragments, max_steps)
    if keyword_steps:
        return keyword_steps

    fallback = []
    for fragment in fragments[:max_steps]:
        snippet = fragment[:180].strip()
        if snippet:
            fallback.append(snippet)
    return fallback


def _decode_jsonish_text(value):
    text = str(value or "")
    if not text:
        return ""

    decoded = text.replace("\\n", "\n").replace(
        "\\t", " ").replace("\\\"", '"')
    decoded = re.sub(r"\s+", " ", decoded).strip()
    return decoded


def _extract_jsonish_string_field(text, field_name):
    raw = str(text or "")
    if not raw:
        return ""

    escaped_field = re.escape(str(field_name or "").strip())
    if not escaped_field:
        return ""

    candidates = [raw, raw.replace('\\\"', '"')]
    for candidate in candidates:
        match = re.search(
            rf'"{escaped_field}"\s*:\s*"((?:\\\\.|[^"\\\\])*)"',
            candidate,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue
        value = _decode_jsonish_text(match.group(1))
        if value:
            return value

    return ""


def _parse_jsonish_array_body(body, max_items):
    """Extract up to `max_items` deduped, decoded string items from a
    "[...]" array body matched by _extract_jsonish_string_array_field().
    Split out to keep this loop out of that function's own complexity
    count (SonarCloud python:S3776).
    """
    extracted = []
    for item in re.findall(r'"((?:\\.|[^"\\])*)"', body, flags=re.DOTALL):
        cleaned = _decode_jsonish_text(item)
        if cleaned and cleaned not in extracted:
            extracted.append(cleaned)
        if len(extracted) >= max_items:
            break
    return extracted


def _extract_jsonish_string_array_field(text, field_name, max_items=6):
    raw = str(text or "")
    if not raw:
        return []

    escaped_field = re.escape(str(field_name or "").strip())
    if not escaped_field:
        return []

    candidates = [raw, raw.replace('\\\"', '"')]
    for candidate in candidates:
        match = re.search(
            rf'"{escaped_field}"\s*:\s*\[(.*?)\]',
            candidate,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            continue

        extracted = _parse_jsonish_array_body(match.group(1), max_items)
        if extracted:
            return extracted

    return []


def _looks_like_leaked_structured_payload(text):
    normalized = str(text or "").lower()
    if not normalized:
        return False

    leak_markers = (
        "```json",
        '"ruling"',
        '"summary"',
        "## summary",
        "## practical steps",
    )
    return any(marker in normalized for marker in leak_markers)


def _strip_structured_noise(text):
    cleaned = str(text or "").strip()
    if not cleaned:
        return ""

    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("```", "")
    cleaned = re.sub(r"^#+\s*(ruling|summary|practical steps?)\s*$", "", cleaned,
                     flags=re.IGNORECASE | re.MULTILINE)
    cleaned = re.sub(r"\*\*(Prohibited|Permitted)\*\*",
                     "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _recover_leaked_structured_fields(clean_structured, raw_answer):
    """If the model leaked its raw JSON structure into free text, recover
    ruling/summary/practical_steps from it. Mutates `clean_structured` in
    place. Split out of _coerce_ai_answer_shape() to keep this branch out
    of that function's own complexity count (SonarCloud python:S3776).
    """
    existing_ruling = str(clean_structured.get("ruling") or "").strip()
    if not (_looks_like_leaked_structured_payload(existing_ruling)
            or _looks_like_leaked_structured_payload(raw_answer)):
        return

    extracted_ruling = (
        _extract_jsonish_string_field(raw_answer, "ruling")
        or _extract_jsonish_string_field(existing_ruling, "ruling")
    )
    extracted_summary = (
        _extract_jsonish_string_field(raw_answer, "summary")
        or _extract_jsonish_string_field(existing_ruling, "summary")
    )
    extracted_steps = (
        _extract_jsonish_string_array_field(raw_answer, "practical_steps")
        or _extract_jsonish_string_array_field(existing_ruling, "practical_steps")
    )

    if extracted_ruling:
        clean_structured["ruling"] = extracted_ruling
    if extracted_summary:
        clean_structured["summary"] = extracted_summary
    if extracted_steps:
        clean_structured["practical_steps"] = extracted_steps


def _reevaluate_prohibition_flag(clean_structured, ruling_text):
    """Re-check whether `ruling_text` actually asserts a direct
    prohibition, downgrading a spurious is_prohibited=True flag when the
    text only mentions "forbidden work"/melacha in a permissive context.
    Mutates `clean_structured` in place. Split out of
    _coerce_ai_answer_shape() (SonarCloud python:S3776) -- see
    _recover_leaked_structured_fields.
    """
    if not (clean_structured.get("is_prohibited") and ruling_text):
        return

    lowered_ruling = ruling_text.lower()
    direct_prohibition = bool(re.search(
        r"(\b(?:not\s+permitted|may\s+not|must\s+not|assur|asur)\b|"
        r"\b(?:is|are|remains|considered|deemed)\s+(?:strictly\s+)?(?:forbidden|prohibited)\b|"
        r"אסור)",
        lowered_ruling,
        flags=re.IGNORECASE,
    ))
    contextual_forbidden_mentions = bool(re.search(
        r"(\bforbidden\s+work\b|\bmelacha\b|\bavoid\s+melacha\b)",
        lowered_ruling,
        flags=re.IGNORECASE,
    ))
    permission_signals = bool(re.search(
        r"(\bpermitted\b|\ballowed\b|\bmitzvah\b|\bobligation\b|\brecommended\b|מותר)",
        lowered_ruling,
        flags=re.IGNORECASE,
    ))

    if not direct_prohibition or (contextual_forbidden_mentions and permission_signals):
        clean_structured["is_prohibited"] = False


def _resolve_clean_structured_answer(result, raw_answer):
    """Return the structured-answer dict to enrich, or None if the model
    output has no usable structured content (result is left untouched)."""
    structured = result.get("structured")
    clean_structured = dict(structured) if isinstance(structured, dict) else None

    if clean_structured:
        _recover_leaked_structured_fields(clean_structured, raw_answer)

    if (not clean_structured) or not str(clean_structured.get("ruling") or "").strip():
        parsed = claude.parse_structured_model_output(raw_answer)
        if isinstance(parsed, dict) and str(parsed.get("ruling") or "").strip():
            clean_structured = parsed

    return clean_structured


def _enrich_structured_answer(clean_structured, question, mode):
    """Normalize ruling/summary text and backfill summary/practical_steps
    when the question calls for detail. Mutates and returns clean_structured."""
    detail_needed = _is_detail_requested(question, mode)
    ruling_text = _strip_structured_noise(clean_structured.get("ruling") or "")
    clean_structured["ruling"] = ruling_text

    summary_text = _strip_structured_noise(
        clean_structured.get("summary") or "")
    if summary_text:
        clean_structured["summary"] = summary_text

    practical_steps = clean_structured.get("practical_steps")
    if not isinstance(practical_steps, list):
        practical_steps = []

    _reevaluate_prohibition_flag(clean_structured, ruling_text)

    if detail_needed and not summary_text:
        clean_structured["summary"] = _summarize_ruling_text(
            ruling_text,
            max_sentences=4,
            max_chars=520,
        )

    if detail_needed and len(practical_steps) < 2:
        clean_structured["practical_steps"] = _extract_action_steps_from_ruling(
            ruling_text)

    return clean_structured


def _coerce_ai_answer_shape(result, question, mode, answer_language="en"):
    """Stabilize model output shape so UI always gets readable sections."""
    if not isinstance(result, dict):
        return result

    result_error = str(result.get("error") or "")
    if result_error.startswith(("security_blocked_input", "security_blocked_domain", "security_blocked_safety")):
        return result

    raw_answer = str(result.get("answer") or "").strip()
    clean_structured = _resolve_clean_structured_answer(result, raw_answer)
    if not clean_structured:
        return result

    clean_structured = _enrich_structured_answer(clean_structured, question, mode)

    rendered_answer = claude.render_structured_markdown(
        clean_structured,
        answer_language=answer_language,
    )
    if rendered_answer:
        result["structured"] = clean_structured
        result["answer"] = rendered_answer

    return result


# _canonicalize_community_name: reconciled to backend/helpers.py as part of
# the Phase 4 Finding A cleanup above -- re-imported as a back-compat shim
# (search: "Re-import shims"). backend/helpers.py's copy uses its own
# COMMUNITIES/COMMUNITY_ALIASES dicts (byte-identical content to this file's
# copies below); this file's copies stay because _detect_community_in_text
# (not named for migration, reached into lazily by backend/rag.py and
# monkeypatched directly in tests/test_rag.py) still needs them locally.


def _detect_community_in_text(question):
    q_lower = (question or "").lower()
    for alias, canonical in sorted(COMMUNITY_ALIASES.items(), key=lambda x: len(x[0]), reverse=True):
        if alias in q_lower:
            return canonical

    for canonical in COMMUNITIES.keys():
        if canonical.lower() in q_lower:
            return canonical

    return None


def _build_pyluach_holiday_events(year):
    """Fallback holiday event list for FullCalendar when Hebcal is unavailable."""
    events = []
    try:
        current = greg_date(int(year), 1, 1)
        end = greg_date(int(year), 12, 31)
    except Exception:
        return events

    while current <= end:
        try:
            heb = pyluach_dates.GregorianDate(
                current.year, current.month, current.day).to_heb()
            holiday_name = heb.holiday()
            if holiday_name:
                emoji = _holiday_emoji_for_event(holiday_name, "major")
                events.append({
                    "title": f"{emoji} {holiday_name}",
                    "start": current.isoformat(),
                    "allDay": True,
                    "display": "block",
                    "category": "major",
                    "color": "#802f3e",
                    "textColor": "#ffffff",
                })
        except Exception:
            # Keep fallback generation resilient even if one date fails.
            pass
        current += timedelta(days=1)

    return events


def _strip_leading_symbol_prefix(text):
    raw = str(text or "").strip()
    if not raw:
        return ""
    return re.sub(r"^[^\w\u0590-\u05FF]+", "", raw).strip()


# Each rule: (title substrings, category values, emoji) -- the first rule
# whose title-substring matches `lowered` OR whose category value matches
# `cat` wins. The trailing "✡️" return is the fallback for everything else
# (which also covers what were previously separate cat in {"modern"} /
# {"major","minor","holiday","special"} checks -- those returned the exact
# same "✡️" the fallback already does, so folding them in is a no-op,
# confirmed by an exhaustive before/after comparison). Table-driven instead
# of a long if/elif chain (SonarCloud python:S3776).
_HOLIDAY_EMOJI_RULES = (
    (("yom ha'atzmaut", "yom haatzmaut"), (), "🇮🇱"),
    (("hanukkah", "chanukah"), (), "🕎"),
    (("erev rosh hashana", "rosh hashana"), (), "🍎🍯"),
    (("lag ba'omer", "lag baomer"), (), "🔥"),
    (("yom yerushalayim",), (), "🇮🇱"),
    (("erev shavuot",), (), "⛰️"),
    (("shavuot",), (), "🌸"),
    (("sukkot", "succot", "sukkos", "succos"), (), "🍋🌿"),
    (("rosh chodesh",), ("roshchodesh",), "🌙"),
    (("taanis", "taanit", "fast", "tzom", "tisha b'av", "17 of tamuz", "gedaliah", "esther"), ("fast",), "✡️"),
    (("shabbat",), ("shabbat", "parashat"), "🕍"),
)


def _holiday_emoji_for_event(title, category=""):
    lowered = str(title or "").strip().lower()
    cat = str(category or "").strip().lower()

    for title_tokens, cat_values, emoji in _HOLIDAY_EMOJI_RULES:
        if any(token in lowered for token in title_tokens) or cat in cat_values:
            return emoji

    return "✡️"


def _holiday_color_for_category(category):
    palette = {
        "major": "#802f3e",
        "minor": "#594176",
        "modern": "#2563eb",
        "fast": "#374151",
        "roshchodesh": "#5a99b7",
        "shabbat": "#004e5f",
        "parashat": "#004e5f",
        "holiday": "#802f3e",
        "special": "#6b7280",
    }
    return palette.get(str(category or "").strip().lower(), "#6b7280")


load_dotenv()
app = Flask(__name__)

# Configure structured JSON logging as early as possible so all log records
# (including import-time warnings from sub-modules) use the JSON formatter.
setup_logging()
_request_logger = get_logger("shelah.request.flask")

is_production_runtime = (
    os.environ.get("VERCEL") == "1"
    or os.environ.get("FLASK_ENV", "").strip().lower() == "production"
)

# Corpus validation is a developer/CI safety net, not a production request-path
# need: it bills Active CPU on every cold start (Vercel §14.4.1). Skip it in
# production imports by default; VALIDATE_CUSTOMS_AT_STARTUP explicitly
# overrides in either direction (e.g. force it on for a production canary).
_validate_customs_env = os.environ.get(
    "VALIDATE_CUSTOMS_AT_STARTUP", "").strip().lower()
if _validate_customs_env:
    _should_validate_customs_at_startup = _validate_customs_env not in (
        "0", "false", "no", "off")
else:
    _should_validate_customs_at_startup = not is_production_runtime

if _should_validate_customs_at_startup:
    validate_all_customs_at_startup()

_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if not _flask_secret:
    import logging as _logging
    _logging.getLogger(__name__).critical(
        "FLASK_SECRET_KEY is not set — using a random ephemeral key. "
        "All sessions will be invalidated on every process restart. "
        "Set FLASK_SECRET_KEY in your environment for a stable key."
    )
    _flask_secret = os.urandom(32)
app.secret_key = _flask_secret

# Rate limiting (plan.md §16.3-L2 / §16.8.2): Flask-Limiter used to be
# installed here, independently of asgi.py's own /ask-only limiter -- two
# stores, two key functions, two 429 shapes that had nothing keeping them in
# sync (plan.md §16.8.1). Both are gone. The single enforcement point is now
# backend.rate_limit.RateLimitMiddleware, registered once on
# asgi.fastapi_app, covering this Flask app's routes too via the
# WSGIMiddleware mount in asgi.py. See backend/rate_limit.py for the policy
# table, store, and RATE_LIMIT_REDIS_URL / RATELIMIT_ENABLED env vars.
#
# NOTE: this means `python3 app.py` (bare Flask, no ASGI layer -- local-dev
# convenience only, never the production entrypoint; see plan.md §16.1 D1)
# now has no rate limiting of its own. Deliberate: a second limiter "just
# for dev mode" is exactly the duplication this change removes elsewhere.


CLERK_PUBLISHABLE_KEY = (
    os.environ.get("CLERK_PUBLISHABLE_KEY")
    or os.environ.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
    or ""
).strip()
# plan.md §8.A.1/§8.D.2: single source of truth for the legal-document
# versions the click-through consent gate (templates/index.html) and
# accept_legal() (backend/routes_user.py) both key off of. Bump either
# constant whenever that document changes materially — the version travels
# into the consent modal's localStorage key and the accepted-consent record,
# so a bump alone re-prompts every user on next load, satisfying "re-prompt
# on material version change" without any server round-trip.
LEGAL_TERMS_VERSION = "2.0"
LEGAL_PRIVACY_VERSION = "2.0"
# Sentry Browser DSN — public by design (that's what a browser DSN is,
# plan.md §17.1), but still env-only and a true no-op when unset: the
# template only emits the CDN <script> tags when this is truthy (see the
# index() route below and templates/index.html), mirroring the discipline
# already applied to the server-side SENTRY_DSN in backend/logging_setup.py.
SENTRY_DSN_BROWSER = (os.environ.get("SENTRY_DSN_BROWSER") or "").strip()
# Must match the Python-side Sentry init exactly (backend/logging_setup.py)
# so a browser error can be correlated back to the deploy that caused it.
SENTRY_ENVIRONMENT = os.environ.get("VERCEL_ENV", "development")
SENTRY_RELEASE = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "").strip()
CLERK_JWT_ISSUER = (os.environ.get("CLERK_JWT_ISSUER")
                    or "").strip().rstrip("/")
CLERK_AUDIENCE = (os.environ.get("CLERK_AUDIENCE") or "").strip()
# Phase 4 backend refactor (plan.md §4): _in_prod_runtime / CLERK_ENFORCE_AUTH
# were a live, byte-identical duplicate of backend/auth.py's copy (every
# blueprint and asgi.py already imported the backend.auth version directly;
# only app.py still carried its own). Canonical implementation now lives in
# backend/auth.py -- compatibility shim, re-imported below (search:
# "Re-import shims"). Remove this comment when app.py's local auth surface
# is fully retired to backend/auth.py (cleanup ticket).
SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
SUPABASE_PUBLISHABLE_KEY = (os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY") or "").strip()
SUPABASE_SECRET_KEY = (os.environ.get("SUPABASE_SECRET_KEY") or "").strip()
SUPABASE_PREFS_TABLE = (os.environ.get(
    "SUPABASE_PREFS_TABLE") or "user_preferences").strip()
SUPABASE_COMMUNITY_KNOWLEDGE_TABLE = (os.environ.get(
    "SUPABASE_COMMUNITY_KNOWLEDGE_TABLE") or "community_knowledge").strip()
SUPABASE_USER_MEMORIES_TABLE = (os.environ.get(
    "SUPABASE_USER_MEMORIES_TABLE") or "user_memories").strip()
SUPABASE_STUDY_BOOKMARKS_TABLE = (os.environ.get(
    "SUPABASE_STUDY_BOOKMARKS_TABLE") or "study_bookmarks").strip()
SUPABASE_ASK_HISTORY_TABLE = (os.environ.get(
    "SUPABASE_ASK_HISTORY_TABLE") or "ask_history").strip()
SUPABASE_ANSWER_FEEDBACK_TABLE = (os.environ.get(
    "SUPABASE_ANSWER_FEEDBACK_TABLE") or "answer_feedback").strip()
# Security posture, not per-deployment config -- every environment should
# enforce RLS the same way, so this is a literal rather than an env var.
STRICT_SUPABASE_RLS = True
_supabase_client = None


# _env_int: reconciled to backend/rag.py as part of the Phase 4 Finding A
# cleanup above -- re-imported as a back-compat shim (search: "Re-import
# shims"). Still used directly below and at this file's own call sites.
from backend.rag import _env_int  # noqa: E402 -- deliberate: kept beside the config block it serves, not hoisted


# Retrieval-tuning constants, not per-deployment config -- changing how many
# rows feed the AI prompt is a prompt-quality decision that belongs in code
# review, not something an operator flips via env var.
RAG_TOP_KNOWLEDGE_ROWS = 5
RAG_MEMORY_ROWS = 2


from backend.auth import (  # noqa: E402 -- deliberate: grouped with the Supabase/auth helpers below, not hoisted
    _verify_clerk_token,
    _extract_bearer_token,
    CLERK_ENFORCE_AUTH,
    maybe_require_clerk_auth,
)


def _get_supabase_client():
    global _supabase_client
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
        return None
    if _supabase_client is None:
        # Backend retrieval uses the secret key to avoid RLS limits on publishable/auth keys.
        _supabase_client = create_client(
            SUPABASE_URL, SUPABASE_SECRET_KEY)
    return _supabase_client


def _looks_like_jwt(value):
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(parts)


def _extract_supabase_token_from_list(parsed_list):
    """Extract a Supabase access token from a parsed JSON list-shaped
    cookie value. Split out of _extract_supabase_token_from_cookie_value()
    to keep this loop out of that function's own complexity count
    (SonarCloud python:S3776).
    """
    for item in parsed_list:
        if isinstance(item, str) and _looks_like_jwt(item):
            return item
        if isinstance(item, dict):
            token = item.get("access_token") or item.get("accessToken")
            if isinstance(token, str) and token:
                return token
    return None


def _extract_supabase_token_from_cookie_value(raw_value):
    if not raw_value:
        return None

    decoded = unquote(raw_value)
    if _looks_like_jwt(decoded):
        return decoded

    try:
        parsed = json.loads(decoded)
    except Exception:
        return None

    if isinstance(parsed, dict):
        token = parsed.get("access_token") or parsed.get("accessToken")
        return token if isinstance(token, str) and token else None

    if isinstance(parsed, list):
        return _extract_supabase_token_from_list(parsed)

    return None


def _categorize_supabase_auth_cookies():
    """Split request cookies into (session_cookie_values, chunked_cookies)
    for _extract_supabase_access_token(). chunked_cookies maps base cookie
    name -> [(chunk_index, value), ...] for cookies split across multiple
    "name.0", "name.1", ... parts. Split out to keep this loop out of that
    function's own complexity count (SonarCloud python:S3776).
    """
    session_cookie_values = []
    chunked_cookies = {}
    for cookie_name, cookie_value in request.cookies.items():
        if not (cookie_name.startswith("sb-") and "-auth-token" in cookie_name):
            continue

        if "." in cookie_name:
            base, suffix = cookie_name.rsplit(".", 1)
            if suffix.isdigit():
                chunked_cookies.setdefault(base, []).append(
                    (int(suffix), cookie_value))
                continue

        session_cookie_values.append(cookie_value)
    return session_cookie_values, chunked_cookies


def _extract_supabase_access_token(bearer_token=None):
    # Prefer Authorization header so API clients can override cookie auth.
    bearer = _extract_bearer_token(bearer_token)
    if bearer:
        return bearer

    if not has_request_context():
        # No Flask request context (e.g. asgi.py's native FastAPI /ask route,
        # which calls this chain with an explicit bearer_token and no Flask
        # request ever pushed) -- the cookie fallback below reads Flask's
        # global `request` proxy and would raise RuntimeError here. The
        # explicit bearer_token was already checked above; nothing left to
        # try. plan.md §35.1.
        return None

    direct_cookie_names = [
        "sb-access-token",
        "supabase-access-token",
    ]
    for cookie_name in direct_cookie_names:
        direct_value = request.cookies.get(cookie_name)
        token = _extract_supabase_token_from_cookie_value(direct_value)
        if token:
            return token

    session_cookie_values, chunked_cookies = _categorize_supabase_auth_cookies()

    for cookie_value in session_cookie_values:
        token = _extract_supabase_token_from_cookie_value(cookie_value)
        if token:
            return token

    for chunk_parts in chunked_cookies.values():
        sorted_parts = sorted(chunk_parts, key=lambda part: part[0])
        joined_value = "".join(part[1] for part in sorted_parts)
        token = _extract_supabase_token_from_cookie_value(joined_value)
        if token:
            return token

    return None


def _get_request_supabase_client(bearer_token=None):
    """Flask equivalent of Next.js createServerClient for request-scoped reads."""
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        return None

    access_token = _extract_supabase_access_token(bearer_token)
    if not access_token or SyncClientOptions is None:
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        options = SyncClientOptions(headers=auth_headers)
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, options=options)
    except TypeError:
        # Compatibility fallback for older supabase-py signatures.
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def _get_user_scoped_supabase_client(bearer_token=None):
    """Return request-scoped Supabase client for RLS-protected user tables."""
    client = _get_request_supabase_client(bearer_token)
    if not client:
        return None

    if STRICT_SUPABASE_RLS and not _extract_supabase_access_token(bearer_token):
        return None

    return client


def _get_request_user_id():
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = str(claims.get("sub") or "").strip()
    if user_id:
        return user_id

    token = _extract_bearer_token()
    if not token:
        return None

    try:
        decoded = _verify_clerk_token(token)
    except Exception:
        return None

    user_id = str(decoded.get("sub") or "").strip()
    return user_id or None


def _normalize_rag_text(value, max_chars=360):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if max_chars > 0 and len(text) > max_chars:
        text = f"{text[:max_chars].rstrip()}..."
    return text


# Re-import shims for backward compatibility with asgi.py and blueprints.
# Functions moved to backend/ modules are re-imported here so any existing
# call-sites inside app.py or legacy consumers keep working unchanged.
from backend.rag import (  # noqa: E402 -- deliberate: back-compat re-export shim, must stay at the bottom
    _build_ask_tool_context,
    _retrieve_community_knowledge,
    _compose_answer_with_prefixes,
    _store_ask_history,
    _knowledge_rows_to_customs,
    _fetch_user_memory_summaries,
    _store_user_memory_summary,
)
# _knowledge_rows_to_customs, _fetch_user_memory_summaries,
# _store_user_memory_summary: reconciled to backend/rag.py as part of the
# Phase 4 Finding A cleanup above -- re-imported as back-compat shims (search
# above). backend/ask_pipeline.py already reaches into these via
# flask_app_module.<name> attribute access, which keeps working unchanged
# since the imported name still lives on the `app` module object.
# _build_interaction_summary stays defined below -- backend/rag.py's
# _store_user_memory_summary reaches back into it via a lazy `import app`.


def _build_interaction_summary(question, answer):
    clean_q = _normalize_rag_text(question, max_chars=160)
    clean_a = re.sub(r"[#*_`~>\-]+", " ", str(answer or ""))
    clean_a = _normalize_rag_text(clean_a, max_chars=240)
    return f"Q: {clean_q} | A: {clean_a}".strip()


# maybe_require_clerk_auth / require_clerk_auth: reconciled to
# backend/auth.py as part of the Phase 4 auth cleanup above -- re-imported
# here as back-compat shims (search: "Re-import shims").


def _get_prayer_refs(prayer_name):
    """Resolve prayer/service name to a list of Sefaria refs."""
    resolved_name = (unquote(prayer_name or "") or "").strip()
    if resolved_name in SIDDUR_SECTION_MAP:
        return SIDDUR_SECTION_MAP[resolved_name]

    from backend.sefaria_library import get_index_leaf_refs
    return get_index_leaf_refs(resolved_name, max_refs=80)


def _coerce_coordinate(value, min_value, max_value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < min_value or numeric > max_value:
        return None
    return numeric


# Browser/CDN max-age for the HTML shell, /static/*, and manifest.webmanifest
# (see apply_response_cache_policy below). Short by design while the app is
# still under active, frequent deployment: these responses are public and
# cacheable, but the HTML shell references hashed-by-deploy asset URLs, and
# a long max-age means a stale shell won't even ask the server for a fresh
# one until it expires. 5 minutes bounds that window without meaningfully
# hurting cache-hit rate for a single browsing session. Raise this once
# deploys are infrequent (plan.md §14.3 will also split this per-content-type
# tier -- immutable/deterministic/corpus content deserves a much longer
# s-maxage than the shell does; this single shared knob predates that work).
# Env var changes need a Vercel redeploy to take effect same as a code
# change would, so there's no "tune without redeploying" benefit to keeping
# this configurable -- it's a literal.
RESOURCE_RELOAD_SECONDS = 60 * 5
# Flask session cookie lifetime. NOT an auth control -- Clerk auth is
# stateless JWT-per-request (backend/auth.py) and never touches Flask's
# session. The only thing stored here is a last-known lat/lon for zmanim/
# calendar (session['lat']/['lon'], set in routes_calendar.py), so this is a
# low-sensitivity convenience cookie, not a security boundary. 30 days,
# independent of RESOURCE_RELOAD_SECONDS -- a user checking zmanim again
# next week shouldn't have to re-share their location.
SESSION_RELOAD_SECONDS = 60 * 60 * 24 * 30
STATIC_STALE_WHILE_REVALIDATE_SECONDS = max(
    60 * 60,
    min(60 * 60 * 24, RESOURCE_RELOAD_SECONDS // 2),
)

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    seconds=SESSION_RELOAD_SECONDS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = is_production_runtime
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = RESOURCE_RELOAD_SECONDS
# plan.md §16.4 body cap: no route in this app accepts file uploads or any
# payload larger than a small JSON body (the /ask question itself is capped
# to MAX_INPUT_CHARS=1200 chars at the sanitizer). 256 KiB is generous
# headroom for the largest legitimate body (bulk preference/bookmark JSON)
# while blocking abusive oversized requests. This only protects routes
# reached through Werkzeug/Flask -- it does NOT cover asgi.py's native
# FastAPI /ask route, which has its own independent byte cap for the same
# reason the rate limiter needed one (see asgi.py's request_id_middleware).
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024


@app.before_request
def apply_session_cookie_policy():
    # Ensure Flask issues an expiring cookie instead of a browser-session cookie.
    session.permanent = True


@app.before_request
def bind_request_id_context():
    # Deliberately header-only, NOT `or get_request_id()`: the request_id
    # ContextVar is never reset after a request finishes, so on a reused
    # WSGI worker thread (Werkzeug's single-threaded dev server, or any
    # thread pool that recycles threads) a header-less request would
    # silently inherit the previous request's id instead of getting a
    # fresh one. asgi.py's ASGI middleware guarantees this header is
    # always present for requests that arrive through it (client-supplied
    # or injected); Flask running standalone (local dev, direct
    # test_client use) always gets a correctly fresh id here instead.
    g.request_id = bind_request_id(request.headers.get("X-Request-Id"))
    g.request_start_time = time.monotonic()


@app.after_request
def log_request_completion(response):
    # g.request_id only (not `or get_request_id()`) for the same reason as
    # bind_request_id_context above — falling back to the ContextVar risks
    # surfacing a stale value from a prior request on the same thread.
    request_id = getattr(g, "request_id", "")
    if request_id:
        response.headers.setdefault("X-Request-Id", request_id)
    start_time = getattr(g, "request_start_time", None)
    duration_ms = (
        round((time.monotonic() - start_time) * 1000, 2)
        if start_time is not None
        else None
    )
    _request_logger.info(
        "request_complete",
        extra={
            "http_method": request.method,
            "http_path": request.path,
            "http_status": response.status_code,
            "duration_ms": duration_ms,
        },
    )
    return response


@app.after_request
def apply_response_cache_policy(response):
    path = request.path or ""

    # Security headers for every response — single source of truth is
    # backend.helpers.SECURITY_RESPONSE_HEADERS (also applied by asgi.py's
    # request_id_middleware for native FastAPI routes like /ask that bypass
    # this Flask app entirely; see that constant's docstring for the CSP
    # 'unsafe-inline' rationale).
    for header_name, header_value in SECURITY_RESPONSE_HEADERS.items():
        response.headers.setdefault(header_name, header_value)

    # plan.md §14.3.1: explicit per-route cache tier (Immutable /
    # Deterministic-by-date / Corpus-derived / Private), replacing the old
    # blanket /api/* -> no-store branch. classify_cache_tier() is the single
    # source of truth shared with asgi.py's native /ask + /api/async/health
    # routes (backend/cache_policy.py's module docstring has the full
    # rationale). A handler that took a session/user-dependent branch inside
    # an otherwise-public route (e.g. /api/zmanim falling back to the
    # caller's remembered location, /api/holidays' last-resort fallback —
    # see backend/routes_calendar.py) sets g.cache_tier_force_private=True
    # itself; that per-request fact always wins over the static table, since
    # only the handler that ran knows which branch it actually took.
    if getattr(g, "cache_tier_force_private", False):
        cache_control = CACHE_TIER_PRIVATE
    else:
        cache_control = classify_cache_tier(request.method, path)
    if cache_control is not None:
        response.headers["Cache-Control"] = cache_control
        # Informational only (cache-debugging: confirms which deploy served
        # a given cached response) -- NOT the CDN invalidation mechanism.
        # This project has not independently verified whether Vercel's Edge
        # Cache auto-purges on deploy; docs/VERCEL_COST_OPTIMIZATION.md
        # flags that as an operator-confirmation item rather than assuming
        # it. SENTRY_RELEASE is empty in local dev (no VERCEL_GIT_COMMIT_SHA),
        # so the header is simply omitted there.
        if cache_control != CACHE_TIER_PRIVATE and SENTRY_RELEASE:
            response.headers["X-Deploy-Hash"] = SENTRY_RELEASE
        return response

    if path == "/service-worker.js":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    if path.startswith("/static/"):
        response.headers["Cache-Control"] = (
            f"public, max-age={RESOURCE_RELOAD_SECONDS}, "
            f"stale-while-revalidate={STATIC_STALE_WHILE_REVALIDATE_SECONDS}"
        )
        return response

    if path == "/manifest.webmanifest":
        response.headers["Cache-Control"] = (
            f"public, max-age={RESOURCE_RELOAD_SECONDS}, must-revalidate"
        )
        return response

    if path in {"/robots.txt", "/sitemap.xml"}:
        # plan.md §12.5.1: identical for every visitor and slow-changing —
        # same treatment as manifest.webmanifest rather than falling through
        # to no explicit Cache-Control (neither is text/html so the generic
        # branch below never catches them).
        response.headers["Cache-Control"] = (
            f"public, max-age={RESOURCE_RELOAD_SECONDS}, must-revalidate"
        )
        return response

    if response.mimetype in {"text/html", "application/xhtml+xml"}:
        response.headers["Cache-Control"] = (
            f"public, max-age={RESOURCE_RELOAD_SECONDS}, must-revalidate"
        )

    return response


# _parse_multi_value_arg, _extract_search_metadata_filters: reconciled to
# backend/helpers.py as part of the Phase 4 Finding A cleanup above --
# re-imported as back-compat shims (search: "Re-import shims").


def _extract_client_ip():
    return _resolve_client_ip(
        request.headers, remote_addr=request.remote_addr, default="") or None


def _lookup_lat_lon_from_ip():
    """Best-effort IP-geolocation lookup for get_engine()'s location
    fallback. Returns (lat, lon), or (None, None) on failure. Split out to
    keep this loop out of that function's own complexity count
    (SonarCloud python:S3776).
    """
    client_ip = _extract_client_ip()
    ip_target = ""
    if client_ip and client_ip not in {"127.0.0.1", "::1"}:
        ip_target = client_ip

    try:
        # ip-api.com is free, no key required, ~45 req/min limit.
        # Use request IP from Vercel headers instead of server runtime IP.
        # ip-api free tier only supports HTTP; ipwho.is is HTTPS fallback.
        lookup_urls = [
            f"http://ip-api.com/json/{ip_target}?fields=status,lat,lon,timezone,query",
            f"https://ipwho.is/{ip_target}" if ip_target else "https://ipwho.is/",
        ]

        for lookup_url in lookup_urls:
            r = requests.get(lookup_url, timeout=3)
            data = r.json() if r.ok else {}

            ip_lat = None
            ip_lon = None
            if data.get("status") == "success":
                ip_lat = _coerce_coordinate(data.get('lat'), -90, 90)
                ip_lon = _coerce_coordinate(data.get('lon'), -180, 180)
            elif data.get("success") is True:
                ip_lat = _coerce_coordinate(data.get('latitude'), -90, 90)
                ip_lon = _coerce_coordinate(
                    data.get('longitude'), -180, 180)

            if ip_lat is not None and ip_lon is not None:
                return ip_lat, ip_lon
    except Exception as e:
        app.logger.warning(f"Location IP lookup failed: {str(e)}")

    return None, None


def get_engine():
    # Instantiate engine using session location or IP fallback
    lat = _coerce_coordinate(session.get('lat'), -90, 90)
    lon = _coerce_coordinate(session.get('lon'), -180, 180)

    if lat is None or lon is None:
        ip_lat, ip_lon = _lookup_lat_lon_from_ip()
        if ip_lat is not None and ip_lon is not None:
            lat = ip_lat
            lon = ip_lon
            session['lat'] = lat
            session['lon'] = lon

    if lat is None or lon is None:
        # Defaulting to NYC if all lookup methods fail
        app.logger.info("Using default location: New York City")
        lat, lon = (40.7128, -74.0060)

    return ShelahEngine(lat=lat, lon=lon)


@app.route("/")
@app.route("/settings")
@app.route("/profile")
def index():
    # Daily-study (daf yomi / mishnah yomi / rambam / hebrew date) used to be
    # fetched here via a synchronous Sefaria API call, blocking the homepage's
    # TTFB on every cold-cache instance. The client now fetches
    # /api/daily-study itself after first paint (populateDailyStudy() in
    # index.html) and fills in the skeleton.
    return render_template(
        "index.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
        sentry_dsn_browser=SENTRY_DSN_BROWSER,
        sentry_environment=SENTRY_ENVIRONMENT,
        sentry_release=SENTRY_RELEASE,
        legal_terms_version=LEGAL_TERMS_VERSION,
        legal_privacy_version=LEGAL_PRIVACY_VERSION,
    )


@app.route("/terms")
def terms():
    return render_template(
        "terms.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
        legal_terms_version=LEGAL_TERMS_VERSION,
    )


@app.route("/privacy")
def privacy():
    return render_template(
        "privacy.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
        legal_privacy_version=LEGAL_PRIVACY_VERSION,
    )


@app.route("/accessibility")
def accessibility():
    return render_template(
        "accessibility.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@app.route("/manifest.webmanifest")
def web_manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")


@app.route("/service-worker.js")
def service_worker():
    deploy_hash = os.environ.get("DEPLOY_HASH", "v8")
    sw_path = Path(app.static_folder) / "service-worker.js"
    content = sw_path.read_text(encoding="utf-8")
    versioned = re.sub(
        r'const CACHE_VERSION = "[^"]*"',
        f'const CACHE_VERSION = "{deploy_hash}"',
        content,
        count=1,
    )
    return Response(
        versioned,
        mimetype="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template(
        "404.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    ), 404


def _ask_question_prayer_payload(question, mode, answer_language, canonical_lens):
    """Stage 0 of ask_question(): prayer-keyword early return, or None if
    `question` isn't a prayer question. Split out to keep this pipeline
    stage's branching out of that route's own complexity count
    (SonarCloud python:S3776).
    """
    if not any(prayer in question for prayer in ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]):
        return None

    DEVTOOLS_STATS["answers_total"] += 1
    prayer_answer = (
        f"Prayer Service Guide\n\n{question}\n\n"
        "You can browse full liturgy books and services from the prayer sections. "
        "For practical application, compare local community custom with your rabbi's guidance."
    )
    if answer_language == "he":
        prayer_answer = (
            f"מדריך תפילה\n\n{question}\n\n"
            "ניתן לעיין בספרי התפילה והשירותים הליטורגיים המלאים באזור התפילה. "
            "להכרעה מעשית יש להשוות למנהג הקהילה המקומית ולהתייעץ עם הרב שלך."
        )
    return {
        "answer": prayer_answer,
        "confidence": 0.85,
        "sources": [{
            "ref": "Sefaria Liturgy",
            "title": "Sefaria Prayer Books",
            "lines": [{"en": f"Prayer Service: {question}", "he": f"תפילה: {question}"}]
        }],
        "customs": [],
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": 1,
            "custom_count": 0,
            "generated_at": int(time.time()),
            "fallback": False,
            "cached": False,
        }
    }


def _collect_primary_sources_sync(question, engine):
    """Fetch + fully resolve the primary Sefaria source texts for a
    question (thread-pool parallel). Split out of ask_question()
    (SonarCloud python:S3776) -- see _ask_question_prayer_payload.
    """
    primary_refs = sefaria.find_refs_for_question(question)
    max_primary_refs = _env_int(
        "ASK_PRIMARY_SOURCE_LIMIT", 4)  # Capped at 4 for speed
    max_primary_refs = max(1, min(max_primary_refs, 8))
    primary_ref_candidates = []
    for ref in primary_refs:
        normalized_ref = str(ref or "").strip()
        if not normalized_ref:
            continue
        primary_ref_candidates.append(normalized_ref)
        if len(primary_ref_candidates) >= max_primary_refs:
            break

    primary_sources = []
    if primary_ref_candidates:
        source_futures = [
            submit_with_context(_THREAD_POOL, engine.get_library_text, ref)
            for ref in primary_ref_candidates
        ]
        for future in source_futures:
            try:
                source_data = future.result(timeout=3)
            except Exception:
                continue
            if isinstance(source_data, dict):
                primary_sources.append(source_data)
    return primary_sources


def _flatten_primary_sources_for_claude(primary_sources, answer_language):
    """Flatten primary source line dicts into {ref, text} pairs for the AI
    prompt. Split out of ask_question() (SonarCloud python:S3776) -- see
    _ask_question_prayer_payload.
    """
    flat_sources_for_claude = []
    for src in primary_sources:
        src_lines = src.get('lines', []) if isinstance(src, dict) else []
        if not isinstance(src_lines, list):
            src_lines = []
        preferred_lines = []
        for line in src_lines:
            if not isinstance(line, dict):
                continue
            preferred = (line.get('he') or line.get('en')) if answer_language == 'he' else (
                line.get('en') or line.get('he'))
            if preferred:
                preferred_lines.append(str(preferred).strip())
        flat_sources_for_claude.append({
            'ref': str(src.get('ref') or '') if isinstance(src, dict) else '',
            'text': ' '.join(preferred_lines)
        })
    return flat_sources_for_claude


def _collect_ask_question_context(question, canonical_lens, user_id, answer_language, engine):
    """Stage 1 of ask_question(): parallel source/knowledge collection
    (thread-pool based, since this is the sync Flask route). Returns a
    context dict consumed by the strict-guard and AI-synthesis stages
    below. Split out of ask_question() (SonarCloud python:S3776) -- see
    _ask_question_prayer_payload.
    """
    primary_sources = _collect_primary_sources_sync(question, engine)

    # 2-4. Fetch remaining context in parallel using the module-level pool.
    halachipedia_future = submit_with_context(
        _THREAD_POOL, engine.get_halachipedia_summary, question)
    knowledge_future = submit_with_context(
        _THREAD_POOL,
        _retrieve_community_knowledge,
        question,
        canonical_lens=canonical_lens,
        max_rows=RAG_TOP_KNOWLEDGE_ROWS,
    )
    memory_future = submit_with_context(
        _THREAD_POOL,
        _fetch_user_memory_summaries,
        user_id,
        limit=RAG_MEMORY_ROWS,
    )
    wiki_future = submit_with_context(_THREAD_POOL, engine.get_wiki, question)

    try:
        halachipedia_info = halachipedia_future.result(timeout=4)
    except Exception:
        halachipedia_info = None
    try:
        knowledge_rows = knowledge_future.result(timeout=4)
    except Exception:
        knowledge_rows = []
    try:
        user_memory_summaries = memory_future.result(timeout=5)
    except Exception:
        user_memory_summaries = []
    try:
        wiki_info = wiki_future.result(timeout=3)
    except Exception:
        wiki_info = None

    halachipedia_list = [halachipedia_info] if halachipedia_info else []
    knowledge_rows = knowledge_rows if isinstance(
        knowledge_rows, list) else []
    user_memory_summaries = user_memory_summaries if isinstance(
        user_memory_summaries, list) else []
    customs_info = _knowledge_rows_to_customs(knowledge_rows)
    wiki_list = [wiki_info] if wiki_info else []

    # 5. Prepare flattened primary source text for the protected AI wrapper.
    flat_sources_for_claude = _flatten_primary_sources_for_claude(
        primary_sources, answer_language)

    has_primary_sources = bool(flat_sources_for_claude)
    has_customs = bool(knowledge_rows)
    has_whitelisted_external = bool(halachipedia_list)
    use_tertiary_web_context = (
        not has_primary_sources
        and not has_customs
        and not has_whitelisted_external
    )
    wiki_context_for_claude = wiki_list if use_tertiary_web_context else []

    return {
        "primary_sources": primary_sources,
        "knowledge_rows": knowledge_rows,
        "user_memory_summaries": user_memory_summaries,
        "halachipedia_list": halachipedia_list,
        "wiki_list": wiki_list,
        "customs_info": customs_info,
        "flat_sources_for_claude": flat_sources_for_claude,
        "has_primary_sources": has_primary_sources,
        "has_customs": has_customs,
        "has_whitelisted_external": has_whitelisted_external,
        "use_tertiary_web_context": use_tertiary_web_context,
        "wiki_context_for_claude": wiki_context_for_claude,
    }


def _ask_question_strict_payload(mode, canonical_lens, ctx):
    """Stage 2 of ask_question(): strict-mode guard, or None if the
    request isn't blocked. Split out of ask_question() (SonarCloud
    python:S3776) -- see _ask_question_prayer_payload.
    """
    if mode != "strict" or ctx["flat_sources_for_claude"]:
        return None

    DEVTOOLS_STATS["answers_total"] += 1
    DEVTOOLS_STATS["strict_blocks"] += 1
    DEVTOOLS_STATS["fallback_answers"] += 1
    display_sources = _compact_ai_sources(ctx["primary_sources"])
    return {
        "answer": (
            "Strict Sources Mode could not complete this request because no primary Sefaria sources "
            "were matched with sufficient confidence. Please refine the question with a text reference."
        ),
        "confidence": 0.2,
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": display_sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "community_lens": canonical_lens,
            "source_count": 0,
            "custom_count": len(ctx["customs_info"]),
            "generated_at": int(time.time()),
            "fallback": True,
            "strict_blocked": True,
            "safety_class": "ok",
            "cached": False,
        }
    }


def _security_blocked_ask_payload(
    result, mode, canonical_lens, knowledge_rows, user_memory_summaries, user_id, question_was_sanitized,
    question="", answer_language="en",
):
    """The "security_blocked" branch of ask_question()'s AI-synthesis
    stage. Split out of ask_question() (SonarCloud python:S3776) -- see
    _ask_question_prayer_payload.
    """
    blocked_answer = str(result.get("answer") or "").strip()
    if not blocked_answer:
        blocked_answer = "Request blocked by security policy. Please submit a direct halakhic question."

    # This branch covers every security_blocked_* case, including the
    # §8.B-AGE safety-referral classes (medical/self-harm/abuse) built by
    # claude._build_safety_referral_result -- those carry a real
    # safety_class/rabbinic_disclaimer on result["structured"] that must
    # reach the UI so the referral banner (plan.md §8.B.1) renders instead
    # of the ordinary answer chrome. Domain refusals set safety_class too
    # (claude.py's blocked_structured), defaulting to "ok" only for the
    # plain security_blocked_input/output cases that never classified.
    structured_payload = result.get("structured")
    if not isinstance(structured_payload, dict):
        structured_payload = None
    safety_class = (structured_payload or {}).get("safety_class", "ok")

    DEVTOOLS_STATS["answers_total"] += 1
    DEVTOOLS_STATS["fallback_answers"] += 1

    # Defensibility logging (plan.md §8.B.6) applies here too -- this branch
    # is exactly where the highest-risk safety-referral answers (medical /
    # self-harm / abuse) live, and skipping the log for them (as this
    # function previously did) would mean the interactions with the most
    # liability exposure were the ones never retained for dispute
    # reconstruction. Mirrored by asgi.py's own dedicated
    # _security_blocked_ask_async_payload (plan.md §22.3.2 parity suite).
    # Deliberately does NOT also call _store_user_memory_summary here --
    # that's a separate mechanism (fed back into future prompts as context)
    # outside plan.md §8.B.6's scope.
    _store_ask_history(
        user_id,
        question,
        blocked_answer,
        sources=[],
        ai_cited_sources=[],
        community=canonical_lens,
        mode=mode,
        language=answer_language,
        safety_class=safety_class,
        prompt_version=claude.PROMPT_VERSION,
    )

    return {
        "answer": blocked_answer,
        "confidence": result.get("confidence", 0),
        "wiki": [],
        "customs": [],
        "sources": [],
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "community_lens": canonical_lens,
            "source_count": 0,
            "custom_count": 0,
            "knowledge_count": len(knowledge_rows),
            "memory_count": len(user_memory_summaries),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": True,
            "structured": False,
            "is_prohibited": bool((structured_payload or {}).get("is_prohibited", False)),
            "input_sanitized": question_was_sanitized,
            "security": result.get("security") or {},
            "safety_class": (structured_payload or {}).get("safety_class", "ok"),
            "rabbinic_disclaimer": (structured_payload or {}).get(
                "rabbinic_disclaimer") or claude.RABBI_FINAL_RULING_FOOTER,
            "cached": False,
        }
    }


def _run_ask_question_ai_synthesis(
    question, mode, canonical_lens, answer_language, user_id, question_was_sanitized, ctx, engine,
):
    """Stage 3 of ask_question(): AI synthesis. Returns the response
    payload for the security-blocked or success cases; raises on any
    other failure so the caller's except-block can run the fallback.
    Split out of ask_question() (SonarCloud python:S3776) -- see
    _ask_question_prayer_payload.
    """
    # Bounded by AI_TOTAL_BUDGET_SECONDS via the module-level _THREAD_POOL
    # so a slow/stuck model call can't hang this request indefinitely —
    # mirrors the asyncio.wait_for budget on the asgi.py async path
    # (plan.md §23.4). On timeout this raises concurrent.futures.
    # TimeoutError, which is an Exception subclass and falls through to
    # the existing fallback ladder below, unchanged.
    #
    # AI_AGENTIC_TOOLS (plan.md §9.4, Prompt 20, env-default off) swaps in
    # the agentic tool-use loop; ask_pipeline.run_agentic_ask() is a
    # coroutine function, so the thread-pool worker runs it via
    # asyncio.run() -- that worker thread has no event loop of its own
    # (only the ASGI app's event loop, on a different thread, does), so
    # asyncio.run() here is the correct, non-conflicting way to drive it.
    # Off (the default), this branch is never taken and behavior is
    # byte-for-byte the pre-existing claude.ask_claude() call.
    if claude.AI_AGENTIC_TOOLS:
        def _run_agentic(**kwargs):
            return asyncio.run(ask_pipeline.run_agentic_ask(**kwargs))
        _ask_future = submit_with_context(
            _THREAD_POOL,
            _run_agentic,
            question=question,
            sefaria_sources=ctx["flat_sources_for_claude"],
            customs=ctx["customs_info"],
            user_memories=ctx["user_memory_summaries"],
            wiki=ctx["wiki_context_for_claude"],
            halachipedia=ctx["halachipedia_list"],
            mode=mode,
            community_lens=canonical_lens,
            answer_language=answer_language,
            tool_context=_build_ask_tool_context(engine),
        )
    else:
        _ask_future = submit_with_context(
            _THREAD_POOL,
            claude.ask_claude,
            question=question,
            sefaria_sources=ctx["flat_sources_for_claude"],
            customs=ctx["customs_info"],
            user_memories=ctx["user_memory_summaries"],
            wiki=ctx["wiki_context_for_claude"],
            halachipedia=ctx["halachipedia_list"],
            mode=mode,
            community_lens=canonical_lens,
            answer_language=answer_language,
            tool_context=_build_ask_tool_context(engine),
        )
    result = _ask_future.result(timeout=claude.AI_TOTAL_BUDGET_SECONDS)

    result = _coerce_ai_answer_shape(
        result,
        question,
        mode,
        answer_language=answer_language,
    )

    result_error = str(result.get("error") or "")
    if result_error and not result_error.startswith("security_blocked"):
        raise RuntimeError(result_error or "AI request failed")

    if result_error.startswith("security_blocked"):
        return _security_blocked_ask_payload(
            result, mode, canonical_lens, ctx["knowledge_rows"],
            ctx["user_memory_summaries"], user_id, question_was_sanitized,
            question=question, answer_language=answer_language,
        )

    structured_payload = result.get("structured")
    if not isinstance(structured_payload, dict):
        structured_payload = None

    raw_ai_answer = ""
    if structured_payload:
        raw_ai_answer = claude.render_structured_markdown(
            structured_payload,
            answer_language=answer_language,
            is_simple=bool(result.get("is_simple", False)),
        )
    else:
        raw_ai_answer = str(result.get("answer") or "").strip()

    if not raw_ai_answer:
        raise RuntimeError("AI response was empty")

    # plan.md §9.3 point 3: an agentic answer that actually invoked
    # web_search must carry the same general-web warning as the pre-fetch
    # path's tertiary-web-context fallback, even when the pre-fetch wiki
    # context itself was empty (the two are unrelated when the flag is on
    # -- the model may have reached for the live web_search tool on a turn
    # where the pre-fetch RAG step never touched wiki content at all).
    # "used_web_search" is only ever present on a run_agentic_ask() result.
    if "used_web_search" in result:
        needs_web_warning = bool(result.get("used_web_search"))
    else:
        needs_web_warning = ctx["use_tertiary_web_context"] and bool(
            ctx["wiki_context_for_claude"])

    # The "educational information, not a halachic ruling" disclaimer is
    # shown persistently in the UI banner (renderDisclaimerBanner in
    # templates/index.html) -- it must not also be baked into the answer
    # text itself, or it renders twice.
    normalized_answer = _compose_answer_with_prefixes(
        raw_ai_answer,
        include_web_warning=needs_web_warning,
    )
    if not str(normalized_answer or "").strip():
        raise RuntimeError("AI response normalized to empty content")

    result["answer"] = normalized_answer
    _store_user_memory_summary(user_id, question, normalized_answer)

    DEVTOOLS_STATS["answers_total"] += 1
    display_sources = _compact_ai_sources(ctx["primary_sources"])
    ai_cited = extract_ai_cited(structured_payload)

    _store_ask_history(
        user_id,
        question,
        normalized_answer,
        sources=display_sources,
        ai_cited_sources=ai_cited,
        community=canonical_lens,
        mode=mode,
        language=answer_language,
        safety_class=(structured_payload or {}).get("safety_class", "ok"),
        prompt_version=claude.PROMPT_VERSION,
    )

    # Successful AI answer path returns immediately; fallback is only for empty/error responses.
    return {
        "answer": normalized_answer,
        "confidence": result.get("confidence"),
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": display_sources,
        "ai_cited_sources": ai_cited,
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": len(ctx["primary_sources"]),
            "custom_count": len(ctx["customs_info"]),
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": bool(result.get("is_fallback", False)),
            "structured": bool(structured_payload),
            "is_prohibited": bool((structured_payload or {}).get("is_prohibited", False)),
            "input_sanitized": question_was_sanitized,
            "security": result.get("security") or {},
            "safety_class": (structured_payload or {}).get("safety_class", "ok"),
            "rabbinic_disclaimer": (structured_payload or {}).get(
                "rabbinic_disclaimer") or claude.RABBI_FINAL_RULING_FOOTER,
            "cached": False,
        }
    }


def _run_ask_question_fallback(question, mode, canonical_lens, answer_language, user_id, ai_error, ctx):
    """Stage 4 of ask_question(): halakhic-source-discovery fallback, run
    when _run_ask_question_ai_synthesis() raises. Split out of
    ask_question() (SonarCloud python:S3776) -- see
    _ask_question_prayer_payload.
    """
    _capture_backend_error(
        "ask_ai_synthesis_failed",
        ai_error,
        {
            "question": question,
            "mode": mode,
            "community_lens": canonical_lens,
            "user_id": user_id or "",
        },
    )
    fallback_payload = get_halakhic_sources(question)
    fallback_warning = str(
        fallback_payload.get("warning") or "").strip()

    fallback_answer = _compose_answer_with_prefixes(
        "## Ruling\n\nAI synthesis unavailable. Returning discovered halakhic references.",
        include_web_warning=bool(fallback_warning),
    )

    _store_user_memory_summary(user_id, question, fallback_answer)

    DEVTOOLS_STATS["answers_total"] += 1
    DEVTOOLS_STATS["fallback_answers"] += 1
    fallback_sources = _compact_ai_sources(
        fallback_payload.get("sources", []))
    return {
        "answer": fallback_answer,
        "confidence": 0.4,
        "wiki": ctx["wiki_list"] + ctx["halachipedia_list"],
        "customs": ctx["customs_info"],
        "sources": fallback_sources,
        "ai_cited_sources": [],
        "meta": {
            "mode": mode,
            "language": answer_language,
            "community_lens": canonical_lens,
            "source_count": fallback_payload.get("source_count", 0),
            "custom_count": len(ctx["customs_info"]),
            "knowledge_count": len(ctx["knowledge_rows"]),
            "memory_count": len(ctx["user_memory_summaries"]),
            "identity_aware": bool(user_id),
            "generated_at": int(time.time()),
            "fallback": True,
            "status": fallback_payload.get("status", "fallback"),
            "fallback_detail": {
                "keywords": fallback_payload.get("keywords", []),
                "sequence": fallback_payload.get("sequence", []),
                "counts": fallback_payload.get("counts", {}),
                "level": fallback_payload.get("fallback_level", "unknown"),
                "warning": fallback_warning,
                "reason": _coarse_ai_error_reason(ai_error),
            },
            "safety_class": "ok",
            "cached": False,
        }
    }


@app.route("/ask", methods=["POST"])
@maybe_require_clerk_auth
def ask_question():
    data = request.get_json(silent=True) or {}
    raw_question = data.get("question", "")
    question = claude.sanitize_user_query(raw_question)
    question_was_sanitized = question != str(raw_question or "").strip()

    if not question:
        return jsonify({"error": "No valid question provided"}), 400
    answer_language = str(data.get("language") or "en").strip().lower()
    if answer_language not in {"en", "he"}:
        answer_language = "en"
    mode = _sanitize_answer_mode(data.get("mode"))
    community_lens = (data.get("community") or "All").strip()
    canonical_lens = "All" if community_lens.lower() == "all" else (
        _canonicalize_community_name(community_lens) or community_lens)
    user_id = _get_request_user_id()
    ask_cache_key = "|".join([
        question.lower(),
        answer_language,
        mode,
        canonical_lens.lower(),
        user_id or "anon",
    ])

    cached_payload = _get_cached_ask_payload(ask_cache_key)
    if cached_payload is not None:
        cached_meta = cached_payload.get("meta")
        if isinstance(cached_meta, dict):
            cached_meta["cached"] = True
            cached_meta["generated_at"] = int(time.time())
        return jsonify(cached_payload)

    try:
        engine = get_engine()

        prayer_payload = _ask_question_prayer_payload(
            question, mode, answer_language, canonical_lens)
        if prayer_payload is not None:
            _set_cached_ask_payload(ask_cache_key, prayer_payload)
            return jsonify(prayer_payload)

        ctx = _collect_ask_question_context(
            question, canonical_lens, user_id, answer_language, engine)

        strict_payload = _ask_question_strict_payload(mode, canonical_lens, ctx)
        if strict_payload is not None:
            _set_cached_ask_payload(ask_cache_key, strict_payload)
            return jsonify(strict_payload)

        try:
            payload = _run_ask_question_ai_synthesis(
                question, mode, canonical_lens, answer_language, user_id,
                question_was_sanitized, ctx, engine,
            )
            _set_cached_ask_payload(ask_cache_key, payload)
            return jsonify(payload)

        except Exception as ai_error:
            fallback_payload_response = _run_ask_question_fallback(
                question, mode, canonical_lens, answer_language, user_id,
                ai_error, ctx,
            )
            # Do NOT cache AI failure/fallback responses — allow next request to retry.
            return jsonify(fallback_payload_response)

    except Exception as e:
        _capture_backend_error(
            "ask_route_critical_error",
            e,
            {
                "question": question if "question" in locals() else "",
                "mode": mode if "mode" in locals() else "",
                "community_lens": canonical_lens if "canonical_lens" in locals() else "",
            },
        )
        return jsonify({"error": "An internal error occurred while processing your request."}), 500


# ─── COMMUNITY CUSTOMS DATA (Merkava) ─────────────────────────────────────────
# Shared community registry consumed by backend/routes_community.py and by the
# _canonicalize_community_name / _detect_community_in_text helpers above.

COMMUNITIES = {
    "Ashkenaz": "ashkenaz",
    "Bukharian": "bukharian",
    "Ethiopian": "ethiopian",
    "Georgian": "georgian",
    "Greek-Romaniote": "greek-romaniote",
    "Iraqi": "iraqi",
    "Kavkazi": "mountain-jewish-kavkazi",
    "Syrian": "syrian",
    "Persian": "persian",
    "Sefardic": "sefardic",
    "Turkish-Ottoman": "turkish-ottoman-sefardic",
    "Yemenite": "yemenite",
    "Moroccan": "moroccan",
    "Israeli": "sefardic",
}

COMMUNITY_ALIASES = {
    "ashkenazi": "Ashkenaz",
    "ashkenaz": "Ashkenaz",
    "sefardi": "Sefardic",
    "sephardi": "Sefardic",
    "sefardic": "Sefardic",
    "sephardic": "Sefardic",
    "iraqi": "Iraqi",
    "mizrahi": "Iraqi",
    "syrian": "Syrian",
    "yemenite": "Yemenite",
    "yemeni": "Yemenite",
    "moroccan": "Moroccan",
    "morrocan": "Moroccan",
    "israeli": "Israeli",
    "israel": "Israeli",
    "kavkazi": "Kavkazi",
    "mountain jewish": "Kavkazi",
    "mountain-jewish": "Kavkazi",
    "kavkazi jews": "Kavkazi",
    "mountain-jewish-kavkazi": "Kavkazi",
    "bukharan": "Bukharian",
    "bukharian": "Bukharian",
    "ethiopian": "Ethiopian",
    "beta israel": "Ethiopian",
    "georgian": "Georgian",
    "persian": "Persian",
    "iranian": "Persian",
    "greek": "Greek-Romaniote",
    "romaniote": "Greek-Romaniote",
    "greek-romaniote": "Greek-Romaniote",
    "turkish": "Turkish-Ottoman",
    "ottoman": "Turkish-Ottoman",
    "ottoman sefardic": "Turkish-Ottoman",
    "turkish ottoman": "Turkish-Ottoman",
    "turkish ottoman sefardic": "Turkish-Ottoman",
    "turkish-ottoman community": "Turkish-Ottoman",
    "turkish ottoman community": "Turkish-Ottoman",
    "turkish-ottoman": "Turkish-Ottoman",
    "turkish-ottoman-sefardic": "Turkish-Ottoman",
}


# ─── Blueprint registration (Stage 2 route decomposition) ────────────────
# Imported at the bottom so each blueprint's `from app import ...` resolves
# against a fully-initialized app module (no circular-import trap).
# When run as `python3 app.py` the module is registered as __main__, not 'app'.
# Blueprint files that do `from app import X` would trigger a full re-import of
# app.py and a circular-import deadlock.  Alias __main__ as 'app' here so those
# imports resolve against the already-running module instead.
import sys as _sys  # noqa: E402 -- deliberate: the __main__ alias must be installed after app is built
_sys.modules.setdefault('app', _sys.modules['__main__'])
del _sys

# Each import is wrapped individually: a syntax error or import-time exception
# in one blueprint file must not silently swallow the others — it raises
# immediately with the offending module name so the deploy fails loudly
# rather than serving 404s for an unknown subset of routes.
_BLUEPRINTS = [
    ("backend.routes_library", "routes_library"),
    ("backend.routes_prayers", "routes_prayers"),
    ("backend.routes_community", "routes_community"),
    ("backend.routes_calendar", "routes_calendar"),
    ("backend.routes_user", "routes_user"),
    ("backend.routes_devtools", "routes_devtools"),
    ("backend.routes_legal", "routes_legal"),
    ("backend.routes_privacy", "routes_privacy"),
    ("backend.routes_pages", "routes_pages"),
    ("backend.routes_feedback", "routes_feedback"),
]

import importlib as _importlib  # noqa: E402 -- deliberate: blueprint registration must follow app construction
for _mod_path, _bp_name in _BLUEPRINTS:
    try:
        _mod = _importlib.import_module(_mod_path)
        _bp = getattr(_mod, _bp_name)
        app.register_blueprint(_bp)
    except Exception as _bp_exc:
        app.logger.critical(
            "FATAL: blueprint registration failed for %s.%s — %s",
            _mod_path, _bp_name, _bp_exc,
            exc_info=True,
        )
        raise RuntimeError(
            f"Blueprint '{_bp_name}' from '{_mod_path}' failed to load: {_bp_exc}"
        ) from _bp_exc
del _importlib, _BLUEPRINTS, _mod_path, _bp_name, _mod, _bp

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5001))
    debug_mode = os.environ.get(
        'FLASK_DEBUG', '').strip().lower() in ('1', 'true')
    app.run(debug=debug_mode, host='0.0.0.0', port=port)
