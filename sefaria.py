"""
Sefaria topic-to-reference lookup table and helpers.

Responsibilities:
- Maintain curated TOPIC_REFS mappings for common halachic queries.
- Resolve user question keywords into likely Sefaria references.
- Provide retrieval helpers consumed by app.py/data_service.py.

This file is mostly curated domain mapping data plus matching utilities.
"""

import requests

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

    for keyword, refs in TOPIC_REFS.items():
        # Support both exact and partial keyword matching
        if keyword in q_lower or any(word in q_lower for word in keyword.split()):
            for ref in refs:
                if ref not in matched_refs:
                    matched_refs.append(ref)

    # Default fallback
    if not matched_refs:
        matched_refs = [
            "Shulchan_Arukh,_Orach_Chayim.1",
            "Rambam,_Mishneh_Torah,_Laws_of_Prayer.1"
        ]

    return matched_refs[:7]  # Max 7 refs to balance coverage and token cost
