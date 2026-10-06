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
# PRIMARY SEFARIA TEXT MAPPINGS — hand-curated keyword -> source refs
# ═══════════════════════════════════════════════════════════════════════
#
# Every ref below was opened on Sefaria and its text read against the topic it
# is filed under (a ref filed under the wrong siman is worse than none: the
# model cites whatever it is handed). Order inside a list is most-relevant
# first. Mishneh Torah refs use Sefaria's English section titles; the Rambam's
# "Laws of Shabbat" spelling is not one Sefaria resolves (backend/ref_aliases).


def _oc(*simanim):
    return [f"Shulchan_Arukh,_Orach_Chayim.{n}" for n in simanim]


def _yd(*simanim):
    return [f"Shulchan_Arukh,_Yoreh_De'ah.{n}" for n in simanim]


def _eh(*simanim):
    return [f"Shulchan_Arukh,_Even_HaEzer.{n}" for n in simanim]


def _cm(*simanim):
    return [f"Shulchan_Arukh,_Choshen_Mishpat.{n}" for n in simanim]


def _mt(section, chapter):
    """A Mishneh Torah chapter by its Sefaria section title."""
    return f"Mishneh_Torah,_{section.replace(' ', '_')}.{chapter}"


def _same(keywords, refs):
    """One ref list filed under several spellings / languages of a topic."""
    return {keyword: list(refs) for keyword in keywords}


