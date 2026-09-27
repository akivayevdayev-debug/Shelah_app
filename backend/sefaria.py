"""
Sefaria topic-to-reference lookup table and helpers.

Responsibilities:
- Maintain curated TOPIC_REFS mappings for common halachic queries.
- Resolve user question keywords into likely Sefaria references.
- Provide retrieval helpers consumed by app.py/data_service.py.

This file is mostly curated domain mapping data plus matching utilities.
"""

import logging
import re
from itertools import zip_longest
import requests

from backend.cache import TTLCache

logger = logging.getLogger(__name__)

_HTTP = requests.Session()
_DAILY_STUDY_CACHE_KEY = "daily_study"
# redis_prefix: this cache had NO cross-instance tier at all (unlike
# sefaria_library.py's _cache, which at least had a same-process-only disk
# tier attempt) -- every cold Vercel Fluid Compute instance recomputed the
# full Sefaria /api/calendars + pyluach Hebrew-date lookup from scratch,
# which is why /api/daily-study logs showed consistently ~800ms with no
# fast sample ever observed, unlike the wide cold/warm variance seen on
# routes backed by a real shared cache. The stored value (Sefaria calendar
# titles/refs + a Hebrew date string) is location-independent and safe to
# share across every instance/user, same as sefaria_library.py's caches.
_DAILY_STUDY_CACHE = TTLCache(ttl=60 * 5, redis_prefix="daily_study:")

# ═══════════════════════════════════════════════════════════════════════
# PRIMARY SEFARIA TEXT MAPPINGS — Over 100+ halachic references
# ═══════════════════════════════════════════════════════════════════════

