"""
Shared helpers, constants, and word-meaning/translation utilities for Sh'elah blueprints.

All implementations are self-contained — no lazy proxies to app.py.

Hebrew word-meaning lookup chain:
  local glossary → Sefaria BDB/Jastrow → Google Translate → MyMemory

Alternatives pipeline:
  interpretive glossary → parsed meaning candidates → Sefaria lexicon variants
  (falls back to Google Translate only when Sefaria has no result)
"""

import re
import time
from urllib.parse import unquote, quote

import requests as _requests
from flask import request as _flask_request

# ── Answer-mode & source-attribution constants ────────────────────────────────

ANSWER_MODES = {"balanced", "practical", "sources", "strict"}

RABBI_FINAL_RULING_FOOTER = "Please consult with your local Rabbi for a final ruling."
INTERNAL_AI_KNOWLEDGE_DISCLAIMER = (
    "Note: This information was derived from General Halakhic Knowledge "
    f"as the specific database source was unavailable. {RABBI_FINAL_RULING_FOOTER}"
)

# ── Bounded cache ─────────────────────────────────────────────────────────────

_CACHE_MAX_SIZE = 512


def _bounded_cache_set(cache: dict, key, value, maxsize: int = _CACHE_MAX_SIZE) -> None:
    if key not in cache and len(cache) >= maxsize:
        cache.pop(next(iter(cache)), None)
    cache[key] = value


TRANSLATION_CACHE: dict = {}
TRANSLATION_SOURCE_CACHE: dict = {}

# ── Text aliases ──────────────────────────────────────────────────────────────

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

# ── Community registry ────────────────────────────────────────────────────────

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

# ── Hebrew constants ──────────────────────────────────────────────────────────

HEBREW_DIACRITICS_RE = re.compile(r"[֑-ׇ]")
HEBREW_LETTER_RE = re.compile(r"[א-ת]")

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

HEBREW_INTERPRETIVE_GLOSSARY = {
    "ברא": ["create", "fashion", "bring into being"],
    "עשה": ["make", "do", "perform"],
    "אמר": ["say", "speak", "declare"],
    "הלך": ["go", "walk", "proceed"],
    "שמר": ["guard", "keep", "observe"],
}

# ── Hebrew text utilities ─────────────────────────────────────────────────────


def _strip_hebrew_diacritics(text):
    return HEBREW_DIACRITICS_RE.sub("", str(text or ""))


def _contains_hebrew_letters(text):
    return bool(HEBREW_LETTER_RE.search(str(text or "")))


def _normalize_lookup_word(text):
    cleaned = _strip_hebrew_diacritics(text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _normalize_glossary_meaning(value):
    text = re.sub(r"\s+", " ", str(value or "").strip())
    if not text:
        return ""
    if "," in text:
        head, tail = text.split(",", 1)
        if re.fullmatch(r"[A-Za-z'\-\s]{2,40}", head.strip()) and tail.strip():
            return tail.strip()
    return text


def _looks_like_transliteration(text):
    value = re.sub(r"\s+", " ", str(text or "").strip())
    if not value:
        return False
    if not re.fullmatch(r"[A-Za-z'\-\s]{2,80}", value):
        return False

    lower = value.lower()
    tokens = [part for part in lower.split(" ") if part]
    if not tokens:
        return False

    translit_markers = ("sh", "kh", "tz", "ts", "aa", "ee", "oo", "iy", "ui")
    translit_suffixes = ("im", "ot", "ah", "eh", "it", "ut", "iyyah")

    if any("'" in token or "-" in token for token in tokens):
        return True
    if len(tokens) <= 3 and any(marker in lower for marker in translit_markers):
        return True
    if len(tokens) <= 2 and all(
        any(token.endswith(suffix) for suffix in translit_suffixes) for token in tokens
    ):
        return True
    if len(tokens) == 1 and len(tokens[0]) <= 4 and tokens[0].endswith(("a", "e", "i", "o", "u")):
        return True

    return False


# ── Pure utilities ────────────────────────────────────────────────────────────


def _coerce_int(value, default, min_value=1, max_value=100):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(parsed, max_value))


def _decode_route_ref(value, max_rounds=3):
    decoded = str(value or "").strip()
    for _ in range(max_rounds):
        next_value = unquote(decoded).strip()
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


