"""Commentary-sidebar bundle: slim linked-commentary refs plus preloaded text.

The reader's commentary sidebar used to make the browser pull Sefaria's whole
``/related`` payload for a verse (Genesis 1:1: ~700 KB, ~4 MB for a chapter),
throw away everything but the commentary/targum/midrash refs, and only then
start fetching the text of whichever commentator the reader picked -- plus a
second, sequential request for its English translation. Every step began after
the reader had already selected text.

This module serves the same data in two small, server-cached stages so the
client can load it for the verse being read, before anything is selected:

* ``build_sidebar_links(ref)`` -- the commentary / targum / midrash refs in the
  shape ``/api/text/<ref>/links`` uses (so the client's existing grouping code
  reads it unchanged), minus everything the sidebar never shows.
* ``build_sidebar_texts(ref)`` -- the text of the first few passages of the
  most-read commentators on that verse, with an English translation filled in
  for the first of them, fetched in parallel.

Everything is cached per ref, concurrent requests for one ref share a single
upstream fan-out, and nothing here touches the event loop: the Flask routes
that call it already run on the WSGI thread pool.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, wait

from backend.cache import TTLCache

logger = logging.getLogger(__name__)

# Commentators readers look for first. Keep in step with COMMENTARY_PRIORITY in
# templates/index.html -- the client orders the chooser by it, the server uses
# it to decide whose text to preload (tests/test_sidebar_bundle.py compares).
COMMENTARY_PRIORITY = (
    "Rashi", "Onkelos", "Ramban", "Ibn Ezra", "Rashbam", "Sforno", "Radak", "Or HaChaim",
    "Kli Yakar", "Ba'al HaTurim", "Rabbeinu Bahya", "Chizkuni", "Da'at Zekenim", "Abarbanel",
    "Malbim", "Haamek Davar", "Siftei Chakhamim", "Mizrachi", "Gur Aryeh", "Torah Temimah",
    "Rav Hirsch", "Shadal", "Steinsaltz",
    "Bartenura", "Tosafot Yom Tov", "Rambam", "Ikar Tosafot Yom Tov", "Tosafot", "Rashba",
    "Ritva", "Maharsha", "Mishnah Berurah", "Magen Avraham", "Turei Zahav", "Ba'er Hetev",
    "Beur HaGra", "Sha'arei Teshuvah",
)
_PRIORITY_RANK = {name.lower(): index for index, name in enumerate(COMMENTARY_PRIORITY)}

# Safety bound only, same as COMMENTARY_REF_LIMIT on the client: a Talmud daf
# can carry thousands of links.
LINK_REF_LIMIT = 800

# How much text to preload. A verse's commentary text is read one passage at a
# time, so a few passages of the first few commentators cover the likely
# first taps; anything else loads on demand exactly as before.
PRELOAD_AUTHORS = 4
PRELOAD_REFS_PER_AUTHOR = 3
# Machine translation is the one expensive step (a Google round trip per
# line, see backend/helpers.py::_translate_hebrew_text_online), so only the
# first few passages -- the ones a reader opens first -- are translated here.
TRANSLATE_REFS = 3
TRANSLATE_MAX_LINES = 8
TRANSLATE_MAX_SECONDS = 1.0
# A single huge commentary (Ramban on Genesis 1:1 is ~170 KB) isn't worth
# pushing down speculatively.
MAX_TEXT_BYTES = 60_000
MAX_BUNDLE_TEXT_BYTES = 160_000
# Wall-clock bound on the whole texts stage. Passages still running when it
# elapses are left out (they finish into Sefaria's own cache for next time).
TEXTS_DEADLINE_SECONDS = 3.0
MAX_REF_LENGTH = 200

_ONKELOS_BOOK_RE = re.compile(r"^Onkelos\s+(?:Genesis|Exodus|Leviticus|Numbers|Deuteronomy)$", re.IGNORECASE)
# Trailing "2a", "1:5", "1.2", "3-4" style location a work's ref ends with.
_LOCATION_TOKEN_RE = re.compile(r"^\d+[ab]?(?:[:.]\d+[ab]?)*(?:-\d+(?:[:.]\d+)*)?$", re.IGNORECASE)
_DIGIT_RUN_RE = re.compile(r"(\d+)")

_links_cache = TTLCache(maxsize=512, ttl=3600)
_texts_cache = TTLCache(maxsize=128, ttl=1800)


# ── Links ─────────────────────────────────────────────────────────────────────


def _category_rank(category):
    """Commentary before Targum before Midrash; None for everything the
    sidebar doesn't list. "Quoting Commentary" is works that cite this verse
    while commenting on another one, not a commentary on it. Mirrors
    categoryRank in extractCommentaryRefs (templates/index.html)."""
    name = str(category or "")
    lowered = name.lower()
    if "quoting" in lowered:
        return None
    if lowered == "commentary":
        return 0
    if "commentary" in lowered or "supercommentary" in lowered or "parshan" in lowered:
        return 1
    if "targum" in lowered:
        return 2
    if "midrash" in lowered:
        return 3
    return None


def _slim_link(item):
    ref = str(item.get("ref") or "").strip()
    if not ref:
        return None
    entry = {"ref": ref}
    he_ref = str(item.get("heRef") or "").strip()
    if he_ref:
        entry["heRef"] = he_ref
    collective = item.get("collectiveTitle")
    if isinstance(collective, dict):
        title = {k: str(collective.get(k) or "") for k in ("en", "he") if collective.get(k)}
        if title:
            entry["collectiveTitle"] = title
    return entry


def slim_links(grouped):
    """Reduce Sefaria's grouped links to the commentary/targum/midrash refs
    the sidebar lists, in the order and with the de-duplication the client's
    extractCommentaryRefs applies, in the same ``{category: [item]}`` shape."""
    if not isinstance(grouped, dict):
        return {}
    ranked = []
    for category, items in grouped.items():
        rank = _category_rank(category)
        if rank is not None and isinstance(items, list):
            ranked.append((rank, category, items))
    ranked.sort(key=lambda entry: entry[0])

    slim = {}
    seen = set()
    total = 0
    for _, category, items in ranked:
        kept = []
        for item in items:
            if total >= LINK_REF_LIMIT:
                break
            entry = _slim_link(item) if isinstance(item, dict) else None
            if entry is None:
                continue
            key = entry["ref"].lower()
            if key in seen:
                continue
            seen.add(key)
            kept.append(entry)
            total += 1
        if kept:
            slim[category] = kept
    return slim


def build_sidebar_links(ref):
    """``{"ref", "links"}`` for one verse/passage, cached."""
    from backend.sefaria_library import get_linked_texts

    key = ref.lower()
    cached = _links_cache.get(key)
    if cached is not None:
        return cached

    def compute():
        return {"ref": ref, "links": slim_links(get_linked_texts(ref))}

    result = _single_flight(("links", key), compute)
    # An empty result may be a transient upstream failure: don't pin it.
    if result["links"]:
        _links_cache.set(key, result)
    return result


# ── Which commentators to preload ─────────────────────────────────────────────


def _author_base(entry):
    """The work a link belongs to, so every passage of Rashi groups together.
    Mirrors getCommentaryAuthorBase (templates/index.html)."""
    title = str((entry.get("collectiveTitle") or {}).get("en") or "").strip()
    if title:
        return _ONKELOS_BOOK_RE.sub("Onkelos", title)

    raw = str(entry.get("ref") or "").strip()
    head = raw.split(",", 1)[0]
    tokens = head.split()
    for index in range(1, len(tokens) - 1):
        if tokens[index].lower() == "on":
            return " ".join(tokens[:index])
    parts = head.strip().rsplit(None, 1)
    if len(parts) == 2 and _LOCATION_TOKEN_RE.match(parts[1]):
        return parts[0].strip() or head.strip()
    return head.strip()


def _commentary_bucket(ref):
    """0 = "X on <passage>", 1 = targum, 2 = everything else (same buckets
    the client's chooser sorts by)."""
    tokens = ref.lower().split()
    if "on" in tokens[1:-1]:
        return 0
    if tokens and tokens[0].strip(",") in {"onkelos", "targum", "tafsir"}:
        return 1
    return 2


def _natural_key(value):
    return [int(part) if part.isdigit() else part.lower() for part in _DIGIT_RUN_RE.split(value)]


def pick_preload_refs(links, authors=PRELOAD_AUTHORS, per_author=PRELOAD_REFS_PER_AUTHOR):
    """The first few passages of the most-read commentators present in
    ``links``, most-read first. Only commentators in COMMENTARY_PRIORITY are
    preloaded -- the long tail is read rarely and loads on demand."""
    by_author = {}
    for items in links.values():
        for entry in items:
            base = _author_base(entry).lower()
            if base not in _PRIORITY_RANK:
                continue
            slot = by_author.setdefault(
                base, {"rank": _PRIORITY_RANK[base], "bucket": _commentary_bucket(entry["ref"]), "refs": []})
            slot["refs"].append(entry["ref"])

    picked = []
    for slot in sorted(by_author.values(), key=lambda s: (s["rank"], s["bucket"]))[:authors]:
        picked.extend(sorted(slot["refs"], key=_natural_key)[:per_author])
    return picked


# ── Texts ─────────────────────────────────────────────────────────────────────


def _slim_text(payload):
    """Only what the sidebar renders (renderCommentaryView): the flat he/en
    lists duplicate ``lines`` and are only read when there are none."""
    slim = {
        key: payload[key]
        for key in ("ref", "title", "heTitle", "heRef", "lines",
                    "translation_generated", "translation_generated_count",
                    "translation_source", "translation_note")
        if key in payload
    }
    if not slim.get("lines"):
        slim["he"] = payload.get("he", [])
        slim["en"] = payload.get("en", [])
    return slim


def _fetch_commentary_text(ref, translate):
    from backend.helpers import _fill_missing_english_lines
    from backend.sefaria_library import get_text

    payload = get_text(ref)
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    # Translation fills line["en"] in place and get_text can hand back an
    # object its own cache still holds.
    payload = copy.deepcopy(payload)
    if translate:
        payload = _fill_missing_english_lines(
            payload, max_lines=TRANSLATE_MAX_LINES, max_runtime_seconds=TRANSLATE_MAX_SECONDS)
    slim = _slim_text(payload)
    if translate:
        # Tells the client this payload already had its translation pass, so
        # opening it doesn't ask for another; passages that skipped it are
        # translated on demand as before.
        slim["translation_attempted"] = True
    if len(json.dumps(slim, ensure_ascii=False)) > MAX_TEXT_BYTES:
        return None
    return slim


def _fetch_all_texts(refs):
    """Fetch ``refs`` in parallel; ``(texts, complete)``."""
    if not refs:
        return {}, True
    pool = ThreadPoolExecutor(max_workers=min(len(refs), 6), thread_name_prefix="sidebar-text")
    try:
        futures = {
            pool.submit(_fetch_commentary_text, ref, index < TRANSLATE_REFS): ref
            for index, ref in enumerate(refs)
        }
        done, pending = wait(futures, timeout=TEXTS_DEADLINE_SECONDS)
    finally:
        pool.shutdown(wait=False)

    results = {}
    for future in done:
        try:
            text = future.result()
        except Exception:  # noqa: BLE001 -- one commentator failing must not sink the rest
            logger.exception("[Sidebar] text fetch failed for %s", futures[future])
            continue
        if text:
            results[futures[future]] = text

    texts = {}
    total = 0
    for ref in refs:
        text = results.get(ref)
        if text is None:
            continue
        size = len(json.dumps(text, ensure_ascii=False))
        if total + size > MAX_BUNDLE_TEXT_BYTES:
            continue
        total += size
        texts[ref] = text
    return texts, not pending


def build_sidebar_texts(ref):
    """``{"ref", "texts"}``: preloaded commentary text for one verse, keyed by
    the commentary's own ref, cached."""
    key = ref.lower()
    cached = _texts_cache.get(key)
    if cached is not None:
        return cached

    def compute():
        links = build_sidebar_links(ref)["links"]
        refs = pick_preload_refs(links)
        texts, complete = _fetch_all_texts(refs)
        return {"ref": ref, "texts": texts}, complete

    result, complete = _single_flight(("texts", key), compute)
    # A partial result is served but kept briefly, so the next request tries
    # the stragglers again instead of repeating the gap for half an hour.
    if result["texts"]:
        _texts_cache.set(key, result, ttl=None if complete else 60)
    return result


# ── Single flight ─────────────────────────────────────────────────────────────

_flight_lock = threading.Lock()
_flights = {}


class _Flight:
    __slots__ = ("event", "value", "failed")

    def __init__(self):
        self.event = threading.Event()
        self.value = None
        self.failed = True


def _single_flight(key, compute):
    """Run ``compute`` once for concurrent callers of the same ``key``: a
    pointer-down prefetch, the verse-change preload and the selection that
    follows them all ask for one verse within milliseconds. Followers wait for
    the leader's value and fall back to computing it themselves if the leader
    failed."""
    with _flight_lock:
        flight = _flights.get(key)
        leader = flight is None
        if leader:
            flight = _flights[key] = _Flight()

    if not leader:
        flight.event.wait(timeout=TEXTS_DEADLINE_SECONDS + 15)
        if not flight.failed:
            return flight.value
        return compute()

    try:
        flight.value = compute()
        flight.failed = False
        return flight.value
    finally:
        with _flight_lock:
            _flights.pop(key, None)
        flight.event.set()