TOPIC_REFS = {
    # SHABBAT & FESTIVAL LAWS
    "shabbat": [
        "Shulchan_Arukh,_Orach_Chayim.242",
        "Shulchan_Arukh,_Orach_Chayim.243",
        "Shulchan_Arukh,_Orach_Chayim.244",
        "Mishnah_Berurah.242",
        "Rambam,_Mishneh_Torah,_Laws_of_Shabbat.1",
        "Rambam,_Mishneh_Torah,_Laws_of_Shabbat.2",
        "Rama,_Orach_Chayim.242",
    ],
    "shabbos": ["Shulchan_Arukh,_Orach_Chayim.242"],
    "work on shabbat": ["Shulchan_Arukh,_Orach_Chayim.306"],
    "melacha": ["Shulchan_Arukh,_Orach_Chayim.321"],
    "35 melachot": ["Mishnah_Berurah.320"],
    "writing": ["Shulchan_Arukh,_Orach_Chayim.340"],
    "electricity": ["Shulchan_Arukh,_Orach_Chayim.252"],
    "cooking": ["Shulchan_Arukh,_Orach_Chayim.318"],
    "lighting": ["Shulchan_Arukh,_Orach_Chayim.264"],
    "travel": ["Shulchan_Arukh,_Orach_Chayim.248"],
    "muktzeh": ["Shulchan_Arukh,_Orach_Chayim.308"],

    # KASHRUT & DIETARY LAWS
    "kashrut": [
        "Shulchan_Arukh,_Yoreh_De'ah.87",
        "Shulchan_Arukh,_Yoreh_De'ah.88",
        "Shulchan_Arukh,_Yoreh_De'ah.89",
        "Rambam,_Mishneh_Torah,_Laws_of_Forbidden_Foods.1",
    ],
    "kosher": ["Shulchan_Arukh,_Yoreh_De'ah.87"],
    "treife": ["Shulchan_Arukh,_Yoreh_De'ah.194"],
    "meat": [
        "Shulchan_Arukh,_Yoreh_De'ah.87",
        "Shulchan_Arukh,_Yoreh_De'ah.98",
    ],
    "milk": ["Shulchan_Arukh,_Yoreh_De'ah.89"],
    "dairy": ["Shulchan_Arukh,_Yoreh_De'ah.88"],
    "waiting after meat": ["Shulchan_Arukh,_Yoreh_De'ah.89"],
    "fish": ["Shulchan_Arukh,_Yoreh_De'ah.103"],
    "seafood": ["Shulchan_Arukh,_Yoreh_De'ah.103"],
    "insects": ["Shulchan_Arukh,_Yoreh_De'ah.101"],
    "bugs": ["Shulchan_Arukh,_Yoreh_De'ah.101"],
    "wine": ["Shulchan_Arukh,_Yoreh_De'ah.123"],
    "yayin": ["Shulchan_Arukh,_Yoreh_De'ah.123"],

    # PASSOVER
    "pesach": [
        "Shulchan_Arukh,_Orach_Chayim.429",
        "Shulchan_Arukh,_Orach_Chayim.453",
        "Shulchan_Arukh,_Orach_Chayim.472",
        "Rambam,_Mishneh_Torah,_Laws_of_Chametz_and_Matzah.1",
    ],
    "passover": ["Shulchan_Arukh,_Orach_Chayim.429"],
    "chametz": ["Shulchan_Arukh,_Orach_Chayim.429"],
    "hametz": ["Shulchan_Arukh,_Orach_Chayim.429"],
    "kitniyot": ["Shulchan_Arukh,_Orach_Chayim.453"],
    "matzo": ["Shulchan_Arukh,_Orach_Chayim.453"],
    "matzah": ["Shulchan_Arukh,_Orach_Chayim.453"],
    "soy": ["Shulchan_Arukh,_Orach_Chayim.453"],
    "hagaddah": ["Pesach_Haggadah"],
    "haroset": ["Shulchan_Arukh,_Orach_Chayim.475"],
    "bitter herbs": ["Shulchan_Arukh,_Orach_Chayim.473"],
    "maror": ["Shulchan_Arukh,_Orach_Chayim.473"],

    # PRAYER & DEVOTIONS
    "prayer": [
        "Shulchan_Arukh,_Orach_Chayim.89",
        "Shulchan_Arukh,_Orach_Chayim.90",
        "Shulchan_Arukh,_Orach_Chayim.101",
        "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1",
    ],
    "tefillah": ["Shulchan_Arukh,_Orach_Chayim.89"],
    "davening": ["Shulchan_Arukh,_Orach_Chayim.89"],
    "shacharit": ["Shulchan_Arukh,_Orach_Chayim.89"],
    "mincha": ["Shulchan_Arukh,_Orach_Chayim.234"],
    "maariv": ["Shulchan_Arukh,_Orach_Chayim.235"],
    "shema": ["Shulchan_Arukh,_Orach_Chayim.58"],
    "amidah": ["Shulchan_Arukh,_Orach_Chayim.101"],
    "standing": ["Shulchan_Arukh,_Orach_Chayim.94"],
    "concentration": ["Shulchan_Arukh,_Orach_Chayim.98"],
    "minyan": ["Shulchan_Arukh,_Orach_Chayim.55"],
    "kaddish": [
        "Shulchan_Arukh,_Orach_Chayim.56",
        "Shulchan_Arukh,_Yoreh_De'ah.376",
    ],
    "tallit": ["Shulchan_Arukh,_Orach_Chayim.8"],
    "tallith": ["Shulchan_Arukh,_Orach_Chayim.8"],

    # RITUAL OBJECTS & MITZVOT
    "tzitzit": ["Shulchan_Arukh,_Orach_Chayim.8"],
    "tzitzis": ["Shulchan_Arukh,_Orach_Chayim.8"],
    "tefillin": ["Shulchan_Arukh,_Orach_Chayim.25"],
    "phylacteries": ["Shulchan_Arukh,_Orach_Chayim.25"],
    "mezuzah": ["Shulchan_Arukh,_Yoreh_De'ah.285"],
    "mezuzot": ["Shulchan_Arukh,_Yoreh_De'ah.285"],
    "lulav": ["Shulchan_Arukh,_Orach_Chayim.625"],
    "etrog": ["Shulchan_Arukh,_Orach_Chayim.625"],
    "sukkah": [
        "Shulchan_Arukh,_Orach_Chayim.625",
        "Rambam,_Mishneh_Torah,_Laws_of_Sukkah.1",
    ],

    # HOLIDAY LAWS
    "yom tov": ["Shulchan_Arukh,_Orach_Chayim.496"],
    "holiday": ["Shulchan_Arukh,_Orach_Chayim.495"],
    "yom kippur": ["Shulchan_Arukh,_Orach_Chayim.604", "Yom_Kippur"],
    "rosh hashana": ["Shulchan_Arukh,_Orach_Chayim.581", "Rosh_Hashanah"],
    "shofar": ["Shulchan_Arukh,_Orach_Chayim.589"],
    "sukkot": ["Shulchan_Arukh,_Orach_Chayim.625"],
    "chanukah": ["Shulchan_Arukh,_Orach_Chayim.670"],
    "hanukkah": ["Shulchan_Arukh,_Orach_Chayim.670"],
    "menorah": ["Shulchan_Arukh,_Orach_Chayim.671"],
    "purim": ["Shulchan_Arukh,_Orach_Chayim.686"],
    "shavuot": ["Shulchan_Arukh,_Orach_Chayim.494"],
    "tisha b'av": ["Shulchan_Arukh,_Orach_Chayim.554"],

    # LIFE CYCLE LAWS
    "niddah": ["Shulchan_Arukh,_Yoreh_De'ah.183"],
    "mikveh": ["Shulchan_Arukh,_Yoreh_De'ah.197"],
    "purity": ["Shulchan_Arukh,_Yoreh_De'ah.195"],
    "taharah": [
        "Shulchan_Arukh,_Yoreh_De'ah.195",
        "Shulchan_Arukh,_Yoreh_De'ah.366",
    ],
    "marriage": ["Shulchan_Arukh,_Even_HaEzer.26"],
    "divorce": ["Shulchan_Arukh,_Even_HaEzer.119"],
    "get": ["Shulchan_Arukh,_Even_HaEzer.119"],
    "ketubah": ["Shulchan_Arukh,_Even_HaEzer.66"],
    "mourning": ["Shulchan_Arukh,_Yoreh_De'ah.335"],
    "shiva": ["Shulchan_Arukh,_Yoreh_De'ah.344"],
    "death": ["Shulchan_Arukh,_Yoreh_De'ah.335"],
    "burial": ["Shulchan_Arukh,_Yoreh_De'ah.357"],

    # BUSINESS & ETHICS
    "business": ["Shulchan_Arukh,_Choshen_Mishpat.183"],
    "ribbis": ["Shulchan_Arukh,_Yoreh_De'ah.159"],
    "interest": ["Shulchan_Arukh,_Yoreh_De'ah.159"],
    "charity": ["Shulchan_Arukh,_Yoreh_De'ah.247"],
    "tzedakah": ["Shulchan_Arukh,_Yoreh_De'ah.247"],
    "honest weights": ["Shulchan_Arukh,_Choshen_Mishpat.228"],
    "theft": ["Shulchan_Arukh,_Choshen_Mishpat.348"],
    "gemzel": ["Shulchan_Arukh,_Choshen_Mishpat.348"],

    # ANIMAL SLAUGHTER
    "slaughter": ["Shulchan_Arukh,_Yoreh_De'ah.1"],
    "shechita": ["Shulchan_Arukh,_Yoreh_De'ah.1"],
    "knife": ["Shulchan_Arukh,_Yoreh_De'ah.23"],
    "glatt": ["Shulchan_Arukh,_Yoreh_De'ah.39"],

    # MEDICAL & HEALTH
    "healing": ["Shulchan_Arukh,_Yoreh_De'ah.336"],
    "medicine": ["Shulchan_Arukh,_Yoreh_De'ah.336"],
    "pikuach nefesh": ["Shulchan_Arukh,_Orach_Chayim.329"],
    "fasting": ["Shulchan_Arukh,_Orach_Chayim.550"],
    "fast": ["Shulchan_Arukh,_Orach_Chayim.550"],
}