# ── Translation infrastructure ────────────────────────────────────────────────

GOOGLE_TRANSLATE_API_URL = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_TRANSLATE_API_URL = "https://api.mymemory.translated.net/get"


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
    try:
        resp = _requests.get(
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
            return ""
        payload = resp.json() if resp.content else []
        translated = _extract_google_translated_text(payload)
        if not translated or _is_translation_echo(value, translated):
            return ""
        return translated
    except Exception:
        return ""


def _translate_text_mymemory(text, source_lang, target_lang):
    value = str(text or "").strip()
    if not value:
        return ""
    langpair_source = str(source_lang or "auto").strip() or "auto"
    langpair_target = str(target_lang or "en").strip() or "en"
    try:
        resp = _requests.get(
            MYMEMORY_TRANSLATE_API_URL,
            params={"q": value, "langpair": f"{langpair_source}|{langpair_target}"},
            timeout=2.5,
        )
        if not resp.ok:
            return ""
        payload = resp.json() if resp.content else {}
        translated = str(
            (payload.get("responseData") or {}).get("translatedText") or ""
        ).strip()
        if not translated or _is_translation_echo(value, translated):
            return ""
        return translated
    except Exception:
        return ""


def _translate_hebrew_text_google(text):
    value = str(text or "").strip()
    if not value or not _contains_hebrew_letters(value):
        return ""
    translated = _translate_text_google(value, "he", "en")
    if not translated or _is_translation_echo(value, translated):
        return ""
    return translated


def _translate_hebrew_text_mymemory(text):
    value = str(text or "").strip()
    if not value or not _contains_hebrew_letters(value):
        return ""
    translated = _translate_text_mymemory(value, "he", "en")
    if not translated or _is_translation_echo(value, translated):
        return ""
    return translated


def _translate_hebrew_text_online(text):
    """Hebrew → English: Google first, MyMemory fallback. Results cached."""
    value = _normalize_lookup_word(text)
    if not value or not _contains_hebrew_letters(value):
        return "", ""

    cache_key = value[:320]
    if cache_key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE.get(cache_key, ""), TRANSLATION_SOURCE_CACHE.get(cache_key, "")

    translated = _translate_hebrew_text_google(cache_key)
    source = "google-translate"

    if not translated:
        translated = _translate_hebrew_text_mymemory(cache_key)
        source = "mymemory-translate"

    _bounded_cache_set(TRANSLATION_CACHE, cache_key, translated)
    _bounded_cache_set(TRANSLATION_SOURCE_CACHE, cache_key, source if translated else "")

    return (translated, source) if translated else ("", "")


def _translate_english_text_online(text):
    """English → Hebrew: Google first, MyMemory fallback. Results cached."""
    value = _normalize_lookup_word(text)
    if not value:
        return "", ""

    cache_key = f"en-he::{value[:320]}"
    if cache_key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE.get(cache_key, ""), TRANSLATION_SOURCE_CACHE.get(cache_key, "")

    translated = _translate_text_google(value, "en", "he")
    source = "google-translate"

    if not translated or _is_translation_echo(value, translated):
        translated = _translate_text_mymemory(value, "en", "he")
        source = "mymemory-translate"

    if _is_translation_echo(value, translated):
        translated = ""

    _bounded_cache_set(TRANSLATION_CACHE, cache_key, translated)
    _bounded_cache_set(TRANSLATION_SOURCE_CACHE, cache_key, source if translated else "")

    return (translated, source) if translated else ("", "")


# ── Sefaria BDB/Jastrow lexicon ───────────────────────────────────────────────

_SEFARIA_LEXICON_BASE = "https://www.sefaria.org/api/words/"
_PREFERRED_LEXICONS = ("brown-driver-briggs", "bdb", "jastrow", "sefaria")


def _lookup_sefaria_lexicon(word):
    """Look up a Hebrew word in Sefaria's BDB/Jastrow lexicon.
    Returns (definition, lexicon_name) or ("", "")."""
    value = str(word or "").strip()
    if not value or not _contains_hebrew_letters(value):
        return "", ""

    consonants = re.sub(r"[֑-ׇ]", "", value).strip() or value
    cache_key = f"sefaria-lex::{consonants[:80]}"
    if cache_key in TRANSLATION_CACHE:
        return TRANSLATION_CACHE.get(cache_key, ""), TRANSLATION_SOURCE_CACHE.get(cache_key, "")

    try:
        resp = _requests.get(
            _SEFARIA_LEXICON_BASE + quote(consonants, safe=""),
            params={"always_consonants": "1"},
            headers={"User-Agent": "Shelah-App/1.0", "Accept": "application/json"},
            timeout=3.0,
        )
        if not resp.ok:
            _bounded_cache_set(TRANSLATION_CACHE, cache_key, "")
            return "", ""
        entries = resp.json() if resp.content else []
        if not isinstance(entries, list):
            _bounded_cache_set(TRANSLATION_CACHE, cache_key, "")
            return "", ""

        def _lex_rank(entry):
            name = str((entry or {}).get("lexicon_name", "")).lower()
            for i, pref in enumerate(_PREFERRED_LEXICONS):
                if pref in name:
                    return i
            return len(_PREFERRED_LEXICONS)

        definition = ""
        lex_name = ""
        for entry in sorted(entries, key=_lex_rank):
            content = (entry or {}).get("content") or {}
            defs = content.get("definitions") or []
            candidates = []
            for d in defs:
                raw = str((d.get("definition") if isinstance(d, dict) else d) or "").strip()
                raw = re.sub(r"<[^>]+>", "", raw).strip()
                raw = re.sub(r"\s+", " ", raw)[:280]
                if raw and not _is_translation_echo(value, raw):
                    candidates.append(raw)
            if not candidates and content.get("definition"):
                raw = re.sub(r"<[^>]+>", "", str(content["definition"])).strip()[:280]
                if raw:
                    candidates.append(raw)
            if candidates:
                definition = candidates[0]
                lex_name = str(entry.get("lexicon_name", "sefaria-lexicon"))
                break

        _bounded_cache_set(TRANSLATION_CACHE, cache_key, definition)
        _bounded_cache_set(TRANSLATION_SOURCE_CACHE, cache_key, lex_name if definition else "")
        return definition, lex_name
    except Exception:
        return "", ""


# ── English dictionary lookup ─────────────────────────────────────────────────


def _lookup_english_word_meaning(word):
    clean_word = str(word or "").strip().lower()
    if not clean_word:
        return "", ""
    try:
        resp = _requests.get(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{clean_word}",
            timeout=5,
        )
        if not resp.ok:
            return "", ""
        payload = resp.json()
        if not isinstance(payload, list) or not payload:
            return "", ""
        entry = payload[0] if isinstance(payload[0], dict) else {}
        meanings = entry.get("meanings", []) if isinstance(entry.get("meanings"), list) else []
        for meaning in meanings:
            definitions = meaning.get("definitions", []) if isinstance(meaning, dict) else []
            for definition in definitions:
                text = str((definition or {}).get("definition") or "").strip()
                if text:
                    return text, "dictionaryapi.dev"
    except Exception:
        return "", ""
    return "", ""


# ── Hebrew word variant helpers ───────────────────────────────────────────────


def _hebrew_word_variant_candidates(raw_word):
    clean_word = _normalize_lookup_word(raw_word)
    letters_only = re.sub(r"[^א-ת\s]", "", clean_word).strip()
    if not letters_only:
        return []

    variants = []

    def add_variant(value):
        token = str(value or "").strip()
        if token and token not in variants:
            variants.append(token)

    add_variant(letters_only)
    parts = [part for part in letters_only.split() if part]
    if parts:
        add_variant(parts[0])

    for part in parts[:2] if parts else [letters_only]:
        if len(part) >= 4 and part[0] in {"ו", "ה", "ב", "כ", "ל", "מ", "ש"}:
            add_variant(part[1:])

    return variants[:4]


def _parse_meaning_candidates(raw_meaning):
    value = re.sub(r"\s+", " ", str(raw_meaning or "").strip())
    if not value:
        return []
    if not any(sep in value for sep in [";", "/", "|"]):
        return [value]
    chunks = [chunk.strip(" .") for chunk in re.split(r"[;/|]", value) if chunk.strip()]
    return chunks[:4] if chunks else [value]


# ── Hebrew word lookup (full chain) ──────────────────────────────────────────


def _lookup_hebrew_word_meaning(word):
    def _strip_common_hebrew_prefix(token):
        value = str(token or "").strip()
        if len(value) < 4:
            return value
        if value and value[0] in {"ו", "ה", "ב", "כ", "ל", "מ", "ש"}:
            return value[1:]
        return value

    def _hebrew_word_variants(raw_word):
        normalized = _normalize_lookup_word(raw_word)
        variants = []

        def add_variant(candidate):
            value = str(candidate or "").strip()
            if value and value not in variants:
                variants.append(value)

        add_variant(normalized)
        letters_only = re.sub(r"[^א-ת\s]", "", normalized).strip()
        add_variant(letters_only)

        if " " in letters_only:
            for part in letters_only.split():
                add_variant(part)
                add_variant(_strip_common_hebrew_prefix(part))
        else:
            add_variant(_strip_common_hebrew_prefix(letters_only))

        return variants[:8]

    clean_word = _normalize_lookup_word(word)
    if not clean_word:
        return "", ""

    variants = _hebrew_word_variants(clean_word)

    for variant in variants:
        exact = HEBREW_WORD_GLOSSARY.get(variant)
        if exact:
            normalized = _normalize_glossary_meaning(exact)
            if normalized:
                return normalized, "local-hebrew-glossary"

    for variant in variants:
        lex_def, lex_src = _lookup_sefaria_lexicon(variant)
        if lex_def:
            return lex_def, lex_src or "sefaria-lexicon"

    for variant in variants:
        generated, source = _translate_hebrew_text_online(variant)
        if generated and not _looks_like_transliteration(generated):
            return generated, source or "automatic-translation"

    generated, source = _translate_hebrew_text_online(clean_word)
    if generated and not _looks_like_transliteration(generated):
        return generated, source or "automatic-translation"

    return "", ""


# ── Word meaning alternatives (Sefaria-first for Hebrew variants) ─────────────


def _collect_word_meaning_alternatives(raw_word, primary_meaning, word_is_hebrew):
    options = []

    def add_option(candidate):
        normalized = re.sub(r"\s+", " ", str(candidate or "").strip(" ."))
        if not normalized:
            return
        if word_is_hebrew and _looks_like_transliteration(normalized):
            return
        if normalized.lower() in {item.lower() for item in options}:
            return
        options.append(normalized)

    if word_is_hebrew:
        variants = _hebrew_word_variant_candidates(raw_word)

        for variant in variants:
            for interpreted in HEBREW_INTERPRETIVE_GLOSSARY.get(variant, []):
                add_option(interpreted)

        for candidate in _parse_meaning_candidates(primary_meaning):
            add_option(candidate)

        for variant in variants[:2]:
            lex_def, _ = _lookup_sefaria_lexicon(variant)
            if lex_def:
                add_option(lex_def)
            elif len(options) < 2:
                translated, _ = _translate_hebrew_text_online(variant)
                add_option(translated)
    else:
        for candidate in _parse_meaning_candidates(primary_meaning):
            add_option(candidate)

    return options[:3]


# ── Fill missing English lines ────────────────────────────────────────────────


def _fill_missing_english_lines(text_payload, max_lines=12, max_runtime_seconds=2.5):
    if not isinstance(text_payload, dict):
        return text_payload

    lines = text_payload.get("lines", [])
    if not isinstance(lines, list) or not lines:
        return text_payload

    translated_count = 0
    translation_sources = set()
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

        generated, source = _translate_hebrew_text_online(he_value[:320])
        if generated:
            line["en"] = generated
            translated_count += 1
            if source:
                translation_sources.add(source)

    if translated_count:
        provider_label = (
            ", ".join(sorted(translation_sources)) if translation_sources else "online-translation"
        )
        text_payload["translation_generated"] = True
        text_payload["translation_generated_count"] = translated_count
        text_payload["translation_source"] = provider_label
        text_payload["translation_note"] = (
            f"Automatic English translation added for missing lines ({provider_label})."
        )
        text_payload["en"] = [
            str(line.get("en", "")).strip()
            for line in lines
            if isinstance(line, dict) and str(line.get("en", "")).strip()
        ]

    return text_payload


# ── Flask request helpers ─────────────────────────────────────────────────────


def _parse_multi_value_arg(name):
    raw = (_flask_request.args.get(name, "") or "").strip()
    if not raw:
        return []
    return [v for chunk in raw.split(",") if (v := chunk.strip())]


def _extract_search_metadata_filters():
    metadata_filters = {}
    for key in ("era", "author", "category", "geography", "nusach"):
        values = _parse_multi_value_arg(key)
        if values:
            metadata_filters[key] = values
    return metadata_filters


# ── Answer-mode sanitizer ─────────────────────────────────────────────────────


def _sanitize_answer_mode(mode_value):
    mode = (mode_value or "balanced").strip().lower()
    return mode if mode in ANSWER_MODES else "balanced"


# ── Source attribution helpers ────────────────────────────────────────────────


def _join_with_and(values):
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])}, and {values[-1]}"


