"""
Product-surface pages blueprint for Sh'elah (plan.md §12.1, §12.2, §12.5.1).

Covers /about, /help, /glossary, /robots.txt, and /sitemap.xml.

robots.txt/sitemap.xml are served as Flask routes rather than physical files
in static/, matching the existing pattern for favicon.ico/manifest.webmanifest/
service-worker.js in app.py (all three are Flask routes, not root-level static
files) -- see the deferred write-up for why plan.md's "served as static files"
framing doesn't hold given vercel.json currently has no rewrites array at all.
"""

import json
import os

from flask import Blueprint, render_template, Response

from app import (
    CLERK_PUBLISHABLE_KEY,
    CLERK_ENFORCE_AUTH,
)

routes_pages = Blueprint("pages", __name__)

_SITE_BASE_URL = "https://shelah-app.vercel.app"

# plan.md §12.5.1: stable public routes only -- no per-ref library URLs, no
# /ask (personalized/dynamic), no devtools/api. A parasha page is NOT included
# here because no crawlable HTML parasha page exists yet (only the JSON
# /api/parasha endpoint) -- see the deferred write-up.
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


@routes_pages.route("/about")
def about():
    return render_template(
        "about.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_pages.route("/help")
def help_page():
    return render_template(
        "help.html",
        clerk_publishable_key=CLERK_PUBLISHABLE_KEY,
        clerk_enforce_auth=CLERK_ENFORCE_AUTH,
    )


@routes_pages.route("/glossary")
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


@routes_pages.route("/robots.txt")
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


@routes_pages.route("/sitemap.xml")
def sitemap_xml():
    urls = []
    for path, changefreq, priority in _SITEMAP_PATHS:
        urls.append(
            "  <url>\n"
            f"    <loc>{_SITE_BASE_URL}{path}</loc>\n"
            f"    <changefreq>{changefreq}</changefreq>\n"
            f"    <priority>{priority}</priority>\n"
            "  </url>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>\n"
    )
    return Response(body, mimetype="application/xml")


@routes_pages.route("/llms.txt")
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
    # route and /sitemap.xml can't silently drift apart from each other.
    lines.extend(f"- {_SITE_BASE_URL}{path}" for path, _changefreq, _priority in _SITEMAP_PATHS)
    lines.append("")
    return Response("\n".join(lines), mimetype="text/plain")
