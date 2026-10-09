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

# One canonical host for the sitemap, robots.txt, llms.txt and the pages' own
# canonical/og tags.
_SITE_BASE_URL = page_meta.SITE_BASE_URL

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


# What /llms.txt says about each of the site's own pages: (section, title,
# description). Keyed by the _SITEMAP_PATHS path so the two cannot drift -- a
# test fails when a sitemap page has no entry here.
_LLMS_PAGES = {
    "/": ("Ask and study", "Sh'elah home", "Ask a halachic or Torah question in plain language; every answer cites its sources."),
    "/about": ("About", "About Sh'elah", "A solo-built Torah library with community-aware customs and an AI assistant that cites primary sources and points back to your rabbi."),
    "/help": ("About", "Help", "How to ask a good question, what the answer modes mean, reader features like Shul Mode and bookmarks, and a tour of the calendar and zmanim."),
    "/glossary": ("About", "Glossary", "Halachic terms such as kezayit, muktzeh and eruv, defined the way the AI assistant defines them."),
    "/ai-disclosure": ("About", "AI disclosure", "Which AI models are used, how retrieval works, the risk of hallucination, and what the AI does not do."),
    "/terms": ("Legal and policies", "Terms of service", "The rules, disclaimers and legal terms for using the service."),
    "/privacy": ("Legal and policies", "Privacy policy", "What data is collected, how it is used, who it is shared with, and your rights."),
    "/acceptable-use": ("Legal and policies", "Acceptable use policy", "The rules for using the service responsibly."),
    "/dmca": ("Legal and policies", "Copyright (DMCA) policy", "How to report claimed copyright infringement."),
    "/accessibility": ("Legal and policies", "Accessibility statement", "The commitment to an accessible experience, what is implemented, and how to report an issue."),
    "/licenses": ("Legal and policies", "Content licenses and credits", "Credit and license terms for the sources and libraries Sh'elah depends on."),
}


def _llms_library_links():
    """Entry points into the library -- one representative page each. The
    ~950 chapter and prayer pages are in /sitemap.xml, not repeated here."""
    return [
        (page_meta.text_path("Genesis 1"), "Tanakh reader", "Read any chapter of the Hebrew Bible, Hebrew and English side by side (shown: Genesis 1)."),
        (siddur_data.siddur_path(siddur_data.DEFAULT_RITE), "Siddur", "The prayer book by service, in the Edot HaMizrach rite."),
    ]


@routes_pages.route("/llms.txt", methods=["GET"])
def llms_txt():
    """A Markdown map of the site for language-model agents (llmstxt.org): an
    H1, a one-paragraph summary, then sections of `- [title](url): note`
    links, every URL on the canonical host."""
    sections = {}
    for path, _changefreq, _priority in _SITEMAP_PATHS:
        section, title, note = _LLMS_PAGES[path]
        sections.setdefault(section, []).append((path, title, note))
    sections.setdefault("Library", []).extend(_llms_library_links())
    sections["For crawlers"] = [("/sitemap.xml", "Sitemap", "Every public page, including each Tanakh chapter, prayer, siddur service and community.")]

    lines = [
        "# Sh'elah",
        "",
        "> Sh'elah is an AI-powered Torah encyclopedia: ask a halachic or",
        "> Torah-study question in plain language and get an answer grounded",
        "> in primary sources (Talmud, Tanakh, halachic codes) via Sefaria,",
        "> with citations and awareness of differing community customs.",
        "",
    ]
    for section, links in sections.items():
        lines += [f"## {section}", ""]
        lines += [f"- [{title}]({_SITE_BASE_URL}{path}): {note}" for path, title, note in links]
        lines.append("")
    return Response("\n".join(lines), mimetype="text/plain", headers={"Content-Type": "text/plain; charset=utf-8"})
