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

import json
import re
import requests
from flask import Flask, render_template, request, jsonify, session, g, send_from_directory
from dotenv import load_dotenv
import time
import os
from datetime import date as greg_date, timedelta, datetime
from functools import wraps
from urllib.parse import unquote
from pathlib import Path

import jwt
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

from data_service import ShelahEngine
import sefaria
import claude

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

ANSWER_MODES = {"balanced", "practical", "sources", "strict"}

DEVTOOLS_STATS = {
    "answers_total": 0,
    "fallback_answers": 0,
    "strict_blocks": 0,
    "segment_reports": 0,
}

QUICK_TEXT_ALIASES = {
    "genesis": "Genesis 1",
    "bereishit": "Genesis 1",
    "exodus": "Exodus 1",
    "shemot": "Exodus 1",
    "leviticus": "Leviticus 1",
    "vayikra": "Leviticus 1",
    "numbers": "Numbers 1",
    "bamidbar": "Numbers 1",
    "deuteronomy": "Deuteronomy 1",
    "devarim": "Deuteronomy 1",
    "psalms": "Psalms 1",
    "tehillim": "Psalms 1",
    "proverbs": "Proverbs 1",
    "mishlei": "Proverbs 1",
    "jonathan sacks": "Covenant and Conversation; Genesis; The Book of the Beginnings, Living with the Times; The Parasha",
    "jonathan sacks essays": "The Jonathan Sacks Haggadah; Essays, The Story of Stories",
    "jonathan sacks haggadah essays": "The Jonathan Sacks Haggadah; Essays, The Story of Stories",
    "covenant and conversation": "Covenant and Conversation; Genesis; The Book of the Beginnings, Living with the Times; The Parasha",
    "everett fox": "The Early Prophets, by Everett Fox, Joshua, Part I; Preparations for Conquest",
    "the early prophets by everett fox": "The Early Prophets, by Everett Fox, Joshua, Part I; Preparations for Conquest",
    "the early prophets, by everett fox": "The Early Prophets, by Everett Fox, Joshua, Part I; Preparations for Conquest",
    "the five books of moses by everett fox": "The Five Books of Moses, by Everett Fox, Translator's Preface",
    "the five books of moses, by everett fox": "The Five Books of Moses, by Everett Fox, Translator's Preface",
}

HEBREW_DIACRITICS_RE = re.compile(r"[\u0591-\u05C7]")
HEBREW_LETTER_RE = re.compile(r"[\u05D0-\u05EA]")

TRANSLATION_CACHE = {}

HEBREW_WORD_GLOSSARY = {
    "שבת": "Shabbat, the seventh day of rest.",
    "תורה": "Torah, the Five Books of Moses and Torah teaching.",
    "תפילה": "Prayer.",
    "מצוה": "Mitzvah, a divine commandment.",
    "מצווה": "Mitzvah, a divine commandment.",
    "הלכה": "Halakhah, practical Jewish law.",
    "מנהג": "Minhag, accepted communal custom.",
    "תשובה": "Teshuvah, repentance and return.",
    "ברכה": "Berakhah, blessing.",
    "פסח": "Pesach, the festival of the Exodus.",
    "סוכות": "Sukkot, the festival of booths.",
    "שבועות": "Shavuot, festival marking Matan Torah.",
    "ראש": "Head or beginning.",
    "שלום": "Peace, well-being, or greeting.",
    "חסד": "Kindness or loving-kindness.",
    "אמת": "Truth.",
    "יראה": "Awe or reverence.",
    "אהבה": "Love.",
}

APP_ROOT = Path(__file__).resolve().parent
SEFARIA_SEARCH_WRAPPER_URL = "https://www.sefaria.org/api/search-wrapper"

HALAKHIC_CORPUS_ALIASES = {
    "Shulchan Arukh": [
        "shulchan arukh",
        "shulchan aruch",
        "orach chayim",
        "yoreh de'ah",
        "yoreh deah",
        "even haezer",
        "choshen mishpat",
    ],
    "Rambam": [
        "rambam",
        "mishneh torah",
        "moses maimonides",
    ],
    "Mishnah Berurah": [
        "mishnah berurah",
        "mishna berura",
    ],
    "Talmud": [
        "talmud",
        "bavli",
        "yerushalmi",
    ],
    "Gemara": [
        "gemara",
        "talmud",
        "tractate",
    ],
}

QUERY_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "have", "has",
    "during", "about", "into", "when", "what", "where", "which", "does", "is", "can", "may", "if",
    "allowed", "halacha", "halakhah", "question", "please", "tell", "me", "us", "you",
    "איך", "מה", "האם", "עם", "של", "על", "גם", "לא", "כן",
}


def _extract_query_keywords(query, max_keywords=8):
    tokens = re.findall(r"[A-Za-z\u0590-\u05FF]{3,}", str(query or "").lower())
    keywords = []
    for token in tokens:
        if token in QUERY_STOPWORDS:
            continue
        if token not in keywords:
            keywords.append(token)
        if len(keywords) >= max_keywords:
            break
    return keywords


def _query_search_wrapper(query_text, size=12):
    payload = {
        "type": "text",
        "query": query_text,
        "field": "naive_lemmatizer",
        "source_proj": True,
        "slop": 10,
        "start": 0,
        "size": size,
        "filters": [],
        "filter_fields": [],
        "aggs": [],
        "sort_method": "score",
        "sort_fields": ["pagesheetrank"],
        "sort_reverse": False,
        "sort_score_missing": 0.04,
    }

    try:
        resp = requests.post(
            SEFARIA_SEARCH_WRAPPER_URL,
            json=payload,
            timeout=8,
        )
        if not resp.ok:
            return []
        data = resp.json() if resp.content else {}
        return ((data.get("hits") or {}).get("hits") or [])
    except Exception:
        return []


def _match_corpus(hit_source, aliases):
    if not isinstance(hit_source, dict):
        return False

    categories = hit_source.get("categories", [])
    title_variants = hit_source.get("titleVariants", [])
    haystack = " ".join([
        str(hit_source.get("ref") or ""),
        str(hit_source.get("path") or ""),
        " ".join(categories if isinstance(categories, list) else []),
        " ".join(title_variants if isinstance(title_variants, list) else []),
    ]).lower().replace("_", " ")

    return any(alias in haystack for alias in aliases)


def _extract_hit_snippet(hit_source):
    for key in ("naive_lemmatizer", "exact", "content"):
        raw = hit_source.get(key, "")
        if isinstance(raw, str) and raw.strip():
            return re.sub(r"\s+", " ", raw).strip()[:340]
    return ""


def _iter_local_json_matches(payload, keywords, file_name, pointer="root"):
    matches = []

    if isinstance(payload, dict):
        for field in ("Minhag", "minhag", "Title", "title"):
            value = payload.get(field)
            if not isinstance(value, str):
                continue
            lowered = value.lower()
            hit_keywords = [kw for kw in keywords if kw in lowered]
            if not hit_keywords:
                continue
            matches.append({
                "file": file_name,
                "field": field,
                "value": value,
                "match_keywords": hit_keywords,
                "pointer": pointer,
            })

        for key, value in payload.items():
            child_pointer = f"{pointer}.{key}"
            matches.extend(_iter_local_json_matches(
                value, keywords, file_name, child_pointer))

    elif isinstance(payload, list):
        for idx, item in enumerate(payload):
            child_pointer = f"{pointer}[{idx}]"
            matches.extend(_iter_local_json_matches(
                item, keywords, file_name, child_pointer))

    return matches