def get_daily_study():
    """Fetch daily study schedule from Sefaria"""
    cached = _DAILY_STUDY_CACHE.get(_DAILY_STUDY_CACHE_KEY)
    if cached:
        return cached

    try:
        from backend.calendar_service import calendar_engine

        url = "https://www.sefaria.org/api/calendars"
        r = _HTTP.get(url, timeout=5)
        data = r.json()

        # Use Pyluach as primary source for Hebrew date
        hebrew_date = calendar_engine.gregorian_to_hebrew()['hebrew_date']

        info = {
            "hebrew_date": hebrew_date,
            "rambam": None,
            "daf_yomi": None,
            "mishnah_yomi": None
        }

        for item in data.get("calendar_items", []):
            title = item.get("title", {}).get("en", "")
            if "Daily Rambam" in title:
                info["rambam"] = {
                    "title": item.get("displayValue", {}).get("en", ""),
                    "title_he": item.get("displayValue", {}).get("he", ""),
                    "ref": item.get("ref", "")
                }
            elif "Daf Yomi" in title:
                info["daf_yomi"] = {
                    "title": item.get("displayValue", {}).get("en", ""),
                    "title_he": item.get("displayValue", {}).get("he", ""),
                    "ref": item.get("ref", "")
                }
            elif "Mishnah Yomi" in title:
                info["mishnah_yomi"] = {
                    "title": item.get("displayValue", {}).get("en", ""),
                    "title_he": item.get("displayValue", {}).get("he", ""),
                    "ref": item.get("ref", "")
                }

        _DAILY_STUDY_CACHE.set(_DAILY_STUDY_CACHE_KEY, info)
        return info
    except Exception as e:
        logger.warning("[Sefaria Daily Error] %s", e)
        # Graceful fallback using local calendar only
        try:
            from backend.calendar_service import calendar_engine
            hebrew_date = calendar_engine.gregorian_to_hebrew().get('hebrew_date', '')
            holiday = calendar_engine.is_holiday()
        except Exception:
            hebrew_date = ''
            holiday = None
        payload = {
            "hebrew_date": hebrew_date,
            "holiday": holiday,
            "rambam": None,
            "daf_yomi": None,
            "mishnah_yomi": None,
            "offline": True
        }
        _DAILY_STUDY_CACHE.set(_DAILY_STUDY_CACHE_KEY, payload)
        return payload


