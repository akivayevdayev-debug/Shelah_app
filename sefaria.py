import requests
import re
import json
from datetime import datetime

BASE_URL = "https://www.sefaria.org/api"

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
    "shabbat": [
        "Rambam,_Mishneh_Torah,_Laws_of_Shabbat.2",
        "Rama,_Orach_Chayim.242",
    ],

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
    "kaddish": ["Shulchan_Arukh,_Orach_Chayim.56"],
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
    "taharah": ["Shulchan_Arukh,_Yoreh_De'ah.195"],
    "marriage": ["Shulchan_Arukh,_Even_HaEzer.26"],
    "divorce": ["Shulchan_Arukh,_Even_HaEzer.119"],
    "get": ["Shulchan_Arukh,_Even_HaEzer.119"],
    "ketubah": ["Shulchan_Arukh,_Even_HaEzer.66"],
    "mourning": ["Shulchan_Arukh,_Yoreh_De'ah.335"],
    "shiva": ["Shulchan_Arukh,_Yoreh_De'ah.344"],
    "death": ["Shulchan_Arukh,_Yoreh_De'ah.335"],
    "burial": ["Shulchan_Arukh,_Yoreh_De'ah.357"],
    "taharah": ["Shulchan_Arukh,_Yoreh_De'ah.366"],
    "kaddish": ["Shulchan_Arukh,_Yoreh_De'ah.376"],

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


def clean_html(text):
    """Strip HTML tags from Sefaria text"""
    if isinstance(text, list):
        return " ".join([clean_html(t) for t in text if t])
    return re.sub(r'<[^>]+>', '', str(text)).strip()


# ═══════════════════════════════════════════════════════════════════════
# MERKAVA TEXTS & HALACHOT INTEGRATION
# ═══════════════════════════════════════════════════════════════════════

MERKAVA_TEXTS = {
    # Merkava focuses on practical halacha with multiple customs
    "merkava_sources": {
        "shabbat": "https://www.merkava.com/api/halacha/shabbat",
        "kashrut": "https://www.merkava.com/api/halacha/kashrut",
        "family": "https://www.merkava.com/api/halacha/family-purity",
        "holidays": "https://www.merkava.com/api/halacha/holidays",
        "prayer": "https://www.merkava.com/api/halacha/prayer",
    },
    "merkava_customs": {
        "ashkenazi": "https://www.merkava.com/api/customs/ashkenazi",
        "sefardi": "https://www.merkava.com/api/customs/sefardi",
        "mizrahi": "https://www.merkava.com/api/customs/mizrahi",
        "yemenite": "https://www.merkava.com/api/customs/yemenite",
        "bukharian": "https://www.merkava.com/api/customs/bukharian",
    }
}


def fetch_merkava_halacha(topic):
    """Fetch halachic rulings from Merkava database"""
    try:
        if topic in MERKAVA_TEXTS["merkava_sources"]:
            url = MERKAVA_TEXTS["merkava_sources"][topic]
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                return {
                    "source": "Merkava",
                    "topic": topic,
                    "halacha": data.get("ruling", ""),
                    "explanations": data.get("details", []),
                    "customs": data.get("customs", {})
                }
    except Exception as e:
        print(f"[Merkava Error] {topic}: {e}")
    return None


def fetch_merkava_customs(community):
    """Fetch community-specific customs from Merkava"""
    try:
        if community.lower() in MERKAVA_TEXTS["merkava_customs"]:
            url = MERKAVA_TEXTS["merkava_customs"][community.lower()]
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                data = response.json()
                return {
                    "community": community,
                    "source": "Merkava",
                    "customs": data.get("practices", []),
                    "holidays_customs": data.get("holiday_practices", {})
                }
    except Exception as e:
        print(f"[Merkava Customs Error] {community}: {e}")
    return None


# ═══════════════════════════════════════════════════════════════════════
# SIDDUR KOL YAAKOV INTEGRATION
# ═══════════════════════════════════════════════════════════════════════

