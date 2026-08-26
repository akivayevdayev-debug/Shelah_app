"""
Build-time glossary generator (plan.md §12.2.2).

Seeds static/data/glossary.json from the SAME lexicon lookup path the AI
assistant and the reader's word-lookup feature already use
(`backend.helpers._lookup_hebrew_word_meaning`), so a term defined on the
/glossary page and a term the AI defines inline stay consistent instead of
drifting into two independent copies (plan.md §2 reconcile-then-consolidate
rule).

The live Sefaria lexicon lookup is a network call and not guaranteed to have
an entry for every curated term, so each term also carries a hand-written
fallback gloss. A returned definition is only accepted when its source is a
real lexicon hit (the local HEBREW_WORD_GLOSSARY or the Sefaria BDB/Jastrow
lexicon) -- a low-confidence "automatic-translation" result is deliberately
rejected in favor of the curated gloss, since this is a reference page, not
a best-effort inline hover tip.

This is a build-time step, not a per-request API fan-out: the Flask route
(backend/routes_pages.py::glossary) reads the JSON file this script writes,
it never calls the lexicon engine live.

Run after editing GLOSSARY_TERMS:
    python3 scripts/generate_glossary_json.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.helpers import _lookup_hebrew_word_meaning  # noqa: E402

# Sources that count as a real lexicon hit rather than a best-effort guess.
_TRUSTED_LEXICON_SOURCES = ("local-hebrew-glossary", "sefaria", "bdb", "jastrow", "brown-driver-briggs")

# (English term, Hebrew word for lookup, curated fallback gloss).
GLOSSARY_TERMS = [
    ("Kezayit", "כזית", "The volume of an olive — a standard halachic measure for how much food must be eaten to trigger certain blessings or obligations."),
    ("Muktzeh", "מוקצה", "Objects set aside by their nature or designated use as unfit to be handled on Shabbat or Yom Tov."),
    ("Eruv", "עירוב", "A symbolic enclosure, typically of wire strung between poles, that permits carrying items in an otherwise public domain on Shabbat."),
    ("Chametz", "חמץ", "Leavened grain product, forbidden to own, eat, or benefit from during Pesach."),
    ("Tzitzit", "ציצית", "The fringes tied to the corners of a four-cornered garment, commanded in Numbers 15:38."),
    ("Kashrut", "כשרות", "The body of Jewish dietary law governing which foods may be eaten and how they must be prepared."),
    ("Minhag", "מנהג", "An accepted communal or family custom, distinct from formal codified law but often treated with comparable seriousness."),
    ("Halacha", "הלכה", "Jewish law — the practical legal conclusion derived from the Torah, Talmud, and later rabbinic authorities."),
    ("Poskim", "פוסקים", "Rabbinic decisors whose rulings determine practical halacha for a question or community."),
    ("Mitzvah", "מצוה", "A commandment; one of the 613 obligations the Torah is traditionally understood to contain."),
    ("Berakhah", "ברכה", "A blessing recited before or after performing a mitzvah, eating, or experiencing certain events."),
    ("Havdalah", "הבדלה", "The ceremony, with wine, spices, and a braided candle, marking the end of Shabbat or a festival."),
    ("Zman / Zmanim", "זמן", "A halachically defined point or window of time in the day (e.g., sunrise, sunset, the latest time for the morning Shema)."),
    ("Eruv Tavshilin", "עירוב תבשילין", "A procedure performed before a Yom Tov that falls on a Friday, permitting food preparation on the holiday for Shabbat."),
    ("Chumra", "חומרא", "A stringency a person or community adopts beyond the strict letter of the law."),
    ("Kula", "קולא", "A halachically valid leniency, as opposed to a chumra (stringency)."),
    ("Nusach", "נוסח", "The specific liturgical text and order of prayer a community follows (e.g., Nusach Ashkenaz, Nusach Sefard)."),
    ("Tevilah", "טבילה", "Ritual immersion in a mikveh (or other qualifying body of water) for purposes of ritual purity."),
    ("Get", "גט", "A formal Jewish bill of divorce, required to dissolve a Jewish marriage."),
    ("Sh'elah", "שאלה", "A halachic question posed to a rabbi — the origin of this project's name."),
]


def _is_trusted(source):
    lowered = str(source or "").lower()
    return any(token in lowered for token in _TRUSTED_LEXICON_SOURCES)


def build_glossary():
    entries = []
    for term_en, term_he, fallback in GLOSSARY_TERMS:
        definition, source = "", "curated"
        try:
            lex_def, lex_src = _lookup_hebrew_word_meaning(term_he)
        except Exception:
            lex_def, lex_src = "", ""
        if lex_def and _is_trusted(lex_src):
            definition, source = lex_def, lex_src
        else:
            definition, source = fallback, "curated"
        entries.append({
            "term_en": term_en,
            "term_he": term_he,
            "definition": definition,
            "source": source,
        })
    return entries


def main():
    entries = build_glossary()
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_path = os.path.join(repo_root, "static", "data", "glossary.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"Wrote {len(entries)} glossary entries to {out_path}")


if __name__ == "__main__":
    main()