# Words of a multi-word keyword that must not match on their own: "on"
# (from "work on shabbat") is inside nearly every question and pulled
# Orach Chayim 306 into unrelated answers, and "yom"/"rosh" are shared by
# holidays that need different sources (yom tov vs yom kippur, rosh
# hashana vs rosh chodesh). The full phrase still matches.
_PARTIAL_MATCH_SKIP_WORDS = frozenset({"on", "after", "work", "waiting", "35", "yom", "rosh"})

_DEFAULT_REFS = (
    "Shulchan_Arukh,_Orach_Chayim.1",
    "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1",
)


def _starts_a_word(term, text):
    """term appears in text at the start of a word, so plurals and suffixes
    still match ("candles", "shabbat's") but "get" (divorce) no longer
    matches inside "forget", nor "fast" inside "breakfast"."""
    return re.search(r"(?<![a-z0-9'])" + re.escape(term), text) is not None


def _match_topic_refs(text):
    """Refs of every TOPIC_REFS keyword found in text (whole phrase, or any
    distinctive word of a multi-word keyword), in TOPIC_REFS order."""
    q_lower = str(text or "").lower()
    matched_refs = []
    for keyword, refs in TOPIC_REFS.items():
        if _starts_a_word(keyword, q_lower) or any(
            _starts_a_word(word, q_lower) for word in keyword.split()
            if word not in _PARTIAL_MATCH_SKIP_WORDS
        ):
            for ref in refs:
                if ref not in matched_refs:
                    matched_refs.append(ref)
    return matched_refs


def find_refs_for_question(question, context=()):
    """Match question keywords to known refs with enhanced matching.

    `context` is the conversation's earlier questions, newest first. A
    follow-up like "And what if I forgot?" names no topic of its own, so
    its sources come from what the conversation is about; when both match,
    the follow-up's refs and the context's refs alternate so neither
    crowds the other out of the capped list."""
    matched_refs = _match_topic_refs(question)
    context_refs = []
    for earlier in context or ():
        for ref in _match_topic_refs(earlier):
            if ref not in context_refs and ref not in matched_refs:
                context_refs.append(ref)
    if context_refs:
        interleaved = []
        for pair in zip_longest(matched_refs, context_refs):
            interleaved.extend(ref for ref in pair if ref)
        matched_refs = interleaved

    # Default fallback
    if not matched_refs:
        matched_refs = list(_DEFAULT_REFS)

    return matched_refs[:7]  # Max 7 refs to balance coverage and token cost
