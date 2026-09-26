"""
Path deep links for the single-page app (deep-link Phase 5).

static/js/router.js keeps the main view in the path -- ``/text/<ref>``,
``/prayer/<name>``, ``/community/<name>``, ``/answer/<uuid>``,
``/a/<share token>``, ``/history``, ``/calendar/<YYYY-MM-DD>`` -- so these routes serve the SPA shell (the same
page as ``/``, app.index), and the router reads the path on load. A cold open
or a refresh on any of them paints the app instead of a 404. Only the <head>
differs: each URL gets its own title, og:url and canonical
(backend/page_meta.py).

Formal URLs: everything the app writes is a path segment, so after a view
comes a tail of overlays and AI keys (router.js "Paths"): ``chat/<id>``,
a community slug and an AI mode, ``full``/``mini``, ``calendar/<day>``, and
``signin``/``profile``/``settings`` -- e.g. ``/chat/new/sefardic/strict``,
``/text/Genesis.1/chat/<id>/mini``, ``/signin``. The tail is checked here with
the router's own grammar (_parse_tail), so a segment that fits nowhere is a
real 404 rather than an endless supply of soft-404 shells. The view values
themselves aren't validated: a malformed one still gets the shell, and the
router drops it (router.js parsePath/normalizeRoute) and shows home. The
one exception is a text the reader has already found doesn't exist
(sefaria_library.is_known_missing_text, never a Sefaria request here): its
shell comes with a real 404 status, kept out of search results.

No vercel.json rewrite is needed (or wanted): Vercel's implicit routing
already sends every path to the one ASGI function with the path intact, and
a catch-all rewrite there collapsed every path to ``/api/index`` and 404'd
production twice (commits 1015e03, ef33165). /api/*, /static/*, /about and
the other real pages keep their own routes; nothing here overlaps them.
"""

import re
from urllib.parse import quote

from flask import Blueprint, abort, make_response, redirect, request

from app import render_spa_shell
from backend import page_meta, sefaria_library

routes_spa_paths = Blueprint("spa_paths", __name__)


_NOINDEX = "noindex, nofollow"
_PRIVATE_QUERY_KEYS = page_meta.PRIVATE_QUERY_KEYS

# The path views whose value follows the prefix. A trailing slash on any of
# them (or on /history) is the same page, and one URL per page is what
# crawlers and shared links should see.
_VALUE_PREFIXES = ("/text/", "/prayer/", "/community/", "/calendar/", "/answer/", "/a/", "/chat/")
# RFC 3986 path characters left as they are when re-encoding the decoded path.
_PATH_SAFE = "/:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?%"