SIDDUR_KOL_YAAKOV = {
    "name": "Siddur Kol Yaakov",
    "description": "Comprehensive Ashkenazi prayer book with halachic rulings",
    "categories": {
        "morning": {
            "blessings": "Morning blessings (Brachot HaShachar) with halachot",
            "preparation": "Preparations for prayer (washing, dressing)",
            "korbanos": "Remembrance of Temple sacrifices",
        },
        "shacharit": {
            "pesukei_dezimrah": "Verses of praise and psalms",
            "yigdal": "Yigdal - enumeration of 13 principles of faith",
            "kaddish": "Mourner's Kaddish and other Kaddish variations",
            "shema": "Shema Yisrael and blessings",
            "amidah": "Standing prayer (Amidah) - 19 blessings",
        },
        "mincha": {
            "ashrei": "Psalms 145, 146, 147, 148, 149, 150",
            "amidah": "Afternoon Amidah",
            "tahanun": "Confessional prayers",
        },
        "maariv": {
            "blessings": "Evening blessings for the stars",
            "shema": "Shema and Al Hamitot",
            "amidah": "Evening Amidah",
        },
        "special": {
            "shabbat": "Shabbat-specific prayers and kiddush",
            "festivals": "Holiday specific prayers",
            "fast_days": "Prayers for fast days",
            "weddings": "Wedding prayers and blessings",
        },
        "kiddush": {
            "friday_night": "Kiddush for Shabbat eve",
            "saturday": "Kiddush for Shabbat day",
            "yayin": "Blessings over wine",
        },
        "havdalah": {
            "spices": "Blessings and procedure",
            "candle": "Multiple flame candle (Havdalah candle)",
            "text": "Full Havdalah service",
        },
        "halachot": {
            "concentration": "Required concentration and intent (Kavannah)",
            "timings": "Proper times for all prayers (zmanim)",
            "community": "Laws of public prayer and minyan",
        }
    }
}


def fetch_siddur_text(prayer_type, subcategory=None):
    """Fetch prayer text from Siddur Kol Yaakov structure"""
    result = {
        "source": "Siddur Kol Yaakov",
        "prayer": prayer_type,
        "tradition": "Ashkenazi",
    }

    # Return structured data about the prayer service
    if prayer_type in SIDDUR_KOL_YAAKOV["categories"]:
        category = SIDDUR_KOL_YAAKOV["categories"][prayer_type]
        result["components"] = category
        result["description"] = f"Components of {prayer_type} service"

    return result


def get_siddur_by_time(hour):
    """Return appropriate siddur service based on time of day"""
    if 4 <= hour < 6:
        return fetch_siddur_text("morning")
    elif 6 <= hour < 12:
        return fetch_siddur_text("shacharit")
    elif 12 <= hour < 16:
        return fetch_siddur_text("mincha")
    elif 16 <= hour < 20:
        return fetch_siddur_text("mincha")  # Can still daven Mincha
    else:
        return fetch_siddur_text("maariv")


# ═══════════════════════════════════════════════════════════════════════
# MISHNAH, TALMUD & EARLY SOURCES
# ═══════════════════════════════════════════════════════════════════════

ADDITIONAL_SOURCES = {
    "rambam": [
        "Rambam,_Mishneh_Torah,_Laws_of_Shabbat.1",
        "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1",
        "Rambam,_Mishneh_Torah,_Laws_of_Forbidden_Foods.1",
    ],
    "shulchan_arukh": [
        "Shulchan_Arukh,_Orach_Chayim.1",
        "Shulchan_Arukh,_Yoreh_De'ah.1",
        "Shulchan_Arukh,_Even_HaEzer.1",
        "Shulchan_Arukh,_Choshen_Mishpat.1",
    ],
    "mishnah_berurah": [
        "Mishnah_Berurah.1",
    ],
    "midrash": [
        "Midrash_Rabbah",
        "Mekhilta",
    ],
    "talmud": [
        "Talmud_Bavli",
        "Talmud_Yerushalmi",
    ],
}


def get_enhanced_sources(question):
    """Get sources from Sefaria, Merkava, and Siddur in one call"""
    sources = []

    # 1. Sefaria sources (primary)
    sefaria_texts = find_refs_for_question(question)
    for ref in sefaria_texts:
        text_data = fetch_text(ref)
        sources.append({
            "source": "Sefaria",
            "reference": ref,
            **text_data
        })

    # 2. Merkava sources (secondary)
    q_lower = question.lower()
    for topic in MERKAVA_TEXTS["merkava_sources"]:
        if any(word in q_lower for word in topic.split("_")):
            merkava_data = fetch_merkava_halacha(topic)
            if merkava_data:
                sources.append(merkava_data)

    # 3. Add relevant Siddur sections
    current_hour = datetime.now().hour
    if "pray" in q_lower or "prayer" in q_lower or "service" in q_lower:
        siddur_data = get_siddur_by_time(current_hour)
        sources.append(siddur_data)

    return sources