TOPIC_REFS = {
    # SHABBAT -- "shabbat" itself is only an umbrella (see UMBRELLA_KEYWORDS)
    **_same(("shabbat", "shabbos", "shabbes", "שבת"), [
        *_oc(242), _mt("Sabbath", 1), "Mishnah_Berurah.242"]),
    **_same(("work on shabbat", "working on shabbat", "עבודה בשבת"), [
        _mt("Sabbath", 1), _mt("Sabbath", 7)]),
    **_same(("melacha", "melachot", "מלאכה", "מלאכות"), [
        _mt("Sabbath", 7), *_oc(321)]),
    **_same(("39 melachot", "thirty-nine melachot", "thirty-nine categories"), [
        _mt("Sabbath", 7)]),
    "writing": [_mt("Sabbath", 11)],
    **_same(("electricity", "electric", "חשמל"), [*_oc(334), _mt("Sabbath", 12)]),
    **_same(("cook", "cooking", "בישול", "בשל"), _oc(318)),
    **_same(("shabbat candle", "shabbos candle", "נרות שבת"), _oc(263, 264)),
    **_same(("muktzeh", "muktzah", "muktze", "מוקצה"), _oc(308)),
    **_same(("carry", "carrying", "הוצאה"), _oc(301)),
    **_same(("kiddush", "קידוש"), _oc(271, 272)),
    **_same(("havdalah", "havdala", "הבדלה"), _oc(296)),
    **_same(("eruv", "עירוב"), _oc(366)),
    "eruv tavshilin": _oc(527),
    **_same(("three meals", "seudah shlishit", "shalosh seudos"), _oc(291)),
    "melave malka": _oc(300),
    "travel": _oc(248),
    **_same(("pikuach nefesh", "פיקוח נפש"), _oc(329)),

    # KASHRUT & DIETARY LAWS -- "kashrut"/"kosher" are umbrellas
    **_same(("kashrut", "kashrus", "kosher", "כשרות", "כשר"), [
        *_yd(87), _mt("Forbidden Foods", 1)]),
    **_same(("treife", "treif", "treifa", "terefah", "tereifah", "טרפה"), _yd(29)),
    **_same(("meat", "basar", "בשר"), _yd(87)),
    **_same(("milk", "dairy", "cheese", "chalav", "חלב"), _yd(89)),
    **_same(("meat and milk", "basar bechalav", "בשר וחלב"), _yd(87, 88, 89)),
    **_same(("waiting after meat", "wait after meat", "after eating meat", "after having meat",
             "after meat", "wait between meat", "hours after meat", "המתנה בין בשר"), _yd(89)),
    **_same(("fish", "seafood", "shellfish", "shrimp", "דגים"), _yd(83)),
    **_same(("insects", "insect", "bugs", "bug", "worms", "חרקים", "תולעים"), _yd(84)),
    **_same(("wine", "yayin", "grape juice", "יין"), _yd(123)),
    **_same(("bishul akum", "bishul yisrael", "cooked by a non-jew"), _yd(113)),
    **_same(("slaughter", "shechita", "shechitah", "shehita", "shochet", "שחיטה"), _yd(1)),
    "knife": _yd(6),
    **_same(("glatt", "lungs"), _yd(39)),
    "challah": _yd(322),
    **_same(("shatnez", "shaatnez", "kilayim", "שעטנז"), _yd(298)),

    # PASSOVER -- "pesach"/"passover" are umbrellas
    **_same(("pesach", "passover", "פסח"), [*_oc(429, 453, 472), _mt("Leavened and Unleavened Bread", 1)]),
    **_same(("chametz", "hametz", "חמץ"), [_mt("Leavened and Unleavened Bread", 1), *_oc(431)]),
    **_same(("bedikat chametz", "search for chametz", "searching for chametz"), _oc(431, 432)),
    **_same(("sell chametz", "selling chametz", "mechirat chametz", "mechiras chametz",
             "chametz after pesach", "chametz after passover", "after pesach", "after passover",
             "מכירת חמץ"), _oc(448)),
    **_same(("kitniyot", "kitniyos", "legumes", "קטניות"), _oc(453)),
    "soy": _oc(453),
    **_same(("matzah", "matzo", "matza", "matzot", "מצה", "מצות"), _oc(453, 475)),
    **_same(("seder", "סדר"), _oc(473, 475, 472)),
    **_same(("haggadah", "hagaddah", "hagadah", "haggada", "הגדה"), ["Pesach_Haggadah", *_oc(473)]),
    **_same(("four cups", "arba kosot", "ארבע כוסות"), _oc(472, 473)),
    **_same(("haroset", "charoset", "חרוסת"), _oc(475)),
    **_same(("maror", "bitter herbs", "horseradish", "מרור"), _oc(475)),

    # PRAYER & DEVOTIONS -- "prayer" is an umbrella
    **_same(("prayer", "pray", "תפילה"), [*_oc(89, 90, 101), _mt("Prayer and the Priestly Blessing", 1)]),
    **_same(("tefillah", "tefilla", "davening", "daven", "shacharit", "shacharis"), _oc(89)),
    **_same(("mincha", "minchah", "מנחה"), _oc(234)),
    **_same(("maariv", "arvit", "מעריב"), _oc(235)),
    **_same(("shema", "krias shema", "kriyat shema", "keriat shema", "קריאת שמע"), _oc(58)),
    **_same(("amidah", "shemoneh esrei", "shmoneh esrei", "עמידה"), _oc(89, 98, 101)),
    "standing": _oc(94),
    **_same(("kavanah", "kavana", "concentration", "כוונה"), _oc(98)),
    **_same(("minyan", "מניין"), _oc(55)),
    **_same(("kaddish", "קדיש"), [*_oc(56), *_yd(376)]),
    **_same(("tallit", "tallis", "tallith", "tzitzit", "tzitzis", "fringes", "ציצית", "טלית"), _oc(8)),
    **_same(("tefillin", "tfillin", "phylacteries", "תפילין"), _oc(25)),
    **_same(("mezuzah", "mezuzot", "mezuza", "מזוזה"), _yd(285)),
    **_same(("birkat kohanim", "priestly blessing", "duchening", "ברכת כהנים"), _oc(128)),
    **_same(("torah reading", "reading the torah", "kriat hatorah", "aliyah", "קריאת התורה"), _oc(135)),
    **_same(("synagogue", "shul", "beit knesset", "בית כנסת"), _oc(151, 150)),
    **_same(("hand washing", "handwashing", "washing hands", "wash hands", "netilat yadayim",
             "netilas yadayim", "נטילת ידיים"), _oc(158)),
    **_same(("hamotzi", "bread blessing", "המוציא"), _oc(167, 158)),
    **_same(("birkat hamazon", "bentching", "bentch", "benching", "grace after meals",
             "ברכת המזון"), [*_oc(184, 188), _mt("Blessings", 2)]),
    **_same(("bracha", "berachah", "berakhah", "berachot", "brachot", "blessing on",
             "blessing over", "ברכה", "ברכות"), _oc(202, 204, 167)),
    **_same(("modeh ani", "waking up", "wake up", "morning blessings", "birchot hashachar"), _oc(1, 46)),
    **_same(("asher yatzar", "bathroom blessing"), _oc(6)),
    **_same(("traveler's prayer", "travelers prayer", "tefillat haderech", "tefilas haderech"), _oc(110)),
    **_same(("bar mitzvah", "bat mitzvah", "shehecheyanu", "בר מצווה", "בר מצוה"), _oc(225)),
    **_same(("kippah", "yarmulke", "yarmulka", "kippa", "skullcap", "head covering",
             "covering head", "כיסוי ראש"), _oc(91, 2)),
    **_same(("hair covering", "cover hair", "covering hair", "tichel", "sheitel", "wig"), _eh(115)),

    # HOLIDAY LAWS
    **_same(("yom tov", "יום טוב"), _oc(495)),
    **_same(("second day", "yom tov sheni", "two days of yom tov"), _oc(496)),
    "holiday": _oc(495),
    **_same(("yom kippur", "יום כיפור"), [*_oc(611, 604), _mt("Rest on the Tenth of Tishrei", 1)]),
    **_same(("rosh hashana", "rosh hashanah", "rosh hashono", "ראש השנה"), [
        *_oc(582), _mt("Shofar, Sukkah and Lulav", 1)]),
    **_same(("shofar", "שופר"), _oc(589, 585)),
    **_same(("sukkah", "sukkot", "succot", "succah", "sukkos", "סוכה", "סוכות"), [
        *_oc(625, 639), _mt("Shofar, Sukkah and Lulav", 4)]),
    **_same(("lulav", "etrog", "arba minim", "four species", "לולב", "אתרוג"), _oc(645, 651)),
    **_same(("chanukah", "hanukkah", "chanukkah", "hanuka", "chanuka", "חנוכה"), _oc(670)),
    **_same(("menorah", "hanukkiah", "hanukiah", "chanukiah", "מנורה"), _oc(671)),
    **_same(("purim", "פורים"), _oc(695, 687)),
    **_same(("megillah", "megilla", "מגילה"), _oc(690, 687)),
    **_same(("mishloach manot", "mishloach manos", "shalach manos", "משלוח מנות"), _oc(695)),
    **_same(("fast of esther", "taanit esther", "תענית אסתר"), _oc(686)),
    **_same(("shavuot", "shavuos", "שבועות"), _oc(494)),
    **_same(("tisha b'av", "tisha bav", "ninth of av", "תשעה באב"), _oc(554)),
    **_same(("rosh chodesh", "ראש חודש"), _oc(417)),
    **_same(("fast day", "fast days", "fasting", "fast"), _oc(549, 550)),
    **_same(("omer", "sefirat haomer", "sefira", "sefirah", "counting the omer", "ספירת העומר"), _oc(489, 493)),

    # LIFE CYCLE LAWS
    **_same(("niddah", "nidah", "family purity", "taharat hamishpacha", "taharas hamishpacha",
             "נידה"), _yd(183, 195, 201)),
    **_same(("mikveh", "mikvah", "mikva", "mikvaot", "מקווה"), _yd(201, 197)),
    "purity": _yd(195),
    "taharah": _yd(195),
    **_same(("chevra kadisha", "shrouds", "tachrichim"), _yd(352)),
    **_same(("marriage", "marry", "marrying", "engagement", "engaged", "kiddushin", "קידושין"), _eh(26)),
    **_same(("wedding", "chuppah", "huppah", "חתונה", "חופה"), _eh(62, 55)),
    **_same(("sheva brachot", "seven blessings", "שבע ברכות"), _eh(62)),
    **_same(("divorce", "גירושין"), [*_eh(119), _mt("Divorce", 1)]),
    **_same(("ketubah", "ketubba", "ketuba", "ketubot", "כתובה"), _eh(66)),
    **_same(("mourning", "mourner", "aveilut", "avel", "אבלות"), _yd(375, 380)),
    **_same(("shiva", "shivah", "שבעה"), _yd(375, 380)),
    **_same(("kriah", "rending", "keriah", "קריעה"), _yd(340)),
    **_same(("yahrzeit", "yahrtzeit", "יארצייט"), _yd(402)),
    **_same(("burial", "bury", "buried", "funeral", "קבורה"), _yd(357)),
    "autopsy": _yd(349),
    **_same(("visiting the sick", "bikur cholim", "ביקור חולים"), _yd(335)),
    "death": _yd(339),
    **_same(("healing", "medicine", "doctor"), _yd(336)),
    **_same(("brit milah", "bris", "circumcision", "milah", "mohel", "ברית מילה", "ברית"), [
        *_yd(260, 262), _mt("Circumcision", 1)]),
    **_same(("pidyon haben", "פדיון הבן"), _yd(305)),
    **_same(("conversion", "convert", "converting", "geirut", "giyur", "גיור"), _yd(268)),
    **_same(("tattoo", "tattoos", "קעקע"), _yd(180)),
    **_same(("shaving", "beard", "payot", "peyot", "sideburns", "גילוח"), _yd(181)),
    **_same(("honor parents", "honoring parents", "respect parents", "kibbud av", "kibud av",
             "כיבוד אב ואם"), _yd(240)),
    **_same(("torah study", "learning torah", "talmud torah", "תלמוד תורה"), [
        *_yd(246), _mt("Torah Study", 1)]),

    # BUSINESS & ETHICS
    "business": _cm(227),
    **_same(("onaah", "ona'ah", "overcharging", "fraud", "price gouging", "אונאה"), _cm(227)),
    **_same(("honest weights", "weights and measures", "משקלות"), _cm(231)),
    **_same(("theft", "steal", "stealing", "stole", "robbery", "gezel", "gezeilah", "גניבה", "גזל"), _cm(348, 359)),
    **_same(("lost object", "lost item", "found a wallet", "hashavat aveidah", "השבת אבידה"), _cm(259)),
    **_same(("ribbis", "ribit", "usury", "lending with interest", "ריבית"), _yd(160)),
    "interest": _yd(160),
    **_same(("charity", "tzedakah", "tzedaka", "tithing", "maaser", "צדקה"), _yd(247, 248)),
    **_same(("lashon hara", "loshon hora", "gossip", "slander", "לשון הרע"), [
        _mt("Human Dispositions", 7)]),
}