# router.js parsePath's tail grammar (TAIL_PAIRS / TAIL_WORDS / MINHAG_RE).
_TAIL_PAIRS = {"chat": "conversation", "calendar": "date"}
_TAIL_WORDS = {
    **{size: "cv" for size in ("mini", "overlay", "full")},
    **{mode: "mode" for mode in ("balanced", "practical", "sources", "strict")},
    **{page: "auth" for page in ("signin", "profile", "settings")},
}
_MINHAG_RE = re.compile(r"^[A-Za-z][A-Za-z-]{0,39}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_tail(parts, beside_answer=False):
    """The keys a path's tail sets, or None when a segment fits nowhere.

    ``beside_answer``: the view is an answer (/answer, /a), which a
    community or mode may follow without a conversation.
    """
    keys = {}
    i = 0
    while i < len(parts):
        word = parts[i]
        if word in _TAIL_PAIRS:
            key = _TAIL_PAIRS[word]
            value = parts[i + 1] if i + 1 < len(parts) else ""
            if not value or (key == "date" and not _DATE_RE.match(value)):
                return None
            i += 2
        elif word.lower() in _TAIL_WORDS:
            key, value = _TAIL_WORDS[word.lower()], word.lower()
            i += 1
        elif _MINHAG_RE.match(word):
            key, value = "minhag", word
            i += 1
        else:
            return None
        if key in keys:
            return None
        keys[key] = value
    if "cv" in keys and "conversation" not in keys:
        return None
    if ("minhag" in keys or "mode" in keys) and "conversation" not in keys and not beside_answer:
        return None
    return keys


def _view_and_tail(raw, beside_answer=False):
    """``Genesis.1/chat/c1/mini`` -> ("Genesis.1", {...}).

    The router writes a view value as one segment (a `/` in it is escaped),
    but Flask hands the path over decoded, so ``/text/a%2Fb`` arrives as
    ``a/b``: the value is the shortest run of leading segments that leaves a
    tail the grammar reads. An answer id or share token never has a `/`, so
    after an answer (``beside_answer``) the value is the first segment.
    """
    parts = str(raw or "").rstrip("/").split("/")
    for cut in range(1, 2 if beside_answer else len(parts) + 1):
        keys = _parse_tail(parts[cut:], beside_answer)
        if keys is not None:
            return "/".join(parts[:cut]), keys
    abort(404)


def _noindex_shell(status=200):
    response = make_response(render_spa_shell(page_meta.private_meta(request.path)), status)
    response.headers["X-Robots-Tag"] = _NOINDEX
    return response


@routes_spa_paths.before_app_request
def _redirect_trailing_slash():
    """``/text/Genesis.1/`` 308s to ``/text/Genesis.1``, query kept.

    308 rather than 301: permanent, and it may never turn the request into
    something else. A prefix with nothing after it (``/text/``) is left to
    404 as before.
    """
    if request.method not in ("GET", "HEAD") or not request.path.endswith("/"):
        return None
    bare = request.path.rstrip("/")
    if bare != "/history" and not any(bare.startswith(p) and len(bare) > len(p) for p in _VALUE_PREFIXES):
        return None
    location = quote(bare, safe=_PATH_SAFE)
    query = request.query_string.decode("utf-8", "replace")
    if query:
        location += "?" + quote(query, safe=_QUERY_SAFE)
    return redirect(location, code=308)


@routes_spa_paths.after_app_request
def _noindex_legacy_answer_links(response):
    if request.path == "/" and any(request.args.get(key) for key in _PRIVATE_QUERY_KEYS):
        response.headers["X-Robots-Tag"] = _NOINDEX
    return response


@routes_spa_paths.route("/text/<path:ref>", methods=["GET"])
def text_path(ref):
    ref, _ = _view_and_tail(ref)
    if sefaria_library.is_known_missing_text(page_meta.text_ref(ref)):
        return _noindex_shell(404)
    return render_spa_shell(page_meta.text_meta(ref))


@routes_spa_paths.route("/prayer/<path:name>", methods=["GET"])
def prayer_path(name):
    name, _ = _view_and_tail(name)
    return render_spa_shell(page_meta.prayer_meta(name))


@routes_spa_paths.route("/community/<path:name>", methods=["GET"])
def community_path(name):
    name, _ = _view_and_tail(name)
    return render_spa_shell(page_meta.community_meta(name))


@routes_spa_paths.route("/calendar/<path:rest>", methods=["GET"])
def calendar_path(rest):
    day, *tail = rest.rstrip("/").split("/")
    if _parse_tail(tail) is None:
        abort(404)
    return render_spa_shell(page_meta.calendar_meta(day))


# One person's answers, their history list, their conversations and shared
# answers stay out of search results: the shell carries no answer, but an
# indexed URL would still surface the link itself.
@routes_spa_paths.route("/answer/<path:rest>", methods=["GET"])
def answer_path(rest):
    _view_and_tail(rest, beside_answer=True)
    return _noindex_shell()


@routes_spa_paths.route("/a/<path:rest>", methods=["GET"])
def public_answer_path(rest):
    _view_and_tail(rest, beside_answer=True)
    return _noindex_shell()


@routes_spa_paths.route("/chat/<path:rest>", methods=["GET"])
def conversation_path(rest):
    if _parse_tail(["chat", *rest.rstrip("/").split("/")]) is None:
        abort(404)
    return _noindex_shell()


@routes_spa_paths.route("/history", methods=["GET"], strict_slashes=False)
@routes_spa_paths.route("/history/<path:rest>", methods=["GET"])
def history_path(rest=""):
    if rest.strip("/") and _parse_tail(rest.strip("/").split("/")) is None:
        abort(404)
    return _noindex_shell()


# The Clerk sign-in modal over the home page (router.js `auth`). /profile
# and /settings predate the router and stay on app.index.
@routes_spa_paths.route("/signin", methods=["GET"])
def signin_path():
    return _noindex_shell()
