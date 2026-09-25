"""
Per-URL <head> values for the SPA shell (templates/index.html).

Every path the router owns serves the same shell, and it used to declare
itself the homepage on all of them -- one title, one og:url and
``<link rel="canonical" href="/">`` -- which tells a crawler to fold every
text and prayer page into ``/``. These builders give each URL its own title,
og:url and canonical instead.

The values are a pure function of the URL (never of cookies or the session):
the shell is sent ``Cache-Control: public`` and a CDN may hand one response
to every visitor of that URL. English only for the same reason; the client
localizes ``document.title`` after load (syncDocumentTitle in index.html).

Private views (one person's answers, their history, a shared answer) get no
canonical at all -- the route sends ``X-Robots-Tag: noindex`` and a canonical
beside it would be a mixed signal.
"""

import re
from urllib.parse import quote

SITE_BASE_URL = "https://shelah-app.vercel.app"
SITE_NAME = "Sh'elah"
SITE_TITLE = "Sh'elah - Torah Encyclopedia"
_TITLE_SEPARATOR = " · "
_HOME_DESCRIPTION = (
    "AI-powered Torah encyclopedia — search halacha, Talmud, Tanakh, and "
    "Jewish law with source citations and community-aware answers."
)
_HOME_OG_DESCRIPTION = (
    "AI-powered Torah encyclopedia with source citations from Sefaria, "
    "halachic rulings, and community customs."
)

# The query forms of the private views (router.js rewrites them to their
# paths on load, but a crawler reads the response it was sent).
PRIVATE_QUERY_KEYS = ("chat", "a", "history")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Mirrors static/js/router.js refToSlug/slugToRef: "Genesis 2" <->
# "Genesis.2", "Shulchan Arukh, Orach Chayim 345:1" <->
# "Shulchan_Arukh,_Orach_Chayim.345.1".
_SECTION = r"\d+[ab]?"
_SECTIONS = rf"{_SECTION}(?:[:.]{_SECTION})*(?:-{_SECTION}(?:[:.]{_SECTION})*)?"
_REF_SECTIONS_RE = re.compile(rf"^(.+?) ({_SECTIONS})$", re.IGNORECASE)
_SLUG_SECTIONS_RE = re.compile(rf"^(.+?)\.({_SECTIONS})$", re.IGNORECASE)


def ref_to_slug(ref):
    text = str(ref).strip()
    match = _REF_SECTIONS_RE.match(text)
    if not match:
        return text.replace(" ", "_")
    return f"{match.group(1).replace(' ', '_')}.{match.group(2).replace(':', '.')}"


def slug_to_ref(slug):
    text = str(slug).replace("_", " ")
    match = _SLUG_SECTIONS_RE.match(text)
    return f"{match.group(1)} {match.group(2).replace('.', ':')}" if match else text


def _encode_segment(value):
    # router.js encodeSegment: encodeURIComponent, then `:` and `,` unescaped.
    return quote(value, safe="!*'():,")


def _path_value(raw):
    # Flask's <path:> hands over the decoded value; a trailing slash is the
    # same view (router.js parsePath strips it too).
    return str(raw or "").rstrip("/").strip()


def _meta(title=None, description=None, canonical_path="/", og_path=None):
    full_title = f"{title}{_TITLE_SEPARATOR}{SITE_NAME}" if title else SITE_TITLE
    og_path = og_path if og_path is not None else (canonical_path or "/")
    return {
        "title": full_title,
        "description": description or _HOME_DESCRIPTION,
        "og_description": description or _HOME_OG_DESCRIPTION,
        "url": f"{SITE_BASE_URL}{og_path}",
        "canonical": f"{SITE_BASE_URL}{canonical_path}" if canonical_path else None,
    }


def home_meta():
    return _meta()


def private_meta(path="/"):
    return _meta(canonical_path=None, og_path=path)


def text_meta(raw_slug):
    ref = slug_to_ref(_path_value(raw_slug)).strip()
    if not ref:
        return home_meta()
    return _meta(
        title=ref,
        description=f"Read {ref} with sources and commentary on {SITE_NAME}, the AI-powered Torah encyclopedia.",
        canonical_path=f"/text/{_encode_segment(ref_to_slug(ref))}",
    )


def prayer_meta(raw_slug):
    name = _path_value(raw_slug).replace("_", " ").strip()
    if not name:
        return home_meta()
    return _meta(
        title=name,
        description=f"{name}: prayer text on {SITE_NAME}, the AI-powered Torah encyclopedia.",
        canonical_path=f"/prayer/{_encode_segment(name.replace(' ', '_'))}",
    )


def calendar_meta(day):
    day = _path_value(day)
    if not _DATE_RE.match(day):
        return home_meta()
    return _meta(
        title=f"Jewish calendar {day}",
        description=f"Holidays, zmanim and the Hebrew date for {day} on {SITE_NAME}.",
        canonical_path=f"/calendar/{day}",
    )


def query_meta(args):
    """``/`` and its legacy query links: a private key wins, then an old
    ``?text=`` / ``?prayer=`` link canonicalizes to its path form."""
    if any(args.get(key) for key in PRIVATE_QUERY_KEYS):
        return private_meta("/")
    if args.get("text"):
        return text_meta(ref_to_slug(args["text"]))
    if args.get("prayer"):
        return prayer_meta(args["prayer"])
    return home_meta()
