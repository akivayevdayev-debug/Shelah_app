"""
Product-surface pages blueprint for Sh'elah.

Covers /about, /help, /glossary, /robots.txt, and /sitemap.xml.

robots.txt/sitemap.xml are served as Flask routes rather than physical files
in static/, matching the existing pattern for favicon.ico/manifest.webmanifest/
service-worker.js in app.py (all three are Flask routes, not root-level static
files). vercel.json has no rewrites array, so serving them as plain static
files at the root is not an option.
"""

import json
import os
from functools import lru_cache
from xml.sax.saxutils import escape

from flask import Blueprint, render_template, Response

from app import (
    CLERK_PUBLISHABLE_KEY,
    CLERK_ENFORCE_AUTH,
    SIDDUR_SECTION_MAP,
)
from backend import page_meta, siddur_data
from backend.helpers import COMMUNITIES

routes_pages = Blueprint("pages", __name__)

_SITE_BASE_URL = "https://shelah-app.vercel.app"

# The site's own pages -- no /ask (personalized/dynamic), no
# devtools/api. A parasha page is NOT included here because no crawlable HTML
# parasha page exists yet (only the JSON /api/parasha endpoint) -- see the
# deferred write-up. /llms.txt lists these; /sitemap.xml adds the library's
# content pages (_library_sitemap_paths) now that each has its own canonical
# (audit U-1).
_SITEMAP_PATHS = [
    ("/", "weekly", "1.0"),
    ("/about", "monthly", "0.6"),
    ("/help", "monthly", "0.6"),
    ("/glossary", "monthly", "0.6"),
    ("/terms", "yearly", "0.3"),
    ("/privacy", "yearly", "0.3"),
    ("/ai-disclosure", "yearly", "0.3"),
    ("/acceptable-use", "yearly", "0.3"),
    ("/dmca", "yearly", "0.3"),
    ("/accessibility", "yearly", "0.3"),
    ("/licenses", "yearly", "0.3"),
]


# Tanakh's chapters, book by book: mirrors CHAPTER_GRID_BOOKS in
# templates/index.html (a test keeps the two equal). The rest of the library
# has no fixed list of sections to enumerate without asking Sefaria.
_TANAKH_CHAPTERS = (
    ("Genesis", 50), ("Exodus", 40), ("Leviticus", 27), ("Numbers", 36),
    ("Deuteronomy", 34), ("Joshua", 24), ("Judges", 21), ("I Samuel", 31),
    ("II Samuel", 24), ("I Kings", 22), ("II Kings", 25), ("Isaiah", 66),
    ("Jeremiah", 52), ("Ezekiel", 48), ("Hosea", 14), ("Joel", 4), ("Amos", 9),
    ("Obadiah", 1), ("Jonah", 4), ("Micah", 7), ("Nahum", 3), ("Habakkuk", 3),
    ("Zephaniah", 3), ("Haggai", 2), ("Zechariah", 14), ("Malachi", 3),
    ("Psalms", 150), ("Proverbs", 31), ("Job", 42), ("Song of Songs", 8),
    ("Ruth", 4), ("Lamentations", 5), ("Ecclesiastes", 12), ("Esther", 10),
    ("Daniel", 12), ("Ezra", 10), ("Nehemiah", 13), ("I Chronicles", 29),
    ("II Chronicles", 36),
)


def _library_sitemap_paths():
    """The library's content pages, each at its canonical path: every
    Tanakh chapter, the fixed prayer services and the community pages.
    Static data only -- nothing here waits on Sefaria."""
    for book, chapters in _TANAKH_CHAPTERS:
        for chapter in range(1, chapters + 1):
            yield page_meta.text_path(f"{book} {chapter}"), "yearly", "0.5"
    for name in SIDDUR_SECTION_MAP:
        yield page_meta.prayer_path(name), "yearly", "0.5"
    # The siddur: its contents, every service, and each section of a
    # multi-section service (backend/siddur_data.py; checked-in, no fetch).
    yield siddur_data.siddur_path(siddur_data.DEFAULT_RITE), "monthly", "0.6"
    for _occasion, service in siddur_data.iter_services():
        yield siddur_data.siddur_path(siddur_data.DEFAULT_RITE, service["slug"]), "yearly", "0.6"
        if len(service["sections"]) > 1:
            for section in service["sections"]:
                yield siddur_data.siddur_path(siddur_data.DEFAULT_RITE, service["slug"], section["slug"]), "yearly", "0.5"
    for name in sorted(COMMUNITIES):
        yield page_meta.community_path(name), "monthly", "0.5"


@lru_cache(maxsize=1)
def _sitemap_body():
    entries = [*_SITEMAP_PATHS, *_library_sitemap_paths()]
    urls = [
        "  <url>\n"
        f"    <loc>{escape(_SITE_BASE_URL + path)}</loc>\n"
        f"    <changefreq>{changefreq}</changefreq>\n"
        f"    <priority>{priority}</priority>\n"
        "  </url>"
        for path, changefreq, priority in entries
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )


@routes_pages.route("/about", methods=["GET"])
def about():
    return render_template(
        "about.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_pages.route("/help", methods=["GET"])
def help_page():
    return render_template(
        "help.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_pages.route("/glossary", methods=["GET"])
def glossary():
    entries = []
    try:
        data_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "static", "data", "glossary.json",
        )
        with open(data_path, "r", encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        entries = []
    return render_template(
        "glossary.html",
        glossary_entries=entries,
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_pages.route("/robots.txt", methods=["GET"])
def robots_txt():
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /api/",
        "Disallow: /devtools/",
        f"Sitemap: {_SITE_BASE_URL}/sitemap.xml",
        "",
    ]
    return Response("\n".join(lines), mimetype="text/plain")


@routes_pages.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    return Response(_sitemap_body(), mimetype="application/xml")


@routes_pages.route("/llms.txt", methods=["GET"])
def llms_txt():
    lines = [
        "# Sh'elah",
        "",
        "> Sh'elah is an AI-powered Torah encyclopedia: ask a halachic or",
        "> Torah-study question in plain language and get an answer grounded",
        "> in primary sources (Talmud, Tanakh, halachic codes) via Sefaria,",
        "> with citations and awareness of differing community customs.",
        "",
        "## Pages",
        "",
    ]
    # Iterate _SITEMAP_PATHS rather than hand-duplicating its list, so this
    # route and /sitemap.xml can't silently drift apart from each other. (The
    # sitemap's ~950 library pages stay out: this is the site's own pages.)
    lines.extend(f"- {_SITE_BASE_URL}{path}" for path, _changefreq, _priority in _SITEMAP_PATHS)
    lines.append("")
    return Response("\n".join(lines), mimetype="text/plain")
