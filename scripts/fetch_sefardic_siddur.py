#!/usr/bin/env python3
"""
Fetch Sefardic Siddur from Sefaria API and generate PRAYERS_DATA for app.py
"""
import json
import requests
import time

SEFARIA_API = "https://www.sefaria.org/api/texts"

# Main Siderot (prayer services) to fetch
PRAYER_SERVICES = [
    ("Upon Arising", "Siddur Sefard, Upon Arising"),
    ("Weekday Shacharit", "Siddur Sefard, Weekday Shacharit"),
    ("Weekday Mincha", "Siddur Sefard, Weekday Mincha"),
    ("Weekday Maariv", "Siddur Sefard, Weekday Maariv"),
    ("Bedtime Shema", "Siddur Sefard, Bedtime Shema"),
    ("Kiddush Levanah", "Siddur Sefard, Kiddush Levanah"),
    ("Kiddush", "Siddur Sefard, Kiddush"),
    ("Shabbat Shacharit", "Siddur Sefard, Shabbat Shacharit"),
    ("Shabbat Mincha", "Siddur Sefard, Shabbat Mincha"),
    ("Shabbat Maariv", "Siddur Sefard, Shabbat Maariv"),
]


def fetch_from_sefaria(ref):
    """Fetch text from Sefaria API"""
    try:
        url = f"{SEFARIA_API}/{ref.replace(' ', '%20')}"
        print(f"Fetching: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error fetching {ref}: {e}")
        return None


def extract_text(data):
    """Extract Hebrew and English text from Sefaria response"""
    if not data:
        return None, None

    he_text = ""
    en_text = ""

    # Extract Hebrew text
    if "he" in data and data["he"]:
        he_lines = data["he"]
        if isinstance(he_lines, list):
            he_text = " ".join([str(line).strip()
                               for line in he_lines if line])
        else:
            he_text = str(he_lines).strip()

    # Extract English text
    if "text" in data and data["text"]:
        en_lines = data["text"]
        if isinstance(en_lines, list):
            en_text = " ".join([str(line).strip()
                               for line in en_lines if line])
        else:
            en_text = str(en_lines).strip()

    return he_text, en_text


def fetch_all_prayers():
    """Fetch all prayer services"""
    prayers_data = {}

    for prayer_name, ref in PRAYER_SERVICES:
        print(f"\n{'='*60}")
        print(f"Fetching: {prayer_name}")
        print(f"Reference: {ref}")
        print(f"{'='*60}")

        data = fetch_from_sefaria(ref)

        if data:
            he_text, en_text = extract_text(data)

            # Fallback descriptions if text is empty
            if not en_text or len(en_text) < 20:
                descriptions = {
                    "Upon Arising": "Morning blessings recited upon awakening before washing hands.",
                    "Weekday Shacharit": "Morning prayer service for weekdays, including blessings, Shema, and the Amidah.",
                    "Weekday Mincha": "Afternoon prayer service for weekdays, including the Amidah.",
                    "Weekday Maariv": "Evening prayer service for weekdays, including the Shema and Amidah.",
                    "Bedtime Shema": "Prayers recited before sleep, including forgiveness and the Shema.",
                    "Kiddush Levanah": "Blessing recited when seeing the new moon.",
                    "Kiddush": "Blessing over wine on Shabbat and holidays to sanctify the day.",
                    "Shabbat Shacharit": "Extended morning prayer service for Shabbat with additional Psalms and Piyyutim.",
                    "Shabbat Mincha": "Afternoon prayer service for Shabbat.",
                    "Shabbat Maariv": "Evening prayer service for Shabbat.",
                }
                en_text = descriptions.get(
                    prayer_name, f"{prayer_name} from Siddur Sefard (Sefardic Prayer Book)")

            if not he_text or len(he_text) < 20:
                he_text = f"סידור ספרד - {prayer_name}"

            prayers_data[prayer_name] = {
                "en": en_text[:2000],  # Limit to 2000 chars for display
                "he": he_text[:2000],   # Limit to 2000 chars for display
                # Placeholder for Arabic
                "ar": f"{prayer_name} - תרגום לערבית לא זמין כרגע",
                # Placeholder for Russian
                "ru": f"{prayer_name} - перевод на русский язык недоступен в данный момент"
            }

            print(f"✓ Successfully fetched {prayer_name}")
            print(f"  English text length: {len(en_text)} chars")
            print(f"  Hebrew text length: {len(he_text)} chars")
        else:
            print(f"✗ Failed to fetch {prayer_name}")

        # Rate limiting to avoid overwhelming the API
        time.sleep(1)

    return prayers_data


def generate_python_dict(prayers_data):
    """Generate Python dictionary syntax for app.py"""
    output = "PRAYERS_DATA = {\n"

    for prayer_name, translations in sorted(prayers_data.items()):
        output += f'    "{prayer_name}": {{\n'
        for lang in ["en", "he", "ar", "ru"]:
            text = translations[lang]
            # Escape quotes and newlines
            text_escaped = text.replace('\\', '\\\\').replace(
                '"', '\\"').replace('\n', '\\n')
            output += f'        "{lang}": "{text_escaped}",\n'
        output += "    },\n"

    output += "}\n"
    return output


if __name__ == "__main__":
    print("Starting Sefardic Siddur download...\n")

    prayers = fetch_all_prayers()

    print(f"\n{'='*60}")
    print(f"Successfully fetched {len(prayers)} prayer services")
    print(f"{'='*60}\n")

    # Generate Python code
    python_code = generate_python_dict(prayers)

    # Save to file
    with open("sefardic_prayers.py", "w") as f:
        f.write(python_code)

    print("✓ Saved to sefardic_prayers.py")

    # Also save as JSON for verification
    with open("sefardic_prayers.json", "w") as f:
        json.dump(prayers, f, indent=2, ensure_ascii=False)

    print("✓ Saved to sefardic_prayers.json")

    # Print summary
    print(f"\nPrayer Summary:")
    for prayer_name, translations in sorted(prayers.items()):
        print(
            f"  • {prayer_name}: {len(translations['en'])} chars (EN), {len(translations['he'])} chars (HE)")