def _find_local_custom_matches(keywords, max_results=12):
    roots = [
        APP_ROOT / ".github" / "customs",
        APP_ROOT / "customs",
    ]

    collected = []
    seen = set()

    for root in roots:
        if not root.exists() or not root.is_dir():
            continue

        for file_path in sorted(root.glob("*.json")):
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            for match in _iter_local_json_matches(payload, keywords, file_path.name):
                key = (
                    match.get("file", ""),
                    match.get("field", ""),
                    str(match.get("value", "")).lower(),
                    match.get("pointer", ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                collected.append(match)
                if len(collected) >= max_results:
                    return collected

    return collected


def get_halakhic_sources(query):
    """Deterministic fallback source retrieval without any LLM calls."""
    question = str(query or "").strip()
    keywords = _extract_query_keywords(question)
    if not keywords and question:
        keywords = [question.lower()]

    sources = []
    seen_refs = set()
    search_query = " ".join(keywords[:5]).strip() or question
    hit_pool = _query_search_wrapper(search_query, size=120)

    for corpus, aliases in HALAKHIC_CORPUS_ALIASES.items():
        added_for_corpus = 0
        corpus_hits = [
            hit for hit in hit_pool
            if _match_corpus((hit or {}).get("_source", {}), aliases)
        ]

        if not corpus_hits:
            focused_hits = _query_search_wrapper(
                f"{corpus} {search_query}".strip(), size=40)
            corpus_hits = [
                hit for hit in focused_hits
                if _match_corpus((hit or {}).get("_source", {}), aliases)
            ]

        if not corpus_hits:
            focused_hits = _query_search_wrapper(corpus, size=40)
            corpus_hits = [
                hit for hit in focused_hits
                if _match_corpus((hit or {}).get("_source", {}), aliases)
            ]

        for hit in corpus_hits:
            hit_source = hit.get("_source", {}) if isinstance(
                hit, dict) else {}
            if not isinstance(hit_source, dict):
                continue
            if not _match_corpus(hit_source, aliases):
                continue

            ref = str(hit_source.get("ref") or "").strip()
            if not ref or ref in seen_refs:
                continue

            seen_refs.add(ref)
            he_ref = str(hit_source.get("heRef") or "").strip()
            path = str(hit_source.get("path") or "").strip()
            snippet = _extract_hit_snippet(hit_source)

            sources.append({
                "ref": ref,
                "title": ref,
                "lines": [{"en": snippet or f"Matched via {corpus}", "he": he_ref}],
                "domain": "Sefaria",
                "corpus": corpus,
                "path": path,
                "priority": 1,
                "status": "fallback",
                "score": hit.get("_score") if isinstance(hit, dict) else None,
            })

            added_for_corpus += 1
            if added_for_corpus >= 3:
                break

    local_matches = _find_local_custom_matches(keywords, max_results=10)
    for match in local_matches:
        field = match.get("field", "Title")
        value = str(match.get("value") or "").strip()
        file_name = match.get("file", "")
        pointer = match.get("pointer", "")
        sources.append({
            "ref": f"Local Custom: {value[:80]}",
            "title": value[:120] if value else "Local Custom Match",
            "lines": [{
                "en": f"{field}: {value}",
                "he": "",
            }],
            "domain": "local-customs",
            "corpus": "customs-json",
            "file": file_name,
            "pointer": pointer,
            "priority": 2,
            "status": "fallback",
            "match_keywords": match.get("match_keywords", []),
        })

    if not sources:
        sources.append({
            "ref": "No verified source found",
            "title": "No verified source found",
            "lines": [{"en": "No verified source found", "he": ""}],
            "domain": "none",
            "priority": 1,
            "status": "fallback",
        })

    return {
        "status": "fallback",
        "query": question,
        "keywords": keywords,
        "source_count": len(sources),
        "sources": sources,
    }


def _strip_hebrew_diacritics(text):
    return HEBREW_DIACRITICS_RE.sub("", str(text or ""))


def _contains_hebrew_letters(text):
    return bool(HEBREW_LETTER_RE.search(str(text or "")))


def _normalize_lookup_word(text):
    cleaned = _strip_hebrew_diacritics(text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _decode_route_ref(value, max_rounds=3):
    """Decode refs that may arrive pre-encoded or double-encoded from clients/proxies."""
    decoded = str(value or "").strip()
    for _ in range(max_rounds):
        next_value = unquote(decoded).strip()
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _translate_hebrew_text_online(text):
    """Best-effort public translation fallback for short Hebrew snippets."""
    value = str(text or "").strip()
    if not value:
        return ""
    if not _contains_hebrew_letters(value):
        return ""

    url = "https://api.mymemory.translated.net/get"
    try:
        resp = requests.get(
            url,
            params={"q": value, "langpair": "he|en"},
            timeout=2.5,
        )
        if not resp.ok:
            return ""
        payload = resp.json() if resp.content else {}
        translated = str((payload.get("responseData") or {}).get(
            "translatedText") or "").strip()
        if not translated:
            return ""

        # Skip unchanged echoes.
        if translated.lower() == value.lower():
            return ""
        return translated
    except Exception:
        return ""


def _fill_missing_english_lines(text_payload, max_lines=12, max_runtime_seconds=2.5):
    """Fill missing English lines when Hebrew is available and translation can be generated."""
    if not isinstance(text_payload, dict):
        return text_payload

    lines = text_payload.get("lines", [])
    if not isinstance(lines, list) or not lines:
        return text_payload

    translated_count = 0
    started_at = time.time()
    for line in lines:
        if translated_count >= max_lines:
            break
        if time.time() - started_at > max_runtime_seconds:
            break
        if not isinstance(line, dict):
            continue

        en_value = str(line.get("en") or "").strip()
        he_value = _normalize_lookup_word(line.get("he") or "")
        if en_value or not he_value:
            continue
        if not _contains_hebrew_letters(he_value):
            continue

        cache_key = he_value[:320]
        if cache_key in TRANSLATION_CACHE:
            generated = TRANSLATION_CACHE[cache_key]
        else:
            generated = _translate_hebrew_text_online(cache_key)
            TRANSLATION_CACHE[cache_key] = generated

        if generated:
            line["en"] = generated
            translated_count += 1

    if translated_count:
        text_payload["translation_generated"] = True
        text_payload["translation_generated_count"] = translated_count
        text_payload["translation_note"] = "Automatic English translation added for missing lines."
        text_payload["en"] = [
            str(line.get("en", "")).strip()
            for line in lines
            if isinstance(line, dict) and str(line.get("en", "")).strip()
        ]

    return text_payload


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


def _lookup_english_word_meaning(word):
    clean_word = str(word or "").strip().lower()
    if not clean_word:
        return "", ""

    try:
        resp = requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{clean_word}",
            timeout=5,
        )
        if not resp.ok:
            return "", ""
        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            return "", ""

        entry = payload[0] if isinstance(payload[0], dict) else {}
        meanings = entry.get("meanings", []) if isinstance(
            entry.get("meanings"), list) else []
        for meaning in meanings:
            definitions = meaning.get(
                "definitions", []) if isinstance(meaning, dict) else []
            for definition in definitions:
                text = str((definition or {}).get("definition") or "").strip()
                if text:
                    return text, "dictionaryapi.dev"
    except Exception:
        return "", ""

    return "", ""


def _lookup_hebrew_word_meaning(word):
    clean_word = _normalize_lookup_word(word)
    if not clean_word:
        return "", ""

    exact = HEBREW_WORD_GLOSSARY.get(clean_word)
    if exact:
        return exact, "local-hebrew-glossary"

    base = re.sub(r"[^\u05D0-\u05EA\s]", "", clean_word).strip()
    if base in HEBREW_WORD_GLOSSARY:
        return HEBREW_WORD_GLOSSARY[base], "local-hebrew-glossary"

    generated = _translate_hebrew_text_online(clean_word)
    if generated:
        return generated, "automatic-translation"

    return "", ""


def _sanitize_answer_mode(mode_value):
    mode = (mode_value or "balanced").strip().lower()
    return mode if mode in ANSWER_MODES else "balanced"


def _canonicalize_community_name(name):
    if not name:
        return None

    if name in COMMUNITIES:
        return name

    lowered = name.strip().lower()
    if lowered in COMMUNITY_ALIASES:
        return COMMUNITY_ALIASES[lowered]

    normalized = "".join(ch for ch in lowered if ch.isalnum())
    for alias, canonical in COMMUNITY_ALIASES.items():
        alias_norm = "".join(ch for ch in alias.lower() if ch.isalnum())
        if alias_norm == normalized:
            return canonical

    for canonical in COMMUNITIES.keys():
        canonical_norm = "".join(
            ch for ch in canonical.lower() if ch.isalnum())
        if canonical_norm == normalized:
            return canonical

    return None


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


def _holiday_emoji_for_event(title, category=""):
    lowered = str(title or "").strip().lower()
    cat = str(category or "").strip().lower()

    if "yom ha'atzmaut" in lowered or "yom haatzmaut" in lowered:
        return "🇮🇱"
    if "hanukkah" in lowered or "chanukah" in lowered:
        return "🕎"
    if "erev rosh hashana" in lowered or "rosh hashana" in lowered:
        return "🍎🍯"
    if "lag ba'omer" in lowered or "lag baomer" in lowered:
        return "🔥"
    if "yom yerushalayim" in lowered:
        return "🇮🇱"
    if "erev shavuot" in lowered:
        return "⛰️"
    if "shavuot" in lowered:
        return "🌸"
    if any(token in lowered for token in ("sukkot", "succot", "sukkos", "succos")):
        return "🍋🌿"
    if "rosh chodesh" in lowered or cat == "roshchodesh":
        return "🌙"
    if cat == "fast" or any(token in lowered for token in (
        "taanis", "taanit", "fast", "tzom", "tisha b'av", "17 of tamuz", "gedaliah", "esther"
    )):
        return "✡️"
    if "shabbat" in lowered or cat in {"shabbat", "parashat"}:
        return "🕍"
    if cat in {"modern"}:
        return "✡️"
    if cat in {"major", "minor", "holiday", "special"}:
        return "✡️"
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
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

CLERK_PUBLISHABLE_KEY = (
    os.environ.get("CLERK_PUBLISHABLE_KEY")
    or os.environ.get("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
    or ""
).strip()
CLERK_JWT_ISSUER = (os.environ.get("CLERK_JWT_ISSUER")
                    or "").strip().rstrip("/")
CLERK_AUDIENCE = (os.environ.get("CLERK_AUDIENCE") or "").strip()
CLERK_ENFORCE_AUTH = (os.environ.get("CLERK_ENFORCE_AUTH")
                      or "false").strip().lower() == "true"
_clerk_jwks_client = None

SUPABASE_URL = (os.environ.get("SUPABASE_URL") or "").strip()
if not SUPABASE_URL:
    SUPABASE_URL = (os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "").strip()

SUPABASE_PUBLISHABLE_KEY = (
    os.environ.get("SUPABASE_ANON_KEY")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY")
    or os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY")
    or ""
).strip()

SUPABASE_SERVICE_ROLE_KEY = (os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY") or "").strip()
SUPABASE_PREFS_TABLE = (os.environ.get(
    "SUPABASE_PREFS_TABLE") or "user_preferences").strip()
_supabase_client = None


def _extract_bearer_token():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    return token or None


def _get_clerk_jwks_client():
    global _clerk_jwks_client
    if not CLERK_JWT_ISSUER:
        return None
    if _clerk_jwks_client is None:
        jwks_url = f"{CLERK_JWT_ISSUER}/.well-known/jwks.json"
        _clerk_jwks_client = jwt.PyJWKClient(jwks_url)
    return _clerk_jwks_client


def _verify_clerk_token(token):
    if not token:
        raise ValueError("Missing bearer token")
    if not CLERK_JWT_ISSUER:
        raise ValueError("Server missing CLERK_JWT_ISSUER")

    jwks_client = _get_clerk_jwks_client()
    if jwks_client is None:
        raise ValueError("Clerk JWKS client unavailable")

    signing_key = jwks_client.get_signing_key_from_jwt(token).key
    decode_kwargs = {
        "algorithms": ["RS256"],
        "issuer": CLERK_JWT_ISSUER,
    }
    if CLERK_AUDIENCE:
        decode_kwargs["audience"] = CLERK_AUDIENCE
    else:
        decode_kwargs["options"] = {"verify_aud": False}

    return jwt.decode(token, signing_key, **decode_kwargs)


def _get_supabase_client():
    global _supabase_client
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        return None
    if _supabase_client is None:
        _supabase_client = create_client(
            SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _supabase_client


def _looks_like_jwt(value):
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 3 and all(parts)


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
        for item in parsed:
            if isinstance(item, str) and _looks_like_jwt(item):
                return item
            if isinstance(item, dict):
                token = item.get("access_token") or item.get("accessToken")
                if isinstance(token, str) and token:
                    return token

    return None


def _extract_supabase_access_token():
    # Prefer Authorization header so API clients can override cookie auth.
    bearer = _extract_bearer_token()
    if bearer:
        return bearer

    direct_cookie_names = [
        "sb-access-token",
        "supabase-access-token",
    ]
    for cookie_name in direct_cookie_names:
        direct_value = request.cookies.get(cookie_name)
        token = _extract_supabase_token_from_cookie_value(direct_value)
        if token:
            return token

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

    for cookie_value in session_cookie_values:
        token = _extract_supabase_token_from_cookie_value(cookie_value)
        if token:
            return token

    for _, chunk_parts in chunked_cookies.items():
        sorted_parts = sorted(chunk_parts, key=lambda part: part[0])
        joined_value = "".join(part[1] for part in sorted_parts)
        token = _extract_supabase_token_from_cookie_value(joined_value)
        if token:
            return token

    return None


def _get_request_supabase_client():
    """Flask equivalent of Next.js createServerClient for request-scoped reads."""
    if create_client is None:
        return None
    if not SUPABASE_URL or not SUPABASE_PUBLISHABLE_KEY:
        return None

    access_token = _extract_supabase_access_token()
    if not access_token or SyncClientOptions is None:
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)

    auth_headers = {"Authorization": f"Bearer {access_token}"}
    try:
        options = SyncClientOptions(headers=auth_headers)
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY, options=options)
    except TypeError:
        # Compatibility fallback for older supabase-py signatures.
        return create_client(SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY)


def maybe_require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            if CLERK_ENFORCE_AUTH:
                return jsonify({"error": "Authentication required"}), 401
            return route_fn(*args, **kwargs)

        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401

        return route_fn(*args, **kwargs)

    return wrapped


def require_clerk_auth(route_fn):
    @wraps(route_fn)
    def wrapped(*args, **kwargs):
        token = _extract_bearer_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401

        try:
            g.clerk_claims = _verify_clerk_token(token)
        except Exception:
            return jsonify({"error": "Invalid or expired Clerk token"}), 401

        return route_fn(*args, **kwargs)

    return wrapped


def _get_prayer_refs(prayer_name):
    """Resolve prayer/service name to a list of Sefaria refs."""
    resolved_name = (unquote(prayer_name or "") or "").strip()
    if resolved_name in SIDDUR_SECTION_MAP:
        return SIDDUR_SECTION_MAP[resolved_name]

    from sefaria_library import get_index_leaf_refs
    return get_index_leaf_refs(resolved_name, max_refs=80)


def _coerce_coordinate(value, min_value, max_value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if numeric < min_value or numeric > max_value:
        return None
    return numeric


def _coerce_int(value, default, min_value=1, max_value=100):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(parsed, max_value))


# Browser cache/session cadence defaults to 2 days so clients revalidate often.
RESOURCE_RELOAD_SECONDS = _coerce_int(
    os.environ.get("RESOURCE_RELOAD_SECONDS"),
    default=60 * 60 * 24 * 2,
    min_value=60 * 60,
    max_value=60 * 60 * 24 * 14,
)
SESSION_RELOAD_SECONDS = _coerce_int(
    os.environ.get("SESSION_RELOAD_SECONDS"),
    default=RESOURCE_RELOAD_SECONDS,
    min_value=60 * 60,
    max_value=60 * 60 * 24 * 30,
)
STATIC_STALE_WHILE_REVALIDATE_SECONDS = max(
    60 * 60,
    min(60 * 60 * 24, RESOURCE_RELOAD_SECONDS // 2),
)

is_production_runtime = (
    os.environ.get("VERCEL") == "1"
    or os.environ.get("FLASK_ENV", "").strip().lower() == "production"
)

app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(
    seconds=SESSION_RELOAD_SECONDS)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = is_production_runtime
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = RESOURCE_RELOAD_SECONDS


@app.before_request
def apply_session_cookie_policy():
    # Ensure Flask issues an expiring cookie instead of a browser-session cookie.
    session.permanent = True


@app.after_request
def apply_response_cache_policy(response):
    path = request.path or ""

    if path.startswith("/api/") or path in {"/ask", "/set_location"}:
        response.headers["Cache-Control"] = "no-store"
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

    if response.mimetype in {"text/html", "application/xhtml+xml"}:
        response.headers["Cache-Control"] = (
            f"public, max-age={RESOURCE_RELOAD_SECONDS}, must-revalidate"
        )

    return response


def _parse_multi_value_arg(name):
    raw = (request.args.get(name, "") or "").strip()
    if not raw:
        return []
    parts = []
    for chunk in raw.split(","):
        value = chunk.strip()
        if value:
            parts.append(value)
    return parts


def _extract_search_metadata_filters():
    metadata_filters = {}
    for key in ("era", "author", "category", "geography", "nusach"):
        values = _parse_multi_value_arg(key)
        if values:
            metadata_filters[key] = values
    return metadata_filters


def _extract_client_ip():
    forwarded_for = (request.headers.get("X-Forwarded-For")
                     or "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for

    real_ip = (request.headers.get("X-Real-IP") or "").strip()
    if real_ip:
        return real_ip

    remote_ip = (request.remote_addr or "").strip()
    return remote_ip or None


def get_engine():
    # Instantiate engine using session location or IP fallback
    lat = _coerce_coordinate(session.get('lat'), -90, 90)
    lon = _coerce_coordinate(session.get('lon'), -180, 180)

    if lat is None or lon is None:
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
                    lat = ip_lat
                    lon = ip_lon
                    session['lat'] = lat
                    session['lon'] = lon
                    break
        except Exception:
            pass

    if lat is None or lon is None:
        lat, lon = (40.7128, -74.0060)

    return ShelahEngine(lat=lat, lon=lon)


@app.route("/")
def index():
    engine = get_engine()
    daily_study = engine.get_daily_learning()

    # We no longer need hebcal learning as per new architecture, relying on Sefaria cal
    return render_template(
        "index.html",
        daily=daily_study,
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@app.route('/set_location', methods=['POST'])
def set_location():
    data = request.get_json(silent=True) or {}
    lat = _coerce_coordinate(data.get('lat'), -90, 90)
    lon = _coerce_coordinate(data.get('lon'), -180, 180)
    if lat is None or lon is None:
        return jsonify({"error": "Invalid coordinates"}), 400

    session['lat'] = lat
    session['lon'] = lon
    return jsonify({"status": "success", "lat": lat, "lon": lon})


@app.route('/api/zmanim')
def get_zmanim_api():
    community = request.args.get('community', 'standard')
    lat = _coerce_coordinate(request.args.get('lat'), -90, 90)
    lon = _coerce_coordinate(request.args.get('lon'), -180, 180)

    if lat is not None and lon is not None:
        session['lat'] = lat
        session['lon'] = lon
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        engine = get_engine()

    times = engine.get_zmanim(community)
    return jsonify(times)


@app.route('/api/zmanim/month')
def get_zmanim_month():
    lat = _coerce_coordinate(request.args.get('lat'), -90, 90)
    lon = _coerce_coordinate(request.args.get('lon'), -180, 180)

    if lat is not None and lon is not None:
        session['lat'] = lat
        session['lon'] = lon
        engine = ShelahEngine(lat=lat, lon=lon)
    else:
        engine = get_engine()

    events = engine.get_monthly_zmanim()
    return jsonify(events)


@app.route("/manifest.webmanifest")
def web_manifest():
    return send_from_directory("static", "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "favicon.svg", mimetype="image/svg+xml")


@app.route("/service-worker.js")
def service_worker():
    response = send_from_directory(
        "static", "service-worker.js", mimetype="application/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/ask", methods=["POST"])
@maybe_require_clerk_auth
def ask_question():
    data = request.json or {}
    question = data.get("question", "")

    if not question:
        return jsonify({"error": "No question provided"}), 400
    mode = _sanitize_answer_mode(data.get("mode"))
    community_lens = (data.get("community") or "All").strip()
    canonical_lens = "All" if community_lens.lower() == "all" else (
        _canonicalize_community_name(community_lens) or community_lens)

    try:
        engine = get_engine()

        # Check for direct prayer-service questions
        if any(prayer in question for prayer in ["Shacharit", "Mincha", "Maariv", "Kiddush", "Havdalah"]):
            # Return a prayer service focused response
            DEVTOOLS_STATS["answers_total"] += 1
            return jsonify({
                "answer": f"Prayer Service Guide\n\n{question}\n\nYou can browse full liturgy books and services from the prayer sections. For practical application, compare local community custom with your rabbi's guidance.",
                "confidence": 0.85,
                "sources": [{
                    "ref": "Sefaria Liturgy",
                    "title": "Sefaria Prayer Books",
                    "lines": [{"en": f"Prayer Service: {question}", "he": ""}]
                }],
                "customs": [],
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 1,
                    "custom_count": 0,
                    "generated_at": int(time.time()),
                    "fallback": False,
                }
            })

        # Check for Merkava/Community Customs requests
        detected_community = _detect_community_in_text(question)
        if detected_community or "customs" in question.lower() or "minhag" in question.lower():
            community = detected_community or (
                _canonicalize_community_name(community_lens) or "Ashkenaz")
            customs_query = question
            if canonical_lens != "All":
                customs_query = f"{question} {canonical_lens}"
            customs_info = engine.get_customs(customs_query)
            DEVTOOLS_STATS["answers_total"] += 1
            return jsonify({
                "answer": f"Community Customs ({community})\n\n{question}\n\nJewish communities from different diaspora regions developed distinct customs and practices while maintaining core halakhic principles. These traditions reflect the unique historical, cultural, and environmental contexts of each community.",
                "confidence": 0.8,
                "sources": [{
                    "ref": f"Merkava - {community} Customs",
                    "title": f"{community} Community Customs",
                    "lines": [{"en": f"Community: {community}", "he": ""}]
                }],
                "customs": customs_info,
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 1,
                    "custom_count": len(customs_info),
                    "generated_at": int(time.time()),
                    "fallback": False,
                }
            })

        # 1. Fetch Sefaria Refs - Standard halakhic questions
        primary_refs = sefaria.find_refs_for_question(question)
        primary_sources = []
        for ref in primary_refs:
            # ref is a string, not a dict - get library text using the ref directly
            source_data = engine.get_library_text(ref)
            primary_sources.append(source_data)

        # 2. Fetch Halachipedia
        halachipedia_info = engine.get_halachipedia_summary(question)
        halachipedia_list = [halachipedia_info] if halachipedia_info else []

        # 3. Fetch Customs
        customs_query = question
        if canonical_lens != "All":
            customs_query = f"{question} {canonical_lens}"
        customs_info = engine.get_customs(customs_query)

        # 4. Fetch Wikipedia
        wiki_info = engine.get_wiki(question)
        wiki_list = [wiki_info] if wiki_info else []

        # 5. Build Claude Prompt
        # Passing primary_sources directly. We'll let claude.py format them.
        # We need to flat map the sources so Claude can easily read them in plain text, but the UI keeps the separated ones.
        flat_sources_for_claude = []
        for src in primary_sources:
            en_lines = [l['en'] for l in src['lines'] if l['en']]
            flat_sources_for_claude.append({
                'ref': src['ref'],
                'text': ' '.join(en_lines)
            })

        if mode == "strict" and not flat_sources_for_claude:
            DEVTOOLS_STATS["answers_total"] += 1
            DEVTOOLS_STATS["strict_blocks"] += 1
            DEVTOOLS_STATS["fallback_answers"] += 1
            return jsonify({
                "answer": (
                    "Strict Sources Mode could not complete this request because no primary Sefaria sources "
                    "were matched with sufficient confidence. Please refine the question with a text reference."
                ),
                "confidence": 0.2,
                "wiki": wiki_list + halachipedia_list,
                "customs": customs_info,
                "sources": primary_sources,
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": 0,
                    "custom_count": len(customs_info),
                    "generated_at": int(time.time()),
                    "fallback": True,
                    "strict_blocked": True,
                }
            })

        prompt = claude.build_prompt(
            question=question,
            sefaria_sources=flat_sources_for_claude,
            customs=customs_info,
            wiki=wiki_list,
            halachipedia=halachipedia_list,
            mode=mode,
            community_lens=canonical_lens,
        )

        try:
            result = claude.ask_claude(prompt)
            if result.get("error"):
                raise RuntimeError(result.get("error") or "AI request failed")
        except Exception as ai_error:
            fallback_payload = get_halakhic_sources(question)
            DEVTOOLS_STATS["answers_total"] += 1
            DEVTOOLS_STATS["fallback_answers"] += 1
            return jsonify({
                "answer": "AI synthesis unavailable. Returning verified halakhic sources.",
                "confidence": 0.4,
                "wiki": wiki_list + halachipedia_list,
                "customs": customs_info,
                "sources": fallback_payload.get("sources", []),
                "meta": {
                    "mode": mode,
                    "community_lens": canonical_lens,
                    "source_count": fallback_payload.get("source_count", 0),
                    "custom_count": len(customs_info),
                    "generated_at": int(time.time()),
                    "fallback": True,
                    "status": "fallback",
                    "fallback_detail": {
                        "keywords": fallback_payload.get("keywords", []),
                        "reason": str(ai_error),
                    },
                }
            })

        DEVTOOLS_STATS["answers_total"] += 1

        # Send all context back to the frontend
        return jsonify({
            "answer": result.get("answer"),
            "confidence": result.get("confidence"),
            "wiki": wiki_list + halachipedia_list,
            "customs": customs_info,
            "sources": primary_sources,
            "meta": {
                "mode": mode,
                "community_lens": canonical_lens,
                "source_count": len(primary_sources),
                "custom_count": len(customs_info),
                "generated_at": int(time.time()),
                "fallback": False,
            }
        })

    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500


@app.route("/api/stack/health")
def stack_health():
    """Return runtime readiness for Bento stack components."""
    supabase_ready = bool(_get_supabase_client())
    return jsonify({
        "flask": True,
        "vercel": True,
        "clerk": {
            "configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
            "enforced": CLERK_ENFORCE_AUTH,
        },
        "supabase": {
            "configured": bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY),
            "publishable_configured": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
            "ready": supabase_ready,
            "prefs_table": SUPABASE_PREFS_TABLE,
        },
        "calendar": {
            "pyluach": True,
            "zmanim": True,
        },
        "reliability": DEVTOOLS_STATS,
    })


@app.route("/api/devtools/heartbeat")
def devtools_heartbeat():
    """Low-noise diagnostics endpoint for inspector/devtools mode."""
    started = time.time()

    checks = {
        "clerk_configured": bool(CLERK_PUBLISHABLE_KEY and CLERK_JWT_ISSUER),
        "supabase_service_ready": bool(_get_supabase_client()),
        "supabase_publishable_ready": bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY),
    }

    from sefaria_library import get_popular_texts
    popular_started = time.time()
    popular = get_popular_texts()
    checks["library_popular_ready"] = bool(popular)
    checks["library_popular_ms"] = int((time.time() - popular_started) * 1000)

    return jsonify({
        "ok": all(v for k, v in checks.items() if not k.endswith("_ms")),
        "ts": int(time.time()),
        "elapsed_ms": int((time.time() - started) * 1000),
        "checks": checks,
        "stats": DEVTOOLS_STATS,
    })


@app.route("/api/devtools/reliability")
def devtools_reliability():
    return jsonify({
        "stats": DEVTOOLS_STATS,
        "ts": int(time.time()),
    })


@app.route("/api/devtools/segment-report", methods=["POST"])
@maybe_require_clerk_auth
def report_segment_issue():
    payload = request.get_json(silent=True) or {}
    report = {
        "ts": int(time.time()),
        "kind": (payload.get("kind") or "segment").strip()[:60],
        "message": (payload.get("message") or "").strip()[:2000],
        "segment": (payload.get("segment") or "").strip()[:160],
        "ref": (payload.get("ref") or "").strip()[:200],
        "view_type": (payload.get("view_type") or "").strip()[:40],
        "view_value": (payload.get("view_value") or "").strip()[:200],
        "client": {
            "ua": (request.headers.get("User-Agent") or "")[:300],
            "ip": _extract_client_ip() or "",
        },
    }
    claims = getattr(g, "clerk_claims", {}) or {}
    if claims.get("sub"):
        report["user_id"] = claims.get("sub")

    app.logger.warning("SEGMENT_REPORT %s",
                       json.dumps(report, ensure_ascii=True))
    DEVTOOLS_STATS["segment_reports"] += 1
    return jsonify({"ok": True, "logged": True})


@app.route("/api/auth/me")
def clerk_auth_me():
    """Returns Clerk auth status and a minimal user payload."""
    token = _extract_bearer_token()
    if not token:
        return jsonify({"authenticated": False})

    try:
        claims = _verify_clerk_token(token)
        return jsonify({
            "authenticated": True,
            "user_id": claims.get("sub"),
            "session_id": claims.get("sid"),
        })
    except Exception:
        return jsonify({"authenticated": False}), 401


@app.route("/api/user/preferences", methods=["GET", "PUT"])
@require_clerk_auth
def user_preferences():
    """Persist and fetch per-user UI preferences from Supabase."""
    claims = getattr(g, "clerk_claims", {}) or {}
    user_id = claims.get("sub")
    if not user_id:
        return jsonify({"error": "Missing user identity"}), 401

    supabase = _get_supabase_client()
    if not supabase:
        return jsonify({"error": "Supabase not configured"}), 503

    table = supabase.table(SUPABASE_PREFS_TABLE)

    try:
        if request.method == "GET":
            result = table.select("prefs,updated_at").eq(
                "user_id", user_id).limit(1).execute()
            rows = result.data or []
            if not rows:
                return jsonify({
                    "prefs": None,
                    "shelf": None,
                    "notes": None,
                    "reading_state": None,
                    "updated_at": None,
                })

            record = rows[0]
            if not isinstance(record, dict):
                return jsonify({
                    "prefs": None,
                    "shelf": None,
                    "notes": None,
                    "reading_state": None,
                    "updated_at": None,
                })

            stored = record.get("prefs")
            prefs = None
            shelf = None
            notes = None
            reading_state = None
            if isinstance(stored, dict):
                if any(key in stored for key in ("prefs", "shelf", "notes", "reading_state")):
                    prefs = stored.get("prefs") if isinstance(
                        stored.get("prefs"), dict) else None
                    shelf = stored.get("shelf") if isinstance(
                        stored.get("shelf"), dict) else None
                    notes = stored.get("notes") if isinstance(
                        stored.get("notes"), dict) else None
                    reading_state = stored.get("reading_state") if isinstance(
                        stored.get("reading_state"), dict) else None
                else:
                    # Legacy shape where prefs JSON was stored directly.
                    prefs = stored

            return jsonify({
                "prefs": prefs,
                "shelf": shelf,
                "notes": notes,
                "reading_state": reading_state,
                "updated_at": record.get("updated_at"),
            })

        payload = request.get_json(silent=True) or {}
        prefs = payload.get("prefs")
        if not isinstance(prefs, dict):
            return jsonify({"error": "prefs must be an object"}), 400

        shelf = payload.get("shelf")
        notes = payload.get("notes")
        reading_state = payload.get("reading_state")

        if shelf is None:
            shelf = {}
        if notes is None:
            notes = {}
        if reading_state is None:
            reading_state = {}

        if not isinstance(shelf, dict):
            return jsonify({"error": "shelf must be an object"}), 400
        if not isinstance(notes, dict):
            return jsonify({"error": "notes must be an object"}), 400
        if not isinstance(reading_state, dict):
            return jsonify({"error": "reading_state must be an object"}), 400

        now_iso = datetime.utcnow().isoformat() + "Z"
        stored_payload = {
            "prefs": prefs,
            "shelf": shelf,
            "notes": notes,
            "reading_state": reading_state,
        }
        upsert_payload = {
            "user_id": user_id,
            "prefs": stored_payload,
            "updated_at": now_iso,
        }
        table.upsert(upsert_payload, on_conflict="user_id").execute()
        return jsonify({"ok": True, "updated_at": now_iso})
    except Exception as e:
        return jsonify({"error": f"Supabase operation failed: {str(e)}"}), 500


@app.route("/api/todos")
def list_todos():
    """Flask equivalent of the Next.js server query for todos."""
    supabase = _get_request_supabase_client()
    if not supabase:
        return jsonify({
            "error": "Supabase publishable client is not configured",
            "hint": "Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY",
        }), 503

    try:
        result = supabase.from_("todos").select("id,name").execute()
        return jsonify({"todos": result.data or []})
    except Exception as e:
        message = str(e)
        if "PGRST205" in message or "Could not find the table 'public.todos'" in message:
            # Treat a missing optional table as an empty list so the UI keeps working.
            return jsonify({"todos": [], "warning": "Supabase todos table is not configured"})
        return jsonify({"error": f"Failed to load todos: {message}"}), 500


@app.route("/api/library/index")
def library_index():
    """Returns report-adjusted Sefaria library tree (non-loading removals pruned, fix refs applied)."""
    from sefaria_library import get_library_index
    data = get_library_index()
    return jsonify(data)


@app.route("/api/library/popular")
def library_popular():
    """Returns curated popular texts per category."""
    from sefaria_library import get_popular_texts
    return jsonify(get_popular_texts())


@app.route("/api/text/<path:ref>")
def get_text_inline(ref):
    """Fetches a Sefaria text inline — Hebrew + English + metadata."""
    from sefaria_library import get_text
    decoded_ref = _decode_route_ref(ref)
    data = get_text(decoded_ref)

    should_translate = str(request.args.get("autotranslate", "1")).strip().lower() not in {
        "0", "false", "no", "off"
    }
    if should_translate and isinstance(data, dict) and not data.get("error"):
        data = _fill_missing_english_lines(data)

    return jsonify(data)


@app.route("/api/word/meaning")
def get_word_meaning():
    """Look up a highlighted word meaning (best-effort for Hebrew and English)."""
    raw_word = str(request.args.get("word", "") or "").strip()
    if not raw_word:
        return jsonify({"error": "Missing word parameter"}), 400

    if _contains_hebrew_letters(raw_word):
        meaning, source = _lookup_hebrew_word_meaning(raw_word)
    else:
        meaning, source = _lookup_english_word_meaning(raw_word)

    if not meaning:
        return jsonify({
            "word": raw_word,
            "meaning": "",
            "source": "",
            "status": "not_found",
        }), 404

    return jsonify({
        "word": raw_word,
        "meaning": meaning,
        "source": source,
        "status": "ok",
    })


@app.route("/api/library/search")
def library_search():
    """Full-text search across Sefaria texts with report-based removal/fix filtering."""
    from sefaria_library import search_library
    query = request.args.get("q", "")
    size = _coerce_int(request.args.get("size"), 10, min_value=1, max_value=50)
    metadata_filters = _extract_search_metadata_filters()
    if not query:
        return jsonify([])
    results = search_library(
        query, size=size, metadata_filters=metadata_filters)
    return jsonify(results)


@app.route("/api/search/suggest")
def search_suggest():
    """Omnibox suggestions: texts, prayers, communities, and AI query option."""
    from sefaria_library import search_library, get_liturgy_books

    query = (request.args.get("q", "") or "").strip()
    size = _coerce_int(request.args.get("size"), 8, min_value=1, max_value=20)
    metadata_filters = _extract_search_metadata_filters()
    if not query:
        return jsonify([])

    q_lower = query.lower()
    suggestions = []
    seen = set()

    def add_item(item_type, label, value, subtitle="", score=0, label_he="", subtitle_he=""):
        key = (item_type, (value or "").lower())
        if key in seen:
            return
        seen.add(key)
        suggestions.append({
            "type": item_type,
            "label": label,
            "label_he": label_he,
            "value": value,
            "subtitle": subtitle,
            "subtitle_he": subtitle_he,
            "score": score,
        })

    alias_ref = QUICK_TEXT_ALIASES.get(q_lower)
    if alias_ref:
        add_item("text", alias_ref, alias_ref, "Popular Torah alias", 100)

    for community in COMMUNITIES.keys():
        if q_lower in community.lower():
            add_item("community", community, community,
                     "Community customs", 90)

    for alias, canonical in COMMUNITY_ALIASES.items():
        if q_lower in alias and canonical in COMMUNITIES:
            add_item("community", canonical, canonical,
                     f"Community customs (matched '{alias}')", 88)

    for book in get_liturgy_books(max_items=120):
        title = book.get("title", "")
        if title and q_lower in title.lower():
            add_item("prayer", title, title, "Sefaria liturgy", 85)

    for hit in search_library(query, size=size, metadata_filters=metadata_filters):
        ref = hit.get("ref", "")
        he_ref = (hit.get("heRef", "") or "").strip()
        categories = " > ".join(hit.get("categories", [])[:3])
        if ref:
            add_item(
                "text",
                ref,
                ref,
                categories or "Sefaria text",
                70,
                label_he=he_ref or ref,
            )

    add_item("ask", f"Ask Sh'elah: {query}", query,
             "AI synthesis", 40)

    suggestions.sort(key=lambda x: x.get("score", 0), reverse=True)
    return jsonify(suggestions[:size])


@app.route("/api/text/<path:ref>/links")
def get_text_links(ref):
    """Returns all linked commentaries & parallel texts for a given ref."""
    from sefaria_library import get_linked_texts
    decoded_ref = _decode_route_ref(ref)
    return jsonify(get_linked_texts(decoded_ref))


@app.route("/api/text/<path:ref>/graph")
def get_text_graph(ref):
    """Build a lightweight source graph around a text reference."""
    from sefaria_library import get_linked_texts

    decoded_ref = _decode_route_ref(ref)
    links = get_linked_texts(decoded_ref)
    nodes = [{"id": decoded_ref, "label": decoded_ref, "kind": "root"}]
    edges = []
    seen = {decoded_ref}

    for category, items in (links or {}).items():
        for item in (items or [])[:14]:
            target = (item or {}).get("ref", "")
            if not target:
                continue
            if target not in seen:
                seen.add(target)
                nodes.append({
                    "id": target,
                    "label": target,
                    "kind": "linked",
                    "category": category,
                })
            edges.append({
                "source": decoded_ref,
                "target": target,
                "label": category,
            })

    return jsonify({
        "ref": ref,
        "nodes": nodes,
        "edges": edges,
    })


@app.route("/api/library/category/<path:category>")
def library_category(category):
    """Returns all books in a given Sefaria category."""
    from sefaria_library import get_category_contents
    return jsonify(get_category_contents(category))


# ─── PRAYER BOOK API (Siddur Sefard - Sefardic/Mediterranean Siddur) ──────────
# Prayer content is fetched live from Sefaria refs listed in SIDDUR_SECTION_MAP.


@app.route("/api/prayers/list")
def get_prayers_list():
    """Returns all prayer books from Sefaria Liturgy plus legacy quick services."""
    from sefaria_library import get_liturgy_books

    items = []
    seen = set()

    for name in SIDDUR_SECTION_MAP.keys():
        items.append({"name": name, "title": name, "source": "legacy-service"})
        seen.add(name)

    for book in get_liturgy_books(max_items=200):
        title = book.get("title")
        if title and title not in seen:
            items.append({"name": title, "title": title,
                         "source": "sefaria-liturgy"})
            seen.add(title)

    return jsonify(items)


@app.route("/api/prayer/<name>")
def get_prayer(name):
    """Returns prayer-book preview content in English and Hebrew."""
    from sefaria_library import get_text

    resolved_name = (unquote(name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"Prayer '{resolved_name}' not found"}), 404

    preview = None
    for ref in refs[:12]:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            preview = data
            break

    if not preview:
        return jsonify({"error": f"Could not load prayer '{resolved_name}' from Sefaria"}), 404

    en_preview = "\n".join([l.get("en", "") for l in preview.get(
        "lines", []) if l.get("en")][:8]).strip()
    he_preview = "\n".join([l.get("he", "") for l in preview.get(
        "lines", []) if l.get("he")][:8]).strip()
    if not en_preview:
        en_preview = f"Preview available in Hebrew for {resolved_name}."
    if not he_preview:
        he_preview = f"תצוגה מקדימה זמינה באנגלית עבור {resolved_name}."

    prayer_data = {
        "en": en_preview,
        "he": he_preview,
    }

    return jsonify({
        "name": resolved_name,
        "title": resolved_name,
        "content": prayer_data,
        "languages": ["en", "he"]
    })


@app.route("/api/siddur/full/<path:prayer_name>")
def get_siddur_full(prayer_name):
    """Fetch full prayer text from Sefaria for any supported prayer service/book."""
    from sefaria_library import get_text

    resolved_name = (unquote(prayer_name or "") or "").strip()
    refs = _get_prayer_refs(resolved_name)
    if not refs:
        return jsonify({"error": f"No Sefaria mapping for '{resolved_name}'"}), 404

    combined_lines = []
    for ref in refs:
        data = get_text(ref)
        if "error" not in data and (data.get("he") or data.get("en")):
            section_title = ref.split(", ")[-1] if ", " in ref else ref
            he_title = data.get("heTitle", section_title)
            combined_lines.append({
                "he": f"<strong class='text-navy'>{he_title}</strong>",
                "en": f"<strong class='text-navy'>{section_title}</strong>",
                "type": "header"
            })
            combined_lines.extend(data.get("lines", []))

    if not combined_lines:
        return jsonify({"error": "Could not fetch prayer text from Sefaria"}), 404

    return jsonify({
        "prayer": resolved_name,
        "lines": combined_lines,
        "sources": refs
    })


# ─── COMMUNITY CUSTOMS API (Merkava) ──────────────────────────────────────────

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


@app.route("/api/communities/list")
def get_communities_list():
    """Returns list of available communities."""
    communities = sorted(COMMUNITIES.keys())
    return jsonify([{"name": c} for c in communities])


@app.route("/api/community/<name>")
def get_community(name):
    """Returns community customs data."""
    resolved_name = (unquote(name or "") or "").strip()
    canonical_name = _canonicalize_community_name(resolved_name)
    if canonical_name is None:
        return jsonify({"error": f"Community '{resolved_name}' not found"}), 404

    filename = COMMUNITIES[canonical_name]
    filepath = os.path.join(os.path.dirname(__file__),
                            "customs", f"{filename}.json")

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Extract key information for display
        identity = data.get("identity", {})
        trusted_sources = _build_trusted_custom_sources(data)

        # Extract customs from halacha_index
        customs_content = {}
        for item in data.get("halacha_index", []) if isinstance(data, dict) else []:
            if not isinstance(item, dict):
                continue
            topic = item.get("topic", "").lower()
            category = item.get("category", "").lower()
            key = f"{category}_{topic}".strip("_")
            customs_content[key] = {
                "category": category,
                "topic": topic,
                "ruling": item.get("summary", ""),
                "common_practices": item.get("common_practices", []),
                "source": item.get("source", "") or ", ".join(trusted_sources[:4])
            }

        fallback_customs = data if isinstance(data, dict) else {}

        return jsonify({
            "name": canonical_name,
            "requested_name": resolved_name,
            "heritage_id": data.get("heritage_id") if isinstance(data, dict) else None,
            "primary_origin": identity.get("primary_origin", "") if isinstance(identity, dict) else "",
            "customs": customs_content if customs_content else fallback_customs,
            "raw_data": data  # Full data available if needed
        })
    except Exception as e:
        return jsonify({"error": f"Could not load community data: {str(e)}"}), 500


@app.route("/api/community/<name>/timeline")
def get_community_timeline(name):
    """Returns a normalized community timeline for timeline view components."""
    resolved_name = (unquote(name or "") or "").strip()
    canonical_name = _canonicalize_community_name(resolved_name)
    if canonical_name is None:
        return jsonify({"error": f"Community '{resolved_name}' not found"}), 404

    filename = COMMUNITIES[canonical_name]
    filepath = os.path.join(os.path.dirname(__file__),
                            "customs", f"{filename}.json")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Could not load community data: {str(e)}"}), 500

    timeline = []

    identity = data.get("identity", {}) if isinstance(data, dict) else {}
    origin = identity.get("primary_origin") if isinstance(
        identity, dict) else ""
    if origin:
        timeline.append({
            "title": "Primary Origin",
            "description": origin,
            "approx_period": "Historic",
        })

    for key in ("timeline", "history", "historical_timeline", "migration_story"):
        value = data.get(key) if isinstance(data, dict) else None
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    timeline.append({
                        "title": str(item.get("title") or item.get("period") or key).strip()[:120],
                        "description": str(item.get("description") or item.get("event") or "").strip()[:400],
                        "approx_period": str(item.get("year") or item.get("period") or "").strip()[:80],
                    })
                elif isinstance(item, str):
                    timeline.append({
                        "title": key.replace("_", " ").title(),
                        "description": item.strip()[:400],
                        "approx_period": "",
                    })
        elif isinstance(value, str) and value.strip():
            timeline.append({
                "title": key.replace("_", " ").title(),
                "description": value.strip()[:400],
                "approx_period": "",
            })

    if not timeline:
        timeline.append({
            "title": "Tradition",
            "description": f"{canonical_name} customs are preserved through local minhagim and halachic practice.",
            "approx_period": "Ongoing",
        })

    return jsonify({
        "name": canonical_name,
        "events": timeline[:30],
    })


# ─── TEXTS INDEX (for top menu) ───────────────────────────────────────────────
@app.route("/api/texts-index")
def get_texts_index():
    """Returns complete index of browsable texts: prayers, communities, Sefaria."""
    from sefaria_library import get_liturgy_books

    return jsonify({
        "siddur": {
            "title": "Sefaria Prayer Books",
            "items": [b.get("title") for b in get_liturgy_books(max_items=200)]
        },
        "merkava": {
            "title": "Community Customs (Merkava)",
            "items": list(COMMUNITIES.keys())
        },
        "sefaria": {
            "title": "Jewish Text Library",
            "items": ["Tanakh", "Mishnah", "Talmud", "Halakhah", "Kabbalah"]
        }
    })


@app.route("/api/holidays")
def get_holidays():
    """Returns Jewish holiday events for FullCalendar via Hebcal API."""
    year = request.args.get('year', str(greg_date.today().year))
    url = (
        f"https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on"
        f"&nx=on&year={year}&month=x&ss=on&s=on&mf=on&c=off&geo=none"
    )
    try:
        r = requests.get(url, timeout=5)
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            raise ValueError(data.get("error"))
        items = data.get("items", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            items = []

        events = []
        for item in items:
            if not isinstance(item, dict):
                continue

            category = str(item.get("category") or "").strip().lower()
            title_raw = item.get("title") or ""
            title_clean = _strip_leading_symbol_prefix(title_raw)
            start = item.get("date") or item.get("start")

            if not title_clean or not start:
                continue

            emoji = _holiday_emoji_for_event(title_clean, category)
            events.append({
                "title": f"{emoji} {title_clean}",
                "start": start,
                "allDay": "T" not in str(start),
                "display": "block",
                "color": _holiday_color_for_category(category),
                "textColor": "#ffffff",
            })

        return jsonify(events)
    except Exception:
        fallback = _build_pyluach_holiday_events(year)
        if fallback:
            return jsonify(fallback)

        # Last-resort fallback to monthly zmanim events so calendar is never empty.
        try:
            engine = get_engine()
            return jsonify(engine.get_monthly_zmanim())
        except Exception:
            return jsonify([])


@app.route("/api/parasha")
def get_parasha():
    """Return current weekly Parasha information for the Torah section."""
    try:
        r = requests.get("https://www.sefaria.org/api/calendars", timeout=6)
        data = r.json()

        for item in data.get("calendar_items", []):
            title_en = (item.get("title", {}) or {}).get("en", "")
            if "Parashat" in title_en or "Parasha" in title_en:
                display_en = (item.get("displayValue", {}) or {}).get("en", "")
                display_he = (item.get("displayValue", {}) or {}).get("he", "")
                ref = item.get("ref") or ""
                return jsonify({
                    "title": display_en or title_en,
                    "heTitle": display_he,
                    "ref": ref,
                    "source": "sefaria-calendars",
                })
    except Exception:
        pass

    try:
        from calendar_service import calendar_engine
        parasha_name = calendar_engine.get_parasha()
        return jsonify({
            "title": parasha_name or "Parashat HaShavua",
            "heTitle": "",
            "ref": "Genesis 1",
            "source": "calendar-fallback",
        })
    except Exception:
        return jsonify({
            "title": "Parashat HaShavua",
            "heTitle": "",
            "ref": "Genesis 1",
            "source": "default-fallback",
        })


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5001))
    app.run(debug=True, host='0.0.0.0', port=port)
