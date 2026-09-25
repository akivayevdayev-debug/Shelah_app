"""
Path deep links for the single-page app (deep-link Phase 5).

static/js/router.js keeps the main view in the path -- ``/text/<ref>``,
``/prayer/<name>``, ``/answer/<uuid>``, ``/a/<share token>``, ``/history``,
``/calendar/<YYYY-MM-DD>`` -- so these routes serve the SPA shell (the same
page as ``/``, app.index), and the router reads the path on load. A cold open
or a refresh on any of them paints the app instead of a 404. Only the <head>
differs: each URL gets its own title, og:url and canonical
(backend/page_meta.py).

The values aren't validated here: a malformed one still gets the shell, and
the router drops it (router.js parsePath/normalizeRoute) and shows home.

No vercel.json rewrite is needed (or wanted): Vercel's implicit routing
already sends every path to the one ASGI function with the path intact, and
a catch-all rewrite there collapsed every path to ``/api/index`` and 404'd
production twice (commits 1015e03, ef33165). /api/*, /static/*, /about and
the other real pages keep their own routes; nothing here overlaps them.
"""

from flask import Blueprint, make_response, request

from app import render_spa_shell
from backend import page_meta

routes_spa_paths = Blueprint("spa_paths", __name__)


_NOINDEX = "noindex, nofollow"
_PRIVATE_QUERY_KEYS = page_meta.PRIVATE_QUERY_KEYS


def _noindex_shell():
    response = make_response(render_spa_shell(page_meta.private_meta(request.path)))
    response.headers["X-Robots-Tag"] = _NOINDEX
    return response


@routes_spa_paths.after_app_request
def _noindex_legacy_answer_links(response):
    if request.path == "/" and any(request.args.get(key) for key in _PRIVATE_QUERY_KEYS):
        response.headers["X-Robots-Tag"] = _NOINDEX
    return response


@routes_spa_paths.route("/text/<path:ref>", methods=["GET"])
def text_path(ref):
    return render_spa_shell(page_meta.text_meta(ref))


@routes_spa_paths.route("/prayer/<path:name>", methods=["GET"])
def prayer_path(name):
    return render_spa_shell(page_meta.prayer_meta(name))


@routes_spa_paths.route("/calendar/<day>", methods=["GET"])
def calendar_path(day):
    return render_spa_shell(page_meta.calendar_meta(day))


# One person's answers, their history list, and shared answers stay out of
# search results: the shell carries no answer, but an indexed URL would
# still surface the link itself.
@routes_spa_paths.route("/answer/<entry_id>", methods=["GET"])
def answer_path(entry_id):
    return _noindex_shell()


@routes_spa_paths.route("/a/<token>", methods=["GET"])
def public_answer_path(token):
    return _noindex_shell()


@routes_spa_paths.route("/history", methods=["GET"], strict_slashes=False)
def history_path():
    return _noindex_shell()
