"""
Content-hashed URLs for the ES modules under static/js (audit L-10).

There is no bundler to rename a module when it changes: main.js's
``import ... from "./router.js"`` asks for /static/js/router.js before and
after a deploy, so a cache on the way (the browser's, the CDN's, the service
worker's) can hand the new main.js an old router.js. The shell's import map
(templates/index.html) sends each of those imports to
``/static/js/router.js?v=<hash of its bytes>`` instead: a changed module is a
new URL, an unchanged one keeps its cached copy. The entry modules' <script>
tags use the same URLs, so a module that is both an entry and imported still
runs once.

Hashes are keyed by each file's mtime and size, so an edit in development
shows on the next page load without a restart. static/js ships inside the
Vercel function (Flask serves /static), so production hashes the deployed
files.

The stylesheets and classic scripts get the same treatment from ``asset_url``
(a ``?v=<hash of its bytes>`` the templates ask for by file name, in place of
a version string someone had to remember to bump). A URL carrying such a hash
names exactly one set of bytes, so ``is_content_hash`` lets the static-file
cache policy (app.py) call it immutable: cached for a year, never revalidated.
"""

import hashlib
import re
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
JS_DIR = STATIC_DIR / "js"
_URL_PREFIX = "/static/js/"
_HASH_LENGTH = 12
_HASH_RE = re.compile(rf"^[0-9a-f]{{{_HASH_LENGTH}}}$")
# A top-level static import or export: classic scripts (window globals,
# module.exports) have neither and aren't in the map.
_ES_MODULE_RE = re.compile(rb"^(?:import|export)\s", re.MULTILINE)
# Real module names only, not an editor backup or an iCloud "main 2.js" copy.
_NAME_RE = re.compile(r"^[\w.-]+\.js$")


@lru_cache(maxsize=256)
def _digest(path, mtime_ns, size):  # noqa: ARG001 -- mtime/size are the cache key
    data = path.read_bytes()
    if not _ES_MODULE_RE.search(data):
        return None
    return hashlib.sha256(data).hexdigest()[:_HASH_LENGTH]


def _module_urls():
    urls = {}
    for path in sorted(JS_DIR.glob("*.js")):
        if not _NAME_RE.match(path.name):
            continue
        try:
            stat = path.stat()
            digest = _digest(path, stat.st_mtime_ns, stat.st_size)
        except OSError:
            continue
        if digest:
            urls[path.name] = f"{_URL_PREFIX}{path.name}?v={digest}"
    return urls


def module_url(name):
    """The versioned URL of one module, for its <script type="module"> tag."""
    return _module_urls().get(name) or f"{_URL_PREFIX}{name}"


def import_map():
    """The shell's import map: each module's plain URL to its versioned one."""
    return {"imports": {f"{_URL_PREFIX}{name}": url for name, url in _module_urls().items()}}


@lru_cache(maxsize=256)
def _file_digest(path, mtime_ns, size):  # noqa: ARG001 -- mtime/size are the cache key
    return hashlib.sha256(path.read_bytes()).hexdigest()[:_HASH_LENGTH]


def asset_url(name):
    """The content-hashed URL of a file under static/ ("css/reader.css",
    "style.css", "js/actions.js"), for a template's <link> or <script src>.
    A name that is not a file there gets its plain URL, so a typo is a 404 in
    the browser rather than a render error."""
    plain = f"/static/{name}"
    path = (STATIC_DIR / name).resolve()
    try:
        path.relative_to(STATIC_DIR.resolve())
        stat = path.stat()
        return f"{plain}?v={_file_digest(path, stat.st_mtime_ns, stat.st_size)}"
    except (ValueError, OSError):
        return plain


def is_content_hash(version):
    """True for a ``v`` that is one of this module's hashes (and so names one
    set of bytes), False for a hand-written version string or nothing."""
    return bool(version) and bool(_HASH_RE.match(version))