# Keywords that name a whole area. Their refs are an overview, not an answer:
# a question with any more specific hit ("cook" on shabbat, "meat" in kashrut)
# gets that hit's refs instead of the area's, and a bare "tell me about
# Shabbat" gets the overview.
UMBRELLA_KEYWORDS = frozenset({
    "shabbat", "shabbos", "shabbes", "שבת",
    "kashrut", "kashrus", "kosher", "כשרות", "כשר",
    "pesach", "passover", "פסח",
    "prayer", "pray", "תפילה",
})

# Everyday English words that are also halachic topics. Matched as whole words
# and only in a question that is recognisably about Judaism, so "get", "how
# fast", "interesting", "business trip" or "standing desk" never pull in a
# source ("interest" once matched "interested").
_AMBIGUOUS_KEYWORDS = frozenset({
    "fast", "fasting", "interest", "standing", "concentration", "travel",
    "knife", "soy", "business", "holiday", "death", "healing", "medicine",
    "doctor", "purity", "carry", "carrying", "writing", "marry", "marrying",
    "engaged", "engagement",
})

# Shabbat-only topics: their refs say nothing about Yom Tov cooking or a
# weekday task, so they apply only when the question mentions Shabbat.
_SHABBAT_ONLY_KEYWORDS = frozenset({
    "cook", "cooking", "בישול", "בשל", "electricity", "electric", "חשמל", "writing",
    "carry", "carrying", "הוצאה", "muktzeh", "muktzah", "muktze", "מוקצה",
})

