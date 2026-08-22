"""
Retrieval & corpus-matching layer for Sh'elah.

Contract: no Flask context. Owns Hebrew/English keyword extraction, the
halakhic-corpus discovery-query builder, Sefaria/external global source
collection, local-JSON custom-corpus matching, the get_halakhic_sources
fallback ladder (specific API -> broad API -> internal AI knowledge), and the
Google/MyMemory translation primitives. Imports only stdlib + backend.* leaf
modules, never `app`.

Moved verbatim from app.py in Phase 2 of the backend refactor (see plan.md).
`_translate_text_google` / `_translate_text_mymemory` (and their inseparable
pure helpers `_is_translation_echo` / `_extract_google_translated_text`) were
previously diverged, duplicated copies in both app.py and backend/helpers.py
(plan.md §2); this is now the single canonical implementation, re-exported by
both call sites.
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import quote

import requests

from backend import claude
from backend import search
from backend.health_check import health
from backend.utils.text_engine import RABBI_FINAL_RULING_FOOTER

logger = logging.getLogger(__name__)

APP_ROOT = Path(__file__).resolve().parent.parent.parent

SEFARIA_SEARCH_WRAPPER_URL = "https://www.sefaria.org.il/api/search-wrapper"
GOOGLE_TRANSLATE_API_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_TRANSLATE_API_URL = "https://api.mymemory.translated.net/get"

HEBREW_LETTER_RE = re.compile(r"[\u05D0-\u05EA]")

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

# Domain-term literals reused across HALAKHIC_CORPUS_ALIASES,
# DIRECT_TOPIC_SOURCE_MAP, and QUERY_BROADENER_MAP below (SonarCloud
# python:S1192).
_TERM_YOREH_DEAH = "yoreh deah"
_TERM_SEFIRAT_HAOMER = "sefirat haomer"
_TERM_MEAT_AND_MILK = "meat and milk"
_TERM_FAMILY_PURITY = "family purity"

HALAKHIC_CORPUS_ALIASES = {
    "Shulchan Arukh": [
        "shulchan arukh",
        "shulchan aruch",
        "orach chayim",
        "yoreh de'ah",
        _TERM_YOREH_DEAH,
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

HEBREW_PREFIXES = ("ו", "ה", "ל", "ב", "ש", "מ")

INTERNAL_AI_KNOWLEDGE_DISCLAIMER = (
    "Note: ⚠️ This is educational information derived from general halakhic "
    f"knowledge, as the specific database source was unavailable — not a "
    f"halachic ruling. {RABBI_FINAL_RULING_FOOTER}"
)
WEB_FALLBACK_TRUST_TERMS = {
    "halach", "halakh", "jewish", "judaism", "torah", "talmud", "shabbat",
    "yom tov", "kashrut", "tefillin", "mezuzah", "sefaria", "hebrewbooks",
    "peninei", "yeshivat har bracha", "yhb", "zmanim", "hebrew date", "calendar",
}
WEB_FALLBACK_BLOCKLIST_TERMS = {
    "biblegateway", "biblehub", "biblestudytools", "king james", "new testament",
    "gospel", "church", "jesus", "christian bible",
}

# Topic-first direct source anchors for high-signal chapter targeting.
DIRECT_TOPIC_SOURCE_MAP = {
    "omer": {
        "triggers": ["omer", "sefira", "sefirah", _TERM_SEFIRAT_HAOMER, "lag baomer", "lag ba'omer", "haircut", "haircuts"],
        "citations": [
            "Shulchan Arukh, Orach Chayim 489",
            "Shulchan Arukh, Orach Chayim 493",
        ],
        "broad_terms": ["omer", _TERM_SEFIRAT_HAOMER, "sefira", "haircuts", "mourning customs"],
    },
    "shabbat": {
        "triggers": ["shabbat", "shabbos", "melacha", "havdalah", "kiddush"],
        "citations": [
            "Shulchan Arukh, Orach Chayim 242",
            "Shulchan Arukh, Orach Chayim 318",
        ],
        "broad_terms": ["shabbat", "melacha", "havdalah", "kiddush", "nightfall"],
    },
    "kashrut": {
        "triggers": ["kashrut", "kosher", "basar", "chalav", "meat", "dairy", "treif", "treife"],
        "citations": [
            "Shulchan Arukh, Yoreh De'ah 87",
            "Shulchan Arukh, Yoreh De'ah 89",
        ],
        "broad_terms": ["kashrut", "kosher", _TERM_MEAT_AND_MILK, "basar bechalav", _TERM_YOREH_DEAH],
    },
    "niddah": {
        "triggers": ["niddah", "nidda", "mikveh", "taharah", _TERM_FAMILY_PURITY],
        "citations": [
            "Shulchan Arukh, Yoreh De'ah 183",
            "Shulchan Arukh, Yoreh De'ah 197",
        ],
        "broad_terms": ["niddah", _TERM_FAMILY_PURITY, "mikveh", "taharah", _TERM_YOREH_DEAH],
    },
}

QUERY_BROADENER_MAP = {
    "omer": [_TERM_SEFIRAT_HAOMER, "sefira", "haircuts"],
    "sefirah": ["omer", _TERM_SEFIRAT_HAOMER],
    "sefira": ["omer", _TERM_SEFIRAT_HAOMER],
    "haircuts": ["haircut", "mourning customs", "omer"],
    "shabbos": ["shabbat", "melacha", "havdalah"],
    "shabbat": ["melacha", "kiddush", "havdalah"],
    "kashrut": ["kosher", _TERM_YOREH_DEAH, _TERM_MEAT_AND_MILK],
    "kosher": ["kashrut", _TERM_YOREH_DEAH, _TERM_MEAT_AND_MILK],
    "niddah": [_TERM_FAMILY_PURITY, "mikveh", "taharah"],
}


def _strip_common_hebrew_prefixes(token):
    value = str(token or "").strip()
    if not value:
        return value
    if not HEBREW_LETTER_RE.search(value):
        return value

    normalized = value
    # Allow stacked prefixes but keep a meaningful stem length.
    while len(normalized) > 2 and normalized[0] in HEBREW_PREFIXES:
        normalized = normalized[1:]

    return normalized or value


def _expand_hebrew_keyword_forms(token):
    base = str(token or "").strip().lower()
    if not base:
        return []

    expanded = [base]
    stripped = _strip_common_hebrew_prefixes(base)
    if stripped and stripped not in expanded:
        expanded.append(stripped)

    return expanded


def _extract_query_keywords(query, max_keywords=8):
    tokens = re.findall(r"[A-Za-z\u0590-\u05FF]{3,}", str(query or "").lower())
    keywords = []
    for token in tokens:
        for normalized_token in _expand_hebrew_keyword_forms(token):
            if normalized_token in QUERY_STOPWORDS:
                continue
            if normalized_token not in keywords:
                keywords.append(normalized_token)
            if len(keywords) >= max_keywords:
                break
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

    if not health.is_healthy("sefaria"):
        return []

    try:
        resp = requests.post(
            SEFARIA_SEARCH_WRAPPER_URL,
            json=payload,
            timeout=8,
        )
        if not resp.ok:
            health.record_failure("sefaria")
            return []
        data = resp.json() if resp.content else {}
        health.record_success("sefaria")
        return ((data.get("hits") or {}).get("hits") or [])
    except (requests.RequestException, TimeoutError) as exc:
        health.record_failure("sefaria")
        logger.warning("search_provider[sefaria] search-wrapper call failed: %s", exc)
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


def _dedupe_ordered_text(values, max_items=None):
    collected = []
    seen = set()

    for value in values or []:
        normalized = re.sub(r"\s+", " ", str(value or "")).strip()
        if not normalized:
            continue

        key = normalized.lower()
        if key in seen:
            continue

        seen.add(key)
        collected.append(normalized)

        if max_items and len(collected) >= max_items:
            break

    return collected


def _match_direct_topics(question, keywords):
    haystack = f"{question} {' '.join(keywords)}".lower()
    matched = []

    for topic, config in DIRECT_TOPIC_SOURCE_MAP.items():
        triggers = config.get("triggers", [])
        if any(trigger in haystack for trigger in triggers):
            matched.append(topic)

    return matched


def _build_discovery_queries(question, keywords):
    matched_topics = _match_direct_topics(question, keywords)

    specific_queries = []
    broad_terms = list(keywords)

    for topic in matched_topics:
        config = DIRECT_TOPIC_SOURCE_MAP.get(topic, {})
        specific_queries.extend(config.get("citations", []))
        specific_queries.append(f"{topic} shulchan arukh")
        broad_terms.extend(config.get("broad_terms", []))

    for keyword in keywords:
        broad_terms.extend(QUERY_BROADENER_MAP.get(keyword.lower(), []))

    if question:
        specific_queries.append(question)
        broad_terms.extend(_extract_query_keywords(question, max_keywords=12))

    specific_queries = _dedupe_ordered_text(specific_queries, max_items=14)
    broad_terms = _dedupe_ordered_text(broad_terms, max_items=18)

    broad_queries = []
    if broad_terms:
        broad_queries.append(" ".join(broad_terms[:5]))
        if len(broad_terms) >= 8:
            broad_queries.append(" ".join(broad_terms[3:8]))
        broad_queries.extend(broad_terms[:10])
    if question:
        broad_queries.append(question)
    broad_queries = _dedupe_ordered_text(broad_queries, max_items=16)

    if not specific_queries and question:
        specific_queries = [question]

    return {
        "topics": matched_topics,
        "specific_queries": specific_queries,
        "broad_queries": broad_queries,
    }


def _is_sefaria_hit_relevant(hit_source, query_terms):
    if not query_terms:
        return True

    categories = hit_source.get("categories", [])
    title_variants = hit_source.get("titleVariants", [])
    snippet = _extract_hit_snippet(hit_source)
    haystack = " ".join([
        str(hit_source.get("ref") or ""),
        str(hit_source.get("path") or ""),
        " ".join(categories if isinstance(categories, list) else []),
        " ".join(title_variants if isinstance(title_variants, list) else []),
        snippet,
    ]).lower().replace("_", " ")

    terms = []
    for term in query_terms:
        normalized = str(term or "").strip().lower()
        if len(normalized) < 3 or normalized in QUERY_STOPWORDS:
            continue
        terms.append(normalized)

    if not terms:
        return True

    return any(term in haystack for term in terms)


def _collect_global_sefaria_sources(queries, fallback_terms, discovery_stage, priority, max_results=10):
    sources = []
    seen_refs = set()

    per_query_limit = 3 if discovery_stage == "specific-api" else 2

    for query_text in queries:
        normalized_query = str(query_text or "").strip()
        if not normalized_query:
            continue

        hits = _query_search_wrapper(normalized_query, size=80)
        query_terms = _extract_query_keywords(
            normalized_query) or fallback_terms

        added_for_query = 0
        for hit in hits:
            hit_source = hit.get("_source", {}) if isinstance(
                hit, dict) else {}
            if not isinstance(hit_source, dict):
                continue

            if not _is_sefaria_hit_relevant(hit_source, query_terms):
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
                "lines": [{"en": snippet or f"Matched via global search: {normalized_query}", "he": he_ref}],
                "domain": "Sefaria",
                "corpus": "sefaria-global-search",
                "path": path,
                "priority": priority,
                "status": "fallback",
                "discovery_stage": discovery_stage,
                "search_query": normalized_query,
                "score": hit.get("_score") if isinstance(hit, dict) else None,
            })

            added_for_query += 1
            if added_for_query >= per_query_limit or len(sources) >= max_results:
                break

        if len(sources) >= max_results:
            break

    return sources


def _collect_external_global_sources(queries, keywords, discovery_stage, priority, max_results=6):
    providers = [
        ("Halachipedia", "halachipedia.com", search.search_halachipedia),
        ("HebrewBooks", "hebrewbooks.org", search.search_hebrewbooks),
    ]

    sources = []
    seen = set()

    for query_text in queries:
        normalized_query = str(query_text or "").strip()
        if not normalized_query:
            continue

        for provider_name, domain, provider_search in providers:
            if not health.is_healthy("web"):
                continue

            try:
                payload = provider_search(normalized_query)
            except (requests.RequestException, TimeoutError) as exc:
                health.record_failure("web")
                logger.warning(
                    "search_provider[web] %s call failed: %s", provider_name, exc)
                continue
            else:
                health.record_success("web")

            if not isinstance(payload, dict):
                continue

            title = str(payload.get("title") or "").strip()
            summary = str(payload.get("summary") or "").strip()
            if provider_name == "Halachipedia":
                title = re.sub(r"^\[Halachipedia\]\s*", "", title).strip()

            if not title and not summary:
                continue

            if not _looks_like_trusted_web_match(provider_name.lower(), title, summary, keywords):
                continue

            dedupe_key = (provider_name.lower(), title.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            url = str(payload.get("url") or "").strip()
            if not url and provider_name == "Halachipedia" and title:
                slug = quote(title.replace(" ", "_"), safe="")
                url = f"https://halachipedia.com/wiki/{slug}" if slug else "https://halachipedia.com"
            if not url and provider_name == "HebrewBooks":
                url = f"https://www.hebrewbooks.org/search.aspx?st=FT&q={quote(normalized_query, safe='')}"

            sources.append({
                "ref": title[:140] or provider_name,
                "title": title[:160] or provider_name,
                "lines": [{"en": summary[:1000], "he": ""}],
                "domain": domain,
                "corpus": "external-global-search",
                "source_provider": provider_name,
                "url": url,
                "priority": priority,
                "status": "fallback",
                "discovery_stage": discovery_stage,
                "search_query": normalized_query,
            })

            if len(sources) >= max_results:
                return sources

    return sources


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
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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


def _looks_like_trusted_web_match(provider, title, summary, keywords):
    provider_name = str(provider or "").strip().lower()
    title_text = str(title or "").strip()
    summary_text = str(summary or "").strip()
    if not title_text or not summary_text:
        return False

    haystack = f"{title_text} {summary_text}".lower()
    if any(flag in haystack for flag in WEB_FALLBACK_BLOCKLIST_TERMS):
        return False

    if provider_name in {"halachipedia", "hebrewbooks"}:
        return True

    if any(term in haystack for term in WEB_FALLBACK_TRUST_TERMS):
        return True

    # Require relevance to the query if no explicit trust-term signal is present.
    return any(kw in haystack for kw in keywords)


def _fetch_trusted_web_candidate(fetch_fn, query, provider_name, keywords, build_candidate):
    """Run one provider search call and return a trusted candidate dict, or None."""
    if not query or not health.is_healthy("web"):
        return None
    try:
        payload = fetch_fn(query)
    except (requests.RequestException, TimeoutError) as exc:
        health.record_failure("web")
        logger.warning("search_provider[web] %s call failed: %s", provider_name, exc)
        return None
    health.record_success("web")
    if not isinstance(payload, dict):
        return None
    return build_candidate(payload, keywords)


def _build_halachipedia_candidate(payload, keywords):
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    clean_title = re.sub(r"^\[Halachipedia\]\s*", "", title).strip()
    if not _looks_like_trusted_web_match("halachipedia", clean_title, summary, keywords):
        return None
    url_slug = quote(clean_title.replace(" ", "_"), safe="") if clean_title else ""
    return {
        "provider": "Halachipedia",
        "domain": "halachipedia.com",
        "title": clean_title or title,
        "summary": summary,
        "url": f"https://halachipedia.com/wiki/{url_slug}" if url_slug else "https://halachipedia.com",
    }


def _build_wikipedia_candidate(payload, keywords):
    title = str(payload.get("title") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if not _looks_like_trusted_web_match("wikipedia", title, summary, keywords):
        return None
    url_slug = quote(title.replace(" ", "_"), safe="") if title else ""
    return {
        "provider": "Wikipedia",
        "domain": "en.wikipedia.org",
        "title": title,
        "summary": summary,
        "url": f"https://en.wikipedia.org/wiki/{url_slug}" if url_slug else "https://en.wikipedia.org",
    }


def _dedupe_web_candidates(candidates, max_results):
    deduped = []
    seen = set()
    for item in candidates:
        key = (
            str(item.get("provider") or "").lower(),
            str(item.get("title") or "").strip().lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= max_results:
            break
    return deduped


def _format_web_sources(deduped):
    web_sources = []
    for item in deduped:
        title = str(item.get("title") or "Web Source").strip()
        summary = str(item.get("summary") or "").strip()
        web_sources.append({
            "ref": title[:140],
            "title": title[:160],
            "lines": [{"en": summary[:1000], "he": ""}],
            "domain": item.get("domain"),
            "corpus": "general-web",
            "source_provider": item.get("provider"),
            "url": item.get("url"),
            "priority": 3,
            "status": "fallback-web",
        })
    return web_sources


def _build_last_resort_web_sources(question, keywords, max_results=6):
    query = str(question or "").strip()
    keyword_query = " ".join(keywords[:5]).strip()

    halachipedia_queries = []
    if keyword_query:
        halachipedia_queries.append(keyword_query)
        halachipedia_queries.append(f"halakha {keyword_query}".strip())
    if query:
        halachipedia_queries.append(query)

    wiki_titles = [
        "Peninei Halakha",
        "Yeshivat Har Bracha",
        "HebrewBooks",
    ]
    if keyword_query:
        wiki_titles.append(f"Halakha {keyword_query}".strip())
    if query:
        wiki_titles.append(f"Halakha {query}".strip())

    candidates = []

    for q in halachipedia_queries:
        candidate = _fetch_trusted_web_candidate(
            search.search_halachipedia, q, "halachipedia", keywords, _build_halachipedia_candidate)
        if candidate:
            candidates.append(candidate)

    for title_query in wiki_titles:
        candidate = _fetch_trusted_web_candidate(
            search.search_wikipedia, title_query, "wikipedia", keywords, _build_wikipedia_candidate)
        if candidate:
            candidates.append(candidate)

    deduped = _dedupe_web_candidates(candidates, max_results)
    return _format_web_sources(deduped)


def get_halakhic_sources(query):
    """Global discovery fallback: specific API -> broad API -> internal AI knowledge."""
    question = claude.sanitize_user_query(query)
    keywords = _extract_query_keywords(question)
    if not keywords and question:
        keywords = [question.lower()]

    discovery = _build_discovery_queries(question, keywords)
    topic_matches = discovery.get("topics", [])
    specific_queries = discovery.get("specific_queries", [])
    broad_queries = discovery.get("broad_queries", [])

    specific_sefaria = _collect_global_sefaria_sources(
        specific_queries,
        fallback_terms=keywords,
        discovery_stage="specific-api",
        priority=1,
        max_results=8,
    )
    specific_external = _collect_external_global_sources(
        specific_queries,
        keywords=keywords,
        discovery_stage="specific-api",
        priority=1,
        max_results=4,
    )
    specific_sources = specific_sefaria + specific_external

    if specific_sources:
        return {
            "status": "fallback",
            "fallback_level": "specific-api",
            "query": question,
            "keywords": keywords,
            "topics": topic_matches,
            "specific_queries": specific_queries,
            "broad_queries": broad_queries,
            "sequence": ["specific-api", "broad-api", "internal-ai-knowledge"],
            "counts": {
                "specific_api": len(specific_sources),
                "broad_api": 0,
                "internal_ai": 0,
                "sefaria": len(specific_sefaria),
                "external": len(specific_external),
            },
            "warning": "",
            "internal_disclaimer": "",
            "source_count": len(specific_sources),
            "sources": specific_sources,
        }

    broad_sefaria = _collect_global_sefaria_sources(
        broad_queries,
        fallback_terms=keywords,
        discovery_stage="broad-api",
        priority=2,
        max_results=10,
    )
    broad_external = _collect_external_global_sources(
        broad_queries,
        keywords=keywords,
        discovery_stage="broad-api",
        priority=2,
        max_results=6,
    )
    broad_sources = broad_sefaria + broad_external

    if broad_sources:
        return {
            "status": "fallback",
            "fallback_level": "broad-api",
            "query": question,
            "keywords": keywords,
            "topics": topic_matches,
            "specific_queries": specific_queries,
            "broad_queries": broad_queries,
            "sequence": ["specific-api", "broad-api", "internal-ai-knowledge"],
            "counts": {
                "specific_api": 0,
                "broad_api": len(broad_sources),
                "internal_ai": 0,
                "sefaria": len(broad_sefaria),
                "external": len(broad_external),
            },
            "warning": "",
            "internal_disclaimer": "",
            "source_count": len(broad_sources),
            "sources": broad_sources,
        }

    return {
        "status": "internal-ai-needed",
        "fallback_level": "internal-ai-knowledge",
        "query": question,
        "keywords": keywords,
        "topics": topic_matches,
        "specific_queries": specific_queries,
        "broad_queries": broad_queries,
        "sequence": ["specific-api", "broad-api", "internal-ai-knowledge"],
        "counts": {
            "specific_api": 0,
            "broad_api": 0,
            "internal_ai": 1,
            "sefaria": 0,
            "external": 0,
        },
        "warning": "",
        "internal_disclaimer": INTERNAL_AI_KNOWLEDGE_DISCLAIMER,
        "source_count": 1,
        "sources": [{
            "ref": "Internal Halakhic Knowledge",
            "title": "Internal Halakhic Knowledge",
            "lines": [{
                "en": "No relevant API snippet was found. The answer may rely on internal Halakhic knowledge with the required disclaimer.",
                "he": "",
            }],
            "domain": "internal-ai",
            "corpus": "internal-knowledge",
            "priority": 3,
            "status": "internal-ai-needed",
        }],
    }


def _is_translation_echo(source_text, translated_text):
    src = re.sub(r"\s+", " ", str(source_text or "").strip()).lower()
    dst = re.sub(r"\s+", " ", str(translated_text or "").strip()).lower()
    return bool(src and dst and src == dst)


def _extract_google_translated_text(payload):
    if not isinstance(payload, list) or not payload:
        return ""
    segments = payload[0]
    if not isinstance(segments, list):
        return ""

    chunks = []
    for segment in segments:
        if isinstance(segment, list) and segment:
            chunk = str(segment[0] or "").strip()
            if chunk:
                chunks.append(chunk)

    return re.sub(r"\s+", " ", "".join(chunks)).strip()


def _translate_text_google(text, source_lang, target_lang):
    value = str(text or "").strip()
    if not value:
        return ""
    if not health.is_healthy("translate_google"):
        return ""

    try:
        resp = requests.get(
            GOOGLE_TRANSLATE_API_URL,
            params={
                "client": "gtx",
                "sl": str(source_lang or "auto").strip() or "auto",
                "tl": str(target_lang or "en").strip() or "en",
                "dt": "t",
                "q": value,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=2.5,
        )
        if not resp.ok:
            health.record_failure("translate_google")
            return ""
        payload = resp.json() if resp.content else []
        health.record_success("translate_google")
        translated = _extract_google_translated_text(payload)
        if not translated:
            return ""
        if _is_translation_echo(value, translated):
            return ""
        return translated
    except (requests.RequestException, TimeoutError) as exc:
        health.record_failure("translate_google")
        logger.warning("search_provider[translate_google] call failed: %s", exc)
        return ""


def _translate_text_mymemory(text, source_lang, target_lang):
    value = str(text or "").strip()
    if not value:
        return ""
    if not health.is_healthy("translate_mymemory"):
        return ""

    langpair_source = str(source_lang or "auto").strip() or "auto"
    langpair_target = str(target_lang or "en").strip() or "en"

    try:
        resp = requests.get(
            MYMEMORY_TRANSLATE_API_URL,
            params={"q": value, "langpair": f"{langpair_source}|{langpair_target}"},
            timeout=2.5,
        )
        if not resp.ok:
            health.record_failure("translate_mymemory")
            return ""
        payload = resp.json() if resp.content else {}
        health.record_success("translate_mymemory")
        translated = str((payload.get("responseData") or {}).get(
            "translatedText") or "").strip()
        if not translated:
            return ""
        if _is_translation_echo(value, translated):
            return ""
        return translated
    except (requests.RequestException, TimeoutError) as exc:
        health.record_failure("translate_mymemory")
        logger.warning("search_provider[translate_mymemory] call failed: %s", exc)
        return ""