def _build_source_attribution_note(*, has_sefaria=False, has_customs=False, has_whitelisted_external=False, has_general_web=False, has_internal_knowledge=False):
    if has_internal_knowledge or not any((has_sefaria, has_customs, has_whitelisted_external, has_general_web)):
        return INTERNAL_AI_KNOWLEDGE_DISCLAIMER

    sources = []
    if has_sefaria:
        sources.append("Sefaria")
    if has_customs:
        sources.append("Community Customs")
    if has_whitelisted_external:
        sources.append("Halachipedia / HebrewBooks / YHB")
    if has_general_web:
        sources.append("General Web Context")

    joined_sources = _join_with_and(sources)
    return (
        f"Note: This information was pulled from {joined_sources}. "
        f"{RABBI_FINAL_RULING_FOOTER}"
    )


def _compact_ai_sources(sources, max_sources=8, max_lines=3, max_chars=280):
    """Trim bulky source payloads to the excerpt shape used by the UI."""
    if not isinstance(sources, list):
        return []

    compacted = []
    for src in sources[:max_sources]:
        if not isinstance(src, dict):
            continue

        ref = str(src.get("ref") or "").strip()
        title = str(src.get("title") or ref).strip()
        raw_lines_obj = src.get("lines")
        raw_lines = raw_lines_obj if isinstance(raw_lines_obj, list) else []

        lines = []
        has_valid_content = False
        for row in raw_lines[:max_lines]:
            if not isinstance(row, dict):
                continue

            # Strip Sefaria's embedded HTML (footnote/commentary-link markup) BEFORE
            # truncating by character count — truncating first risked slicing a tag
            # in half (e.g. cutting "<i data-commentary-link=...>" mid-attribute),
            # leaving an unclosed fragment the frontend's tag-stripper can't match
            # (it requires a literal closing ">"), which then rendered as visible
            # garbage text in the source box.
            en = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(row.get("en") or "")).strip())
            he = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", str(row.get("he") or "")).strip())

            # Skip lines that indicate the source was not found
            if en.startswith("Text not found") or en.startswith("Error"):
                continue

            if len(en) > max_chars:
                en = f"{en[:max_chars].rstrip()}..."
            if len(he) > max_chars:
                he = f"{he[:max_chars].rstrip()}..."

            if en or he:
                has_valid_content = True
            lines.append({"en": en, "he": he})

        # Skip sources with no valid content
        if not has_valid_content and not lines:
            continue

        domain = str(src.get("domain") or "").strip()
        source_provider = str(src.get("source_provider") or "").strip()
        url = str(src.get("url") or "").strip()

        entry: dict = {
            "ref": ref[:220],
            "title": title[:220],
            "lines": lines,
        }
        if domain:
            entry["domain"] = domain
        if source_provider:
            entry["source_provider"] = source_provider
        if url:
            entry["url"] = url

        compacted.append(entry)

    return compacted


def extract_ai_cited(structured_payload):
    """Pull the AI's own citation list out of a structured /ask payload.

    Single source of truth for both app.py and asgi.py so the two transports
    cannot drift in what counts as an "AI-cited" source (plan.md §7.14).
    """
    ai_cited = []
    if isinstance(structured_payload, dict):
        for s in (structured_payload.get("sources") or []):
            s_str = str(s or "").strip()
            if s_str:
                ai_cited.append(s_str)
    return ai_cited


# ── Community name canonicalizer ──────────────────────────────────────────────


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