_TORAH_CONTEXT_RE = re.compile(
    r"(?<![a-z0-9'])(?:jew\w*|judaism|halach\w*|halakh\w*|torah|talmud\w*|mishn\w*|"
    r"rabbi\w*|rav|mitzv\w*|kosher|kashr\w*|treif\w*|shabbat\w*|shabbos|shabbes|"
    r"yom|pesach|passover|sukkot|succot|chanuk\w*|hanuk\w*|purim|rosh|kippur|shul|"
    r"synagogue|minyan|kohen|orthodox|chassid\w*|sephardi\w*|ashkenaz\w*|shulchan|"
    r"mezuz\w*|tefillin|tzitzit|bris|kaddish|niddah|mikveh|kiddush|havdalah|"
    r"seder|chametz|megillah|shofar|lulav|etrog|sefaria|hashem|davening|daven)"
    r"|[֐-׿]"
)
_SHABBAT_CONTEXT_RE = re.compile(
    r"(?<![a-z0-9'])(?:shabbat|shabbos|shabbes|sabbath|saturday|friday night)|שבת"
)

# Distinctive single words of a multi-word keyword that stand for it on their
# own ("kippur" for "yom kippur"). The generic words of such a keyword ("on",
# "after", "work", "shabbat", "meat", "yom", "rosh", "grace", "torah"...) never
# do: they are inside nearly every question and pulled unrelated refs in.
_PARTIAL_MATCH_WORDS = frozenset({
    "kippur", "hashana", "hashanah", "chodesh", "nefesh", "pikuach", "tisha",
    "melachot", "hamazon", "tavshilin", "herbs", "manot", "manos",
})

