#!/usr/bin/env python3
"""Build the checked-in Edot HaMizrach siddur (data/siddur/edot-hamizrach/).

Why a snapshot: Sefaria asks developers to bulk-download from Sefaria-Export
instead of hammering the live API, and a siddur is a small, static,
versioned corpus -- so the app serves it from files in the repo, with no
upstream call per page (Prayers audit §2.4, research brief (a)5/(b)5). That
also makes every siddur response a pure function of its URL and lets the
service worker precache whole services for offline use.

Source: the public Sefaria-Export bucket (gs://sefaria-export, no auth):
the index schema plus one Hebrew and one English version. Only versions
whose own license field is CC0 or Public Domain are accepted; the build
fails otherwise, so a relicensed version can't slip into the repo.

  Hebrew  " Shaliehsaboo Edition"          (CC0; Sefaria's default)
  English "Sefaria Community Translation"  (CC0; aligned to the Hebrew above)

(The Public Domain "Torat Emet 357" Hebrew numbers several sections
differently, so the English -- which follows the Shaliehsaboo numbering --
would pair with the wrong lines.)

The curation below -- occasions, service order, transliterated names, URL
slugs -- is ours; the section order inside a service and every text is
Sefaria's, unmodified apart from typing and sanitizing each line
(backend/siddur_lines.py).

Usage:
  python3 scripts/build_siddur.py                  # download + build
  python3 scripts/build_siddur.py --source-dir DIR # use files already in DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.parse
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from backend.siddur_lines import build_lines, english_is_aligned  # noqa: E402

EXPORT_BASE = "https://storage.googleapis.com/sefaria-export"
INDEX_TITLE = "Siddur Edot HaMizrach"
SOURCES = {
    "schema": "schemas/Siddur_Edot_HaMizrach.json",
    "he": "json/Liturgy/Siddur/Siddur Edot HaMizrach/Hebrew/ Shaliehsaboo Edition.json",
    "en": "json/Liturgy/Siddur/Siddur Edot HaMizrach/English/Sefaria Community Translation.json",
}
ALLOWED_LICENSES = {"CC0", "Public Domain"}
OUT_DIR = REPO_ROOT / "data" / "siddur" / "edot-hamizrach"

RITE = {
    "slug": "edot-hamizrach",
    "title": {"en": "Sephardi (Edot HaMizrach)", "he": "נוסח עדות המזרח"},
}

# Occasion -> services, in the order a person reaches for them. Each service
# is (Sefaria top-level node, URL slug, transliterated name, English gloss).
OCCASIONS = [
    ("weekday", {"en": "Weekdays", "he": "ימות החול"}, [
        ("Preparatory Prayers", "hashkamat-haboker", "Hashkamat HaBoker", "On waking"),
        ("Weekday Shacharit", "shacharit", "Shacharit", "Weekday morning"),
        ("Additions for Shacharit", "tosafot-shacharit", "Tosafot LeShacharit", "After Shacharit"),
        ("Weekday Mincha", "mincha", "Mincha", "Weekday afternoon"),
        ("Weekday Arvit", "arbit", "Arbit", "Weekday evening"),
        ("Bedtime Shema", "keriat-shema-al-hamita", "Keriat Shema Al HaMita", "Bedtime Shema"),
        ("The Midnight Rite", "tikkun-chatzot", "Tikkun Chatzot", "The midnight rite"),
    ]),
    ("shabbat", {"en": "Shabbat", "he": "שבת"}, [
        ("Shabbat Candle Lighting", "hadlakat-nerot", "Hadlakat Nerot", "Candle lighting"),
        ("Song of Songs", "shir-hashirim", "Shir HaShirim", "Song of Songs"),
        ("Kabbalat Shabbat", "kabbalat-shabbat", "Kabbalat Shabbat", "Welcoming Shabbat"),
        ("Shabbat Arvit", "arbit-shabbat", "Arbit Shel Shabbat", "Friday night"),
        ("Shabbat Evening", "leil-shabbat", "Leil Shabbat", "Kiddush and the Friday night meal"),
        ("Shabbat Shacharit", "shacharit-shabbat", "Shacharit Shel Shabbat", "Shabbat morning"),
        ("Shabbat Mussaf", "musaf-shabbat", "Musaf Shel Shabbat", "Shabbat Musaf"),
        ("Daytime Meal", "seuda-sheniya", "Seuda Sheniya", "The Shabbat day meal"),
        ("Shabbat Mincha", "mincha-shabbat", "Mincha Shel Shabbat", "Shabbat afternoon"),
        ("Third Meal", "seuda-shelishit", "Seuda Shelishit", "The third meal"),
        ("Havdalah", "motzaei-shabbat", "Motzaei Shabbat", "Havdala and Saturday night"),
        ("Mishna Study for Shabbat", "mishnayot-shabbat", "Mishnayot LeShabbat", "Mishna study for Shabbat"),
    ]),
    ("festivals", {"en": "Rosh Chodesh and festivals", "he": "ראש חודש ומועדים"}, [
        ("Rosh Hodesh", "rosh-chodesh", "Rosh Chodesh", "The new month"),
        ("Blessing of the Moon", "birkat-halevana", "Birkat HaLevana", "Blessing of the moon"),
        ("Prayers for Three Festivals", "shalosh-regalim", "Shalosh Regalim", "Pesach, Shavuot, Sukkot"),
        ("Counting of the Omer", "sefirat-haomer", "Sefirat HaOmer", "Counting the Omer"),
        ("Hanukkah", "chanukah", "Chanukah", "Chanukah"),
        ("Purim", "purim", "Purim", "Purim"),
        ("Nissan", "nissan", "Chodesh Nissan", "The month of Nissan"),
        ("Fast Days and Mourning", "taaniyot", "Taaniyot VaAvelut", "Fast days and mourning"),
    ]),
    ("berachot", {"en": "Blessings", "he": "ברכות"}, [
        ("Post Meal Blessing", "birkat-hamazon", "Birkat Hamazon", "Grace after meals"),
        ("Al Hamihya", "meen-shalosh", "Me'en Shalosh", "Al Hamichya"),
        ("Blessings on Enjoyments", "birkot-hanehenin", "Birkot HaNehenin", "Blessings on food and pleasures"),
        ("Assorted Blessings and Prayers", "berachot-shonot", "Berachot Shonot", "Life events and other blessings"),
    ]),
]

# Section slugs that read better than the Sefaria key's slug.
SECTION_SLUGS = {
    "The Shema": "keriat-shema",
    "Pesukei D'Zimra": "pesukei-dezimra",
    "Hanna's Prayer": "vatitpalel-chana",
    "Traveler's Prayer": "tefillat-haderech",
}
# Words the client router reads as something else after a /siddur path
# (static/js/router.js TAIL_WORDS / TAIL_PAIRS / view segments).
RESERVED_SLUGS = {
    "mini", "overlay", "full", "balanced", "practical", "sources", "strict",
    "signin", "profile", "settings", "chat", "calendar", "text", "prayer",
    "community", "answer", "a", "history", "siddur",
}
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def slugify(title: str) -> str:
    slug = re.sub(r"['’]", "", title.strip().lower())
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def _node_title(node: dict, lang: str) -> str:
    key = "title" if lang == "en" else "heTitle"
    if node.get(key):
        return node[key].strip()
    for title in node.get("titles", []):
        if title.get("lang") == lang and title.get("primary"):
            return title["text"].strip()
    return node.get("key", "").strip()


def fetch_sources(source_dir: Path | None) -> dict:
    loaded = {}
    for name, path in SOURCES.items():
        if source_dir:
            local = source_dir / Path(path).name
            loaded[name] = json.loads(local.read_text(encoding="utf-8"))
            continue
        response = requests.get(f"{EXPORT_BASE}/{urllib.parse.quote(path)}", timeout=60)
        response.raise_for_status()
        loaded[name] = json.loads(response.content.decode("utf-8"))
    return loaded


def check_license(version: dict, lang: str) -> dict:
    license_name = str(version.get("license") or "").strip()
    if license_name not in ALLOWED_LICENSES:
        raise SystemExit(
            f"{lang} version {version.get('versionTitle')!r} is licensed {license_name!r}; "
            f"only {sorted(ALLOWED_LICENSES)} may be stored"
        )
    return {
        "title": str(version.get("versionTitle") or "").strip(),
        "license": license_name,
        "source": version.get("versionSource") or "",
    }


def build(sources: dict) -> tuple[dict, dict]:
    """Returns (toc, {service_slug: service_payload})."""
    schema = sources["schema"]["schema"]
    he_text = sources["he"]["text"]
    en_text = sources["en"]["text"]
    top = {node["key"]: node for node in schema["nodes"]}

    curated = [key for _, _, services in OCCASIONS for key, *_ in services]
    missing = sorted(set(top) - set(curated))
    unknown = sorted(set(curated) - set(top))
    if missing or unknown:
        raise SystemExit(f"curation out of sync with Sefaria: uncurated={missing} unknown={unknown}")

    toc_occasions, services_out, seen = [], {}, set()
    for occ_slug, occ_title, services in OCCASIONS:
        toc_services = []
        for key, slug, name, gloss in services:
            node = top[key]
            children = node.get("nodes") or [None]
            sections, payload_sections = [], []
            for child in children:
                sec_node = child or node
                sec_key = sec_node["key"]
                he_segments = he_text.get(key, {}).get(sec_key, []) if child else he_text.get(key, [])
                en_segments = en_text.get(key, {}).get(sec_key, []) if child else en_text.get(key, [])
                sec_slug = slug if child is None else SECTION_SLUGS.get(sec_key.strip(), slugify(sec_key))
                aligned = english_is_aligned(he_segments, en_segments)
                lines = build_lines(he_segments, en_segments if aligned else [])
                ref = f"{INDEX_TITLE}, {key}" + (f", {sec_key.strip()}" if child else "")
                sections.append({
                    "slug": sec_slug,
                    "title": {"en": _node_title(sec_node, "en"), "he": _node_title(sec_node, "he")},
                    "ref": ref,
                    "lines": len(lines),
                    "english": sum(1 for line in lines if line.get("en")),
                    **({} if aligned else {"englishOmitted": "misaligned"}),
                })
                payload_sections.append({"slug": sec_slug, "ref": ref, "lines": lines})
            for sec in sections:
                for word in (slug, sec["slug"]):
                    if not SLUG_RE.match(word) or word in RESERVED_SLUGS:
                        raise SystemExit(f"bad slug {word!r} in {key}")
            sec_slugs = [s["slug"] for s in sections]
            if len(set(sec_slugs)) != len(sec_slugs) or slug in seen:
                raise SystemExit(f"duplicate slug in {key}: {slug} {sec_slugs}")
            seen.add(slug)
            toc_services.append({
                "slug": slug,
                "title": {"en": name, "he": _node_title(node, "he")},
                "gloss": gloss,
                "sections": sections,
            })
            services_out[slug] = {"rite": RITE["slug"], "service": slug, "sections": payload_sections}
        toc_occasions.append({"slug": occ_slug, "title": occ_title, "services": toc_services})

    toc = {
        "schema": 1,
        "rite": RITE,
        "source": {
            "index": INDEX_TITLE,
            "provider": "Sefaria (sefaria.org), via the Sefaria-Export bulk dump",
            "he": check_license(sources["he"], "Hebrew"),
            "en": check_license(sources["en"], "English"),
        },
        "occasions": toc_occasions,
    }
    digest = hashlib.sha256()
    for slug in sorted(services_out):
        digest.update(json.dumps(services_out[slug], ensure_ascii=False, sort_keys=True).encode("utf-8"))
    digest.update(json.dumps(toc, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    toc["version"] = digest.hexdigest()[:12]
    for payload in services_out.values():
        payload["version"] = toc["version"]
    return toc, services_out


def _dump_service(payload: dict) -> str:
    """One line object per row, so a regenerated file diffs line by line."""
    head = {k: v for k, v in payload.items() if k != "sections"}
    rows = [json.dumps(head, ensure_ascii=False)[:-1] + ', "sections": [']
    for s_index, section in enumerate(payload["sections"]):
        rows.append(f'{{"slug": {json.dumps(section["slug"])}, "ref": {json.dumps(section["ref"], ensure_ascii=False)}, "lines": [')
        for l_index, line in enumerate(section["lines"]):
            comma = "," if l_index < len(section["lines"]) - 1 else ""
            rows.append(json.dumps(line, ensure_ascii=False) + comma)
        rows.append("]}" + ("," if s_index < len(payload["sections"]) - 1 else ""))
    rows.append("]}")
    return "\n".join(rows) + "\n"


def write(toc: dict, services: dict, out_dir: Path | None = None) -> None:
    # Resolved per call (not as a default argument, which Python binds once
    # at import) so OUT_DIR can be pointed elsewhere.
    out_dir = out_dir or OUT_DIR
    (out_dir / "services").mkdir(parents=True, exist_ok=True)
    for stale in (out_dir / "services").glob("*.json"):
        if stale.stem not in services:
            stale.unlink()
    (out_dir / "toc.json").write_text(json.dumps(toc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for slug, payload in services.items():
        (out_dir / "services" / f"{slug}.json").write_text(_dump_service(payload), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--source-dir", type=Path, help="read the export files from here instead of downloading")
    args = parser.parse_args(argv)
    toc, services = build(fetch_sources(args.source_dir))
    write(toc, services)
    lines = sum(len(s["lines"]) for p in services.values() for s in p["sections"])
    print(f"wrote {len(services)} services, {lines} lines, version {toc['version']} -> {OUT_DIR.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