def fetch_text_v3(ref):
    """Fetch text using Sefaria V3 API"""
    url = f"{BASE_URL}/v3/texts/{ref}?version=english"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        versions = data.get("versions", [])
        for v in versions:
            if v.get("language") == "en" and v.get("text"):
                return clean_html(v["text"])
        return None
    except:
        return None


def fetch_text_v2(ref):
    """Fallback to Sefaria V2 API"""
    url = f"{BASE_URL}/texts/{ref}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        text = data.get("text", "")
        return clean_html(text) if text else None
    except:
        return None


def fetch_text(ref):
    """Fetch text with V3 primary, V2 fallback"""
    text = fetch_text_v3(ref)
    if not text:
        text = fetch_text_v2(ref)
    return {"ref": ref, "text": text or "Translation not available"}


def get_related_texts(ref):
    """Get linked commentaries for a ref"""
    url = f"{BASE_URL}/related/{ref}"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        links = data.get("links", [])
        # Get top 3 commentary refs
        commentary_refs = []
        for link in links[:10]:
            if link.get("category") in ["Commentary", "Halakhah", "Modern Commentary"]:
                commentary_refs.append(link.get("ref", ""))
        return commentary_refs[:3]
    except:
        return []


def get_daily_study():
    """Fetch daily study schedule from Sefaria"""
    try:
        from calendar_service import calendar_engine

        url = "https://www.sefaria.org/api/calendars"
        r = requests.get(url, timeout=5)
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
                    "ref": item.get("ref", "")
                }
            elif "Daf Yomi" in title:
                info["daf_yomi"] = {
                    "title": item.get("displayValue", {}).get("en", ""),
                    "ref": item.get("ref", "")
                }
            elif "Mishnah Yomi" in title:
                info["mishnah_yomi"] = {
                    "title": item.get("displayValue", {}).get("en", ""),
                    "ref": item.get("ref", "")
                }

        return info
    except Exception as e:
        print("[Sefaria Daily Error]", e)
        # Graceful fallback using local calendar only
        try:
            from calendar_service import calendar_engine
            hebrew_date = calendar_engine.gregorian_to_hebrew().get('hebrew_date', '')
            holiday = calendar_engine.is_holiday()
        except:
            hebrew_date = ''
            holiday = None
        return {
            "hebrew_date": hebrew_date,
            "holiday": holiday,
            "rambam": None,
            "daf_yomi": None,
            "mishnah_yomi": None,
            "offline": True
        }


def find_refs_for_question(question):
    """Match question keywords to known refs with enhanced matching"""
    q_lower = question.lower()
    matched_refs = []
    matched_keywords = []

    for keyword, refs in TOPIC_REFS.items():
        # Support both exact and partial keyword matching
        if keyword in q_lower or any(word in q_lower for word in keyword.split()):
            for ref in refs:
                if ref not in matched_refs:
                    matched_refs.append(ref)
                    matched_keywords.append(keyword)

    # Default fallback
    if not matched_refs:
        matched_refs = [
            "Shulchan_Arukh,_Orach_Chayim.1",
            "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1"
        ]

    return matched_refs[:7]  # Max 7 refs to balance coverage and token cost


def get_sources(question):
    """Main function — get all Sefaria sources for a question"""
    refs = find_refs_for_question(question)
    sources = []

    for ref in refs:
        text_data = fetch_text(ref)

        # Deep Linking: Fetch commentaries
        commentaries = []
        related_refs = get_related_texts(ref)
        for rel_ref in related_refs:
            rel_result = fetch_text(rel_ref)
            if rel_result:
                commentaries.append(rel_result)

        # Build complete result with commentaries
        result = {
            "ref": text_data["ref"],
            "text": text_data["text"],
            "commentaries": commentaries
        }
        sources.append(result)
        print(
            f"  [Sefaria] Fetched: {ref} with {len(commentaries)} commentaries")

    return sources