_MAX_REFS = 7  # Max 7 refs to balance coverage and token cost


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


_HEBREW_LETTER = "א-ת"
_HEBREW_PREFIXES = "ובהכלמש"
# Words that are halachic topics only as whole words ("shul" is not "shulchan",
# "bris" is not "brisket").
_WHOLE_WORD_KEYWORDS = _AMBIGUOUS_KEYWORDS | {"shul", "bris", "wig", "bug", "bugs"}


def _term_regex(term, whole=False):
    """Compiled pattern for one keyword. English terms match at the start of a
    word, so plurals and suffixes still match ("candles", "shabbat's") but
    "get" no longer matches inside "forget", nor "fast" inside "breakfast";
    `whole` also requires the word to end there ("interest" is not
    "interested"). Hebrew terms may carry a one- or two-letter prefix (the
    "ב" of "בשבת") and a short suffix, and nothing longer."""
    words = r"\s+".join(re.escape(word) for word in term.split())
    if re.search(f"[{_HEBREW_LETTER}]", term):
        return re.compile(
            f"(?<![{_HEBREW_LETTER}])[{_HEBREW_PREFIXES}]{{0,2}}{words}"
            f"[{_HEBREW_LETTER}]{{0,2}}(?![{_HEBREW_LETTER}])")
    pattern = r"(?<![a-z0-9'])" + words
    if whole:
        pattern += r"(?:s|es)?(?![a-z0-9'])"
    return re.compile(pattern)


# keyword -> (pattern for the whole keyword, patterns for the distinctive
# single words of a multi-word keyword that may stand for it)
_MATCHERS = {
    keyword: (
        _term_regex(keyword, whole=keyword in _WHOLE_WORD_KEYWORDS),
        tuple(
            _term_regex(word) for word in keyword.split()
            if " " in keyword and word in _PARTIAL_MATCH_WORDS
        ),
    )
    for keyword in TOPIC_REFS
}


def _keyword_matches(keyword, text, torah_context, on_shabbat):
    """"phrase" when the whole multi-word keyword is in text, "word" for a
    single-word keyword (or a distinctive word of a multi-word one), else
    None. Ambiguous and Shabbat-only keywords also need their context."""
    if keyword in _AMBIGUOUS_KEYWORDS and not torah_context:
        return None
    if keyword in _SHABBAT_ONLY_KEYWORDS and not on_shabbat:
        return None
    whole_pattern, partial_patterns = _MATCHERS[keyword]
    if whole_pattern.search(text):
        return "phrase" if " " in keyword else "word"
    if any(pattern.search(text) for pattern in partial_patterns):
        return "word"
    return None


def _round_robin(ref_lists):
    """The lists' refs, one from each in turn: with a cap on how many are
    fetched, a topic with five refs must not push out a second topic's one."""
    ordered = []
    for group in zip_longest(*ref_lists):
        for ref in group:
            if ref and ref not in ordered:
                ordered.append(ref)
    return ordered


def _match_topic_refs(text):
    """Refs for the topics named in text, most specific first: a multi-word
    topic ("waiting after meat"), then single-word topics, and the umbrella
    keywords ("shabbat", "kosher") only when nothing more specific matched.
    A matched phrase stands in for its own words, so "eruv tavshilin" does not
    also bring the refs of "eruv". Empty when text names no topic."""
    lowered = str(text or "").lower().replace("’", "'").replace("‘", "'")
    torah_context = _TORAH_CONTEXT_RE.search(lowered) is not None
    on_shabbat = _SHABBAT_CONTEXT_RE.search(lowered) is not None

    phrases, words, umbrellas = [], [], []
    for keyword in TOPIC_REFS:
        kind = _keyword_matches(keyword, lowered, torah_context, on_shabbat)
        if kind is None:
            continue
        if keyword in UMBRELLA_KEYWORDS:
            umbrellas.append(keyword)
        elif kind == "phrase":
            phrases.append(keyword)
        else:
            words.append(keyword)

    covered = {word for phrase in phrases for word in phrase.split()}
    specific = phrases + [keyword for keyword in words if keyword not in covered]
    return _round_robin(TOPIC_REFS[keyword] for keyword in (specific or umbrellas))


def find_refs_for_question(question, context=()):
    """Match question keywords to known refs; empty when the question names
    no curated topic. There is deliberately no default: the old fallback
    (Orach Chayim 1 and the Rambam's Laws of Prayer) was handed to every
    unmatched question and the model cited it, so the sources shown had
    nothing to do with the question.

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

    return matched_refs[:_MAX_REFS]
