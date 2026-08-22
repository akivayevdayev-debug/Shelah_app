"""
sefaria_library.py

Provides a cached, structured interface to Sefaria's text library.
Instead of redirecting users to sefaria.org, every text is fetched
from the Sefaria API and rendered inline within Sh'elah.

Features:
- get_library_index(): Full category tree from Sefaria's /api/index
- get_text(ref): Hebrew + English arrays for any text reference
- get_texts_for_category(category): All texts in a category
- search_library(query): Full-text search via Sefaria's search API
- get_linked_texts(ref): All linked commentaries for a ref
"""

import requests
import time
import difflib
import re
import json
import hashlib
import threading
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, urlencode, unquote

import os as _os

from backend.cache import TTLCache

SEFARIA_API = _os.environ.get("SEFARIA_API", "https://www.sefaria.org.il/api").rstrip("/")
SEFARIA_V3_API = _os.environ.get("SEFARIA_V3_API", "https://www.sefaria.org.il/api/v3").rstrip("/")

CACHE_TTL = 3600  # 1 hour
DISK_CACHE_TTL = 7 * 24 * 3600  # 7 days for disk cache

# Thread-safe LRU+TTL cache (backend.cache.TTLCache — internally lock-guarded,
# see cache.py). Cached values are treated as immutable after insertion
# (plan.md §5.2): no caller mutates a returned HTTP payload in place.
_cache = TTLCache(maxsize=2048, ttl=CACHE_TTL)

_http_session = requests.Session()
_http_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    "Referer": "https://www.sefaria.org.il/",
    "Origin": "https://www.sefaria.org.il",
})

# Unbounded-lifetime resolution caches, bounded/TTL'd purely for memory
# hygiene under long-running processes — a title/query resolution almost
# never expires within one process lifetime, so this is not a user-visible
# behavior change.
_resolved_title_ref_cache = TTLCache(maxsize=8192, ttl=30 * 24 * 3600)
_resolved_query_ref_cache = TTLCache(maxsize=8192, ttl=30 * 24 * 3600)

# Matches a trailing "<chapter>[:<verse>]" token once str.rsplit(None, 1)
# (below) has already split it off the book name in linear time. The split
# itself must NOT be done with a regex like r"^(.+?)\s+(\d+)...$": "." and
# "\s" overlap (both match spaces/tabs), so a lazy ".+?" ahead of "\s+"
# re-scans the same whitespace run at every backtrack position, and even a
# plain re.search(r"[ \t]+(\d+)...$") re-triggers the same O(n^2) blowup
# since it retries the greedy-then-backtrack scan from every whitespace
# offset (SonarCloud python:S8786 — verified via adversarial timing tests).
_TRAILING_CHAPTER_VERSE_RE = re.compile(r"^(\d+)(?::(\d+))?$")

# Multi-key caches: mutate-in-place is a torn-read hazard under concurrency
# (plan.md §5.2) — ts/report_mtime/data written as separate key assignments
# means a concurrent reader could observe a fresh ts with stale data. These
# use build-new-dict-then-atomic-swap instead: the lock below guards only the
# writer's build-and-swap; readers stay lock-free by snapshotting the
# module-level reference once (a single local variable) before reading
# multiple keys off it, so a concurrent swap can never surface a torn mix.
_title_catalog_cache = {"ts": 0, "report_mtime": 0.0, "data": []}
_title_catalog_lock = threading.Lock()

# Tracks the most recent Sefaria upstream block event so callers can surface it.
_sefaria_block_status: dict = {
    "is_blocked": False,
    "http_status": None,
    "block_reason": "",
    "last_blocked_ts": 0.0,
    "consecutive_blocks": 0,
}
_search_query_cache = TTLCache(maxsize=512, ttl=300)
_library_index_adjustments_cache = {
    "loaded": False,
    "mtime": 0.0,
    "remove_keys": set(),
    "fix_map": {},
}
_library_index_adjustments_lock = threading.Lock()
_library_index_view_cache = {
    "ts": 0.0,
    "report_mtime": 0.0,
    "data": None,
}
_library_index_view_lock = threading.Lock()

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LIBRARY_REPORT_PATH = _PROJECT_ROOT / "reports" / \
    "library_leaf_remove_fix_report.full.json"
_DISK_CACHE_DIR = _PROJECT_ROOT / ".sefaria_cache"


def _disk_cache_path(url: str) -> Path:
    key = hashlib.md5(url.encode("utf-8")).hexdigest()
    return _DISK_CACHE_DIR / f"{key}.json"


def _disk_cache_get(url: str):
    """Load a cached response from disk if it exists and is not stale."""
    try:
        path = _disk_cache_path(url)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(payload.get("ts", 0)) > DISK_CACHE_TTL:
            return None
        return payload.get("data")
    except Exception:
        return None


def _disk_cache_set(url: str, data) -> None:
    """Persist a successful API response to disk."""
    try:
        _DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _disk_cache_path(url)
        path.write_text(
            json.dumps({"ts": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _normalize_title_key(text):
    return "".join(ch.lower() for ch in str(text or "") if ch.isalnum())


NON_LOADING_LITURGY_TITLES = {
    _normalize_title_key("Kinnot for Tisha B'Av (Ashkenaz)"),
    _normalize_title_key("Ma'aneh Lashon Chabad"),
    _normalize_title_key("Ma'avar Yabbok"),
    _normalize_title_key("Machzor Rosh Hashanah Linear"),
    _normalize_title_key("Machzor Yom Ha'atzmaut & Yom Yerushalayim"),
    _normalize_title_key("Machzor Yom Ha'atzmaut & Yom Yetushalayim"),
    _normalize_title_key("Seder Ma'amadot"),
    _normalize_title_key("Seder Tisha B'Av (Edot HaMizrach)"),
    _normalize_title_key("Seder Tisha B'Av (Edot HaMizrac)"),
    _normalize_title_key("Weekday Siddur Chabad"),
}


def _extract_adjustment_keys(row):
    """Yield normalized lookup keys (title/name_ref/initial_ref) for one
    removals/fixes report row. Split out of
    _load_library_index_adjustments() to keep these loops out of that
    function's own complexity count (SonarCloud python:S3776).
    """
    for candidate in (
        row.get("title", ""),
        row.get("name_ref", ""),
        row.get("initial_ref", ""),
    ):
        key = _normalize_title_key(candidate)
        if key:
            yield key


def _parse_library_adjustments_payload(payload):
    """Build (remove_keys, fix_map) from a crawl_library_leaves.py report
    payload. Split out of _load_library_index_adjustments() (SonarCloud
    python:S3776) -- see _extract_adjustment_keys.
    """
    remove_keys = set()
    fix_map = {}

    for row in payload.get("removals", []) or []:
        remove_keys.update(_extract_adjustment_keys(row))

    for row in payload.get("fixes", []) or []:
        suggested = str(row.get("suggested_ref") or "").strip()
        if not suggested:
            continue
        for key in _extract_adjustment_keys(row):
            fix_map[key] = suggested

    return remove_keys, fix_map


def _load_library_index_adjustments():
    """Load removal/fix mappings generated by crawl_library_leaves.py."""
    global _library_index_adjustments_cache
    path = _LIBRARY_REPORT_PATH

    # Lock-free read: snapshot the module-level reference once so a
    # concurrent writer swap can never surface a torn mix of old/new keys.
    snapshot = _library_index_adjustments_cache

    if not path.exists():
        if snapshot["loaded"] and snapshot["mtime"] == 0.0:
            return snapshot
        new_state = {"loaded": True, "mtime": 0.0,
                      "remove_keys": set(), "fix_map": {}}
        with _library_index_adjustments_lock:
            _library_index_adjustments_cache = new_state
        return new_state

    try:
        mtime = path.stat().st_mtime
    except Exception:
        mtime = 0.0

    if snapshot["loaded"] and snapshot["mtime"] >= mtime:
        return snapshot

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[Sefaria Library] Failed loading index adjustments: {exc}")
        new_state = {"loaded": True, "mtime": mtime,
                      "remove_keys": set(), "fix_map": {}}
        with _library_index_adjustments_lock:
            _library_index_adjustments_cache = new_state
        return new_state

    remove_keys, fix_map = _parse_library_adjustments_payload(payload)

    new_state = {"loaded": True, "mtime": mtime,
                 "remove_keys": remove_keys, "fix_map": fix_map}
    with _library_index_adjustments_lock:
        # Re-check: another thread may have already refreshed to an
        # equal-or-newer state while we were parsing the report off-lock —
        # >= (not ==), so a slower thread never clobbers a fresher
        # concurrent write with its own stale result.
        current = _library_index_adjustments_cache
        if current["loaded"] and current["mtime"] >= mtime:
            return current
        _library_index_adjustments_cache = new_state
    return new_state


def _apply_library_index_fix(clone, title_key, fix_map):
    """Attach a fixed opening ref to `clone` if `title_key` has a suggested
    fix and the existing ref doesn't already point elsewhere. Mutates
    `clone` in place. Split out of _prune_and_fix_library_index() to keep
    this branch out of that function's own complexity count (SonarCloud
    python:S3776).
    """
    if not title_key or title_key not in fix_map:
        return
    suggested_ref = fix_map[title_key]
    existing_ref = str(clone.get("firstSectionRef")
                       or clone.get("ref") or "").strip()
    if not existing_ref or _normalize_title_key(existing_ref) == title_key:
        clone["firstSectionRef"] = suggested_ref


def _prune_and_fix_library_index(node, remove_keys, fix_map):
    """Remove known non-loading works and attach fixed opening refs where needed."""
    if isinstance(node, list):
        pruned = []
        for child in node:
            cleaned = _prune_and_fix_library_index(child, remove_keys, fix_map)
            if cleaned is not None:
                pruned.append(cleaned)
        return pruned

    if not isinstance(node, dict):
        return node

    clone = dict(node)

    title = str(clone.get("title") or "").strip()
    title_key = _normalize_title_key(title)
    if title_key and title_key in remove_keys:
        return None

    _apply_library_index_fix(clone, title_key, fix_map)

    for child_key in ("contents", "children"):
        if isinstance(clone.get(child_key), list):
            cleaned_children = _prune_and_fix_library_index(
                clone.get(child_key), remove_keys, fix_map)
            clone[child_key] = cleaned_children if isinstance(
                cleaned_children, list) else []

    return clone


def _cached_get(url, ttl=CACHE_TTL):
    """Cached HTTP GET wrapper — checks memory → disk → network."""
    now = time.time()
    # 1. Memory cache (fastest)
    cached = _cache.get(url)
    if cached is not None:
        return cached
    # 2. Disk cache (survives restarts)
    disk_data = _disk_cache_get(url)
    if disk_data is not None:
        _cache.set(url, disk_data, ttl=ttl)
        return disk_data
    # 3. Network request — never held under the cache's lock (rule: no
    # lock across network/disk/JSON work; TTLCache.set() only locks the
    # brief dict-insert itself).
    try:
        r = _http_session.get(url, timeout=12)
        r.raise_for_status()
        data = r.json()
        # Clear block status on a successful fetch.
        if _sefaria_block_status["is_blocked"]:
            _sefaria_block_status.update({
                "is_blocked": False,
                "http_status": 200,
                "block_reason": "",
                "consecutive_blocks": 0,
            })
        _cache.set(url, data, ttl=ttl)
        _disk_cache_set(url, data)
        return data
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code == 403:
            _sefaria_block_status.update({
                "is_blocked": True,
                "http_status": 403,
                "block_reason": "Cloudflare 403 Forbidden",
                "last_blocked_ts": now,
                "consecutive_blocks": _sefaria_block_status.get("consecutive_blocks", 0) + 1,
            })
            print(
                f"[Sefaria Library Error] Failed to fetch data from {url}. Status Code: {status_code}. Reason: Cloudflare 403 Forbidden. Sefaria is blocking the request.")
        elif status_code not in (400, 404):
            print(
                f"[Sefaria Library Error] HTTP error during fetch. URL: {url}. Status Code: {status_code}. Details: {str(e)}")
        return None
    except requests.RequestException as e:
        print(
            f"[Sefaria Library Error] Network or request error. URL: {url}. Details: {str(e)}")
        return None
    except Exception as e:
        import traceback
        print(
            f"[Sefaria Library Error] Unexpected error occurred. URL: {url}. Type: {type(e).__name__}. Details: {str(e)}")
        traceback.print_exc()
        return None


def _normalize_filter_values(raw_values):
    if raw_values is None:
        return []
    if isinstance(raw_values, str):
        values = [part.strip()
                  for part in raw_values.split(",") if part.strip()]
    elif isinstance(raw_values, (list, tuple, set)):
        values = [str(part).strip()
                  for part in raw_values if str(part).strip()]
    else:
        values = [str(raw_values).strip()] if str(raw_values).strip() else []
    return [value.lower() for value in values]


def _normalize_to_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if item]
    if isinstance(value, str):
        return [value]
    return []


def _infer_nusach(ref_value, categories=None):
    haystack = " ".join([
        str(ref_value or ""),
        " ".join(categories or []),
    ]).lower()
    if "ashkenaz" in haystack:
        return "Ashkenaz"
    if "sefard" in haystack or "sephard" in haystack:
        return "Sefardic"
    if "mizrahi" in haystack:
        return "Mizrahi"
    if "yemen" in haystack:
        return "Yemenite"
    return ""


def _matches_metadata_filters(result, metadata_filters=None):
    if not metadata_filters:
        return True

    candidates = {
        "era": [result.get("era", "")],
        "author": _normalize_to_list(result.get("authors", [])),
        "category": _normalize_to_list(result.get("categories", [])) + [result.get("path", "")],
        "geography": [result.get("geography", ""), result.get("path", ""), result.get("ref", "")],
        "nusach": [result.get("nusach", ""), result.get("path", ""), result.get("ref", "")],
    }

    for key, raw_values in metadata_filters.items():
        needles = _normalize_filter_values(raw_values)
        if not needles:
            continue

        haystack = [str(value).lower()
                    for value in candidates.get(key, []) if value]
        if not haystack:
            return False

        if not any(any(needle in text for text in haystack) for needle in needles):
            return False

    return True


def _encode_ref_path(value):
    """Encode a Sefaria ref/title safely for path-style API endpoints."""
    source = str(value or "").strip().replace(" ", "_")
    return quote(source, safe="._,;:'()-")


def _normalize_requested_ref(value, max_rounds=3):
    decoded = str(value or "").strip()
    for _ in range(max_rounds):
        next_value = unquote(decoded).strip()
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _build_text_url(ref, lang="both", context=0):
    encoded_ref = _encode_ref_path(ref)
    params = urlencode({"lang": lang, "context": context, "pad": 0})
    return f"{SEFARIA_API}/texts/{encoded_ref}?{params}"


def _build_v3_text_url(ref):
    """Build a Sefaria v3 texts endpoint URL requesting source (he) + translation (en)."""
    encoded_ref = _encode_ref_path(ref)
    return f"{SEFARIA_V3_API}/texts/{encoded_ref}?version=source&version=translation"


def _classify_v3_version_texts(versions):
    """Pick Hebrew/English text arrays out of a v3 API versions list.
    Split out of _parse_v3_response() to keep this loop out of that
    function's own complexity count (SonarCloud python:S3776).
    """
    he_raw = []
    en_raw = []
    for version in versions:
        lang = version.get("language", "") or ""
        direction = version.get("direction", "") or ""
        is_source = bool(version.get("isSource", False))
        text = version.get("text") or []
        # Hebrew / source: isSource=True or direction="rtl" or language="he"
        if (is_source or direction == "rtl" or lang == "he") and not he_raw:
            he_raw = text
        # English / translation: avoid Hebrew-direction texts
        elif not he_raw and not is_source and direction != "rtl" and lang != "he" and not en_raw:
            en_raw = text
        elif he_raw and not en_raw and not is_source and direction != "rtl" and lang != "he":
            en_raw = text
    return he_raw, en_raw


def _parse_v3_response(data, requested_ref):
    """Convert a Sefaria v3 /api/v3/texts/{ref} response to our standard get_text() output format."""
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return None
    versions = data.get("versions") or []
    if not versions:
        return None

    he_raw, en_raw = _classify_v3_version_texts(versions)

    # Reuses the same flattening logic as get_text() (both align Hebrew and
    # English nested-array text fields into (path, string) pairs).
    he_leafs = _flatten_text_with_path(he_raw)
    en_leafs = _flatten_text_with_path(en_raw)
    he_by_path = dict(he_leafs)
    en_by_path = dict(en_leafs)
    all_paths = sorted(set(he_by_path.keys()) | set(en_by_path.keys()))

    if not all_paths:
        return None

    lines = []
    for path in all_paths:
        lines.append({
            "he": he_by_path.get(path, ""),
            "en": en_by_path.get(path, ""),
            "segment": ".".join(str(i) for i in path) if path else "1",
        })

    he_flat = [line["he"] for line in lines if line.get("he")]
    en_flat = [line["en"] for line in lines if line.get("en")]

    resolved_ref = data.get("ref", requested_ref)
    fallback_title = str(resolved_ref or requested_ref).split(",", 1)[
        0].strip()
    return {
        "ref": resolved_ref,
        "title": data.get("title") or data.get("indexTitle") or data.get("book") or fallback_title,
        "heTitle": data.get("heTitle") or data.get("heIndexTitle") or "",
        "he": he_flat,
        "en": en_flat,
        "lines": lines,
        "sections": data.get("sections", []),
        "sectionNames": data.get("sectionNames", []),
        "next": data.get("next"),
        "prev": data.get("prev"),
        "categories": data.get("categories", []),
        "authors": [],
        "era": "",
    }


def _is_specific_ref_query(value):
    raw = str(value or "")
    if re.search(r"\d+[ab]?\b", raw, flags=re.IGNORECASE):
        return True
    if re.search(r":\d", raw):
        return True
    if "," in raw:
        suffix = raw.split(",", 1)[1]
        if any(ch.isdigit() for ch in suffix):
            return True
    return False


def _split_title_suffix(value):
    raw = str(value or "").strip()
    if "," not in raw:
        return raw, ""
    title, suffix = raw.split(",", 1)
    return title.strip(), suffix.strip()


# Corpus/category labels the AI sometimes prepends to a Talmud ref (e.g.
# "Talmud Bavli, Chullin 113a") that Sefaria's own refs never include --
# Sefaria addresses tractates directly ("Chullin 113a"). Stripping these
# turns an otherwise-unresolvable ref into a direct hit without a network
# round-trip to /api/name first.
_TALMUD_CORPUS_PREFIX_RE = re.compile(
    r"^(?:the\s+)?(?:babylonian\s+talmud|talmud\s+bavli|talmud|bavli)\s*,\s*",
    re.IGNORECASE,
)


def _strip_known_corpus_prefix(raw_ref):
    stripped = _TALMUD_CORPUS_PREFIX_RE.sub("", raw_ref).strip()
    return stripped if stripped and stripped != raw_ref else ""


def _cache_resolved_ref(cache_key, resolved):
    """Cache + return a resolved opening ref. Split out of
    _resolve_opening_ref_for_title() to DRY up its three set-then-return
    sites and keep them out of that function's own complexity count
    (SonarCloud python:S3776).
    """
    if cache_key:
        _resolved_title_ref_cache.set(cache_key, resolved)
    return resolved


def _compute_opening_ref_from_entry(entry, title):
    """Derive the opening ref from an index entry's firstSectionRef,
    specific-ref-ness, or leaf refs. Split out of
    _resolve_opening_ref_for_title() (SonarCloud python:S3776) -- see
    _cache_resolved_ref.
    """
    canonical_title = str(entry.get("title") or title or "").strip()
    first_section = entry.get("firstSectionRef") or entry.get("firstSection")
    if isinstance(first_section, str) and first_section.strip():
        return first_section.strip()
    if _is_specific_ref_query(canonical_title):
        return canonical_title

    leaf_refs = get_index_leaf_refs(canonical_title, max_refs=1)
    if leaf_refs:
        return leaf_refs[0]
    if entry.get("sectionNames"):
        return f"{canonical_title} 1"
    return canonical_title


def _resolve_opening_ref_for_title(title):
    """Resolve a title to an opening ref that /texts can load."""
    cache_key = _normalize_title_key(title)
    if cache_key:
        cached = _resolved_title_ref_cache.get(cache_key)
        if cached is not None:
            return cached

    adjustments = _load_library_index_adjustments()
    fix_map = adjustments.get("fix_map", {})
    suggested_ref = fix_map.get(cache_key, "") if cache_key else ""
    if suggested_ref:
        return _cache_resolved_ref(cache_key, suggested_ref)

    entry = get_index_entry(title)
    if not isinstance(entry, dict) or entry.get("error"):
        return _cache_resolved_ref(cache_key, str(title or "").strip())

    resolved = _compute_opening_ref_from_entry(entry, title)
    return _cache_resolved_ref(cache_key, resolved)


def _add_name_completion_refs(name_data, add, candidates, max_candidates):
    """Add ref candidates from a Sefaria /name response's completion_objects
    via `add()`. Returns True if the caller should stop (max_candidates
    reached). Split out of _resolve_ref_candidates() to keep this loop out
    of that function's own complexity count (SonarCloud python:S3776).
    """
    for obj in name_data.get("completion_objects", []) or []:
        if obj.get("type") != "ref":
            continue
        add(obj.get("key") or obj.get("title"))
        if len(candidates) >= max_candidates:
            return True
    return False


def _build_ref_candidate_titles(title_part, name_data):
    """Dedup-normalize the (title_part, name_data.index, name_data.book)
    triple into a candidate-titles list. Split out of
    _resolve_ref_candidates() (SonarCloud python:S3776) -- see
    _add_name_completion_refs.
    """
    candidate_titles = []
    for possible in (
        title_part,
        (name_data or {}).get("index") if isinstance(name_data, dict) else "",
        (name_data or {}).get("book") if isinstance(name_data, dict) else "",
    ):
        title = str(possible or "").strip()
        if not title:
            continue
        if title.lower() in {t.lower() for t in candidate_titles}:
            continue
        candidate_titles.append(title)
    return candidate_titles


def _process_ref_candidate_title(title, suffix, is_specific_ref, add, candidates, max_candidates):
    """Resolve one candidate title's index entry and add its ref variants
    via `add()`. Returns True if the caller should stop (max_candidates
    reached). Split out of _resolve_ref_candidates() (SonarCloud
    python:S3776) -- see _add_name_completion_refs.
    """
    entry = get_index_entry(title)
    if not isinstance(entry, dict) or entry.get("error"):
        return False

    canonical = str(entry.get("title") or title).strip()
    if canonical:
        add(canonical)
    if suffix and canonical and not canonical.lower().endswith(f", {suffix.lower()}"):
        add(f"{canonical}, {suffix}")

    first_section = entry.get(
        "firstSectionRef") or entry.get("firstSection")
    if isinstance(first_section, str) and first_section.strip():
        add(first_section.strip())

    if not is_specific_ref and canonical:
        if entry.get("sectionNames"):
            add(f"{canonical} 1")
        for leaf_ref in get_index_leaf_refs(canonical, max_refs=4):
            add(leaf_ref)
            if len(candidates) >= max_candidates:
                return True

    return len(candidates) >= max_candidates


def _resolve_ref_candidates(raw_ref, max_candidates=12):
    """Build candidate refs for texts that require canonical title or leaf-node resolution."""
    raw = _normalize_requested_ref(raw_ref)
    if not raw:
        return []

    candidates = []
    seen = set()

    def add(value):
        candidate = str(value or "").strip()
        if not candidate:
            return
        key = candidate.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append(candidate)

    add(raw)
    if ":" in raw:
        add(raw.replace(":", "."))

    corpus_stripped = _strip_known_corpus_prefix(raw)
    if corpus_stripped:
        add(corpus_stripped)
        if ":" in corpus_stripped:
            add(corpus_stripped.replace(":", "."))

    title_part, suffix = _split_title_suffix(raw)
    is_specific_ref = _is_specific_ref_query(raw)

    safe_name = _encode_ref_path(raw)
    name_data = _cached_get(f"{SEFARIA_API}/name/{safe_name}", ttl=43200)
    if isinstance(name_data, dict):
        if name_data.get("is_ref") and name_data.get("ref"):
            add(name_data.get("ref"))

        if _add_name_completion_refs(name_data, add, candidates, max_candidates):
            return candidates[:max_candidates]

    candidate_titles = _build_ref_candidate_titles(title_part, name_data)

    for title in candidate_titles:
        if _process_ref_candidate_title(title, suffix, is_specific_ref, add, candidates, max_candidates):
            return candidates[:max_candidates]

    return candidates[:max_candidates]


def _flatten_index_titles(node, rows, seen_titles):
    if isinstance(node, list):
        for child in node:
            _flatten_index_titles(child, rows, seen_titles)
        return

    if not isinstance(node, dict):
        return

    title = str(node.get("title") or "").strip()
    categories = node.get("categories", []) or []
    if title and isinstance(categories, list):
        key = title.lower()
        if key not in seen_titles:
            seen_titles.add(key)
            rows.append({
                "title": title,
                "heTitle": str(node.get("heTitle") or "").strip(),
                "categories": [str(item) for item in categories if item],
                "dependence": str(node.get("dependence") or "").strip(),
            })

    for child_key in ("contents", "children"):
        if child_key in node:
            _flatten_index_titles(node.get(child_key), rows, seen_titles)


def _get_title_catalog(ttl=86400):
    global _title_catalog_cache
    now = time.time()
    adjustments = _load_library_index_adjustments()
    report_mtime = float(adjustments.get("mtime", 0.0))

    # Lock-free read: snapshot the module-level reference once so a
    # concurrent writer swap can never surface a torn mix of old/new keys.
    snapshot = _title_catalog_cache
    cached_data = snapshot.get("data", [])
    cached_ts = snapshot.get("ts", 0)
    cached_mtime = float(snapshot.get("report_mtime", 0.0))
    if cached_data and now - cached_ts < ttl and cached_mtime >= report_mtime:
        return cached_data

    # Build the full new state off-lock (network + CPU work) — never hold
    # the writer lock across it.
    rows = []
    seen_titles = set()
    _flatten_index_titles(get_library_index(), rows, seen_titles)

    for row in rows:
        haystack_parts = [
            row.get("title", ""),
            row.get("heTitle", ""),
            " ".join(row.get("categories", [])),
            row.get("title", "").replace(";", " "),
        ]
        row["search"] = " ".join(haystack_parts).lower()

    with _title_catalog_lock:
        # Re-check: another thread may have already won and refreshed to an
        # equal-or-newer state while we were building ours off-lock — >=
        # (not ==), so a slower thread never clobbers a fresher concurrent
        # write with its own stale result.
        current = _title_catalog_cache
        if (
            current.get("data")
            and now - current.get("ts", 0) < ttl
            and float(current.get("report_mtime", 0.0)) >= report_mtime
        ):
            return current["data"]
        _title_catalog_cache = {"ts": now,
                                 "report_mtime": report_mtime, "data": rows}
    return rows


def _score_catalog_row(row, tokens, joined_query):
    """Relevance score for one title-catalog row against the query tokens.
    Returns None if the row doesn't match at all. Split out of
    _search_index_catalog() to keep this loop out of that function's own
    complexity count (SonarCloud python:S3776).
    """
    haystack = row.get("search", "")
    if not all(token in haystack for token in tokens):
        return None

    title_lower = row.get("title", "").lower()
    score = 60
    if title_lower == joined_query:
        score = 120
    elif title_lower.startswith(joined_query):
        score = 100
    elif joined_query in title_lower:
        score = 85

    if "jonathan" in tokens and "sacks" in tokens:
        if any("jonathan sacks" in cat.lower() for cat in row.get("categories", [])):
            score += 25
    if "essay" in tokens and "essay" in title_lower:
        score += 15

    return score


def _build_catalog_search_result(row, seen_refs, metadata_filters):
    """Resolve one ranked catalog row into a search-result dict, or None to
    skip it (no opening ref, duplicate, or fails metadata filters).
    Mutates `seen_refs` on success. Split out of _search_index_catalog()
    (SonarCloud python:S3776) -- see _score_catalog_row.
    """
    opening_ref = _resolve_opening_ref_for_title(row.get("title", ""))
    if not opening_ref or opening_ref in seen_refs:
        return None

    result = {
        "ref": opening_ref,
        "heRef": row.get("heTitle", ""),
        "text": row.get("title", ""),
        "categories": row.get("categories", []),
        "path": row.get("title", ""),
        "authors": [],
        "era": "",
        "geography": "",
        "nusach": _infer_nusach(opening_ref, row.get("categories", [])),
    }

    if not _matches_metadata_filters(result, metadata_filters):
        return None

    seen_refs.add(opening_ref)
    return result


def _search_index_catalog(query, size=10, metadata_filters=None):
    normalized_query = re.sub(
        r"[^0-9a-z\u0590-\u05ff]+", " ", str(query or "").lower()).strip()
    tokens = [token for token in normalized_query.split() if token]
    if not tokens:
        return []

    cache_key = f"{normalized_query}|{size}|{str(metadata_filters or {})}"
    cached = _search_query_cache.get(cache_key)
    if cached is not None:
        return cached

    joined_query = " ".join(tokens)
    ranked_rows = []
    for row in _get_title_catalog():
        score = _score_catalog_row(row, tokens, joined_query)
        if score is not None:
            ranked_rows.append((score, row))

    ranked_rows.sort(key=lambda item: (-item[0], item[1].get("title", "")))

    results = []
    seen_refs = set()
    for _, row in ranked_rows[:max(size * 5, 30)]:
        result = _build_catalog_search_result(row, seen_refs, metadata_filters)
        if result is not None:
            results.append(result)
            if len(results) >= size:
                break

    _search_query_cache.set(cache_key, results)
    return results


def get_library_index():
    """
    Fetches the full Sefaria library category tree.
    Returns a nested structure of categories and texts.
    """
    global _library_index_view_cache
    data = _cached_get(f"{SEFARIA_API}/index")
    if not data:
        return []

    adjustments = _load_library_index_adjustments()
    remove_keys = adjustments.get("remove_keys", set())
    fix_map = adjustments.get("fix_map", {})
    report_mtime = adjustments.get("mtime", 0.0)

    now = time.time()
    # Lock-free read: snapshot the module-level reference once so a
    # concurrent writer swap can never surface a torn mix of old/new keys.
    snapshot = _library_index_view_cache
    if (
        snapshot.get("data") is not None
        and now - float(snapshot.get("ts", 0.0)) < CACHE_TTL
        and float(snapshot.get("report_mtime", 0.0)) >= float(report_mtime)
    ):
        return snapshot["data"]

    # Never mutate cached HTTP payload directly. Built off-lock.
    adjusted = _prune_and_fix_library_index(
        deepcopy(data), remove_keys, fix_map)
    if adjusted is None:
        adjusted = []

    with _library_index_view_lock:
        # Re-check: >= (not ==), so a slower thread never clobbers a
        # fresher concurrent write with its own stale result.
        current = _library_index_view_cache
        if (
            current.get("data") is not None
            and now - float(current.get("ts", 0.0)) < CACHE_TTL
            and float(current.get("report_mtime", 0.0)) >= float(report_mtime)
        ):
            return current["data"]
        _library_index_view_cache = {
            "ts": now, "report_mtime": float(report_mtime), "data": adjusted}
    return adjusted


def _find_category_child_node(candidates, part_norm):
    """Find the child node whose category/title/heCategory label matches
    `part_norm`, or None. Split out of get_category_contents() to keep
    this loop out of that function's own complexity count (SonarCloud
    python:S3776).
    """
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        labels = [
            candidate.get("category", ""),
            candidate.get("title", ""),
            candidate.get("heCategory", ""),
        ]
        label_norms = {_normalize_title_for_compare(
            label) for label in labels if label}
        if part_norm in label_norms:
            return candidate
    return None


def get_category_contents(category_path):
    """
    Returns all books/texts under a given category path.
    category_path: e.g. "Tanakh" or "Tanakh/Torah"
    """
    # plan.md §8.C.5 security-audit pass: category_path is attacker-
    # controlled (Flask <path:category> converter, already percent-decoded)
    # and used to reach every other ref-building function here via
    # _encode_ref_path() -- this one built its outbound URL from a raw
    # .replace() with no quoting, letting a literal '#'/'?' in the path
    # truncate/inject onto the real Sefaria request. _encode_ref_path()'s
    # safe set includes ',' so the /-to-, substitution below still composes
    # correctly through it.
    encoded = _encode_ref_path(category_path.replace("/", ","))
    data = _cached_get(f"{SEFARIA_API}/index/{encoded}")
    if data and not (isinstance(data, dict) and data.get("error")):
        return data

    # Fallback for category paths that Sefaria's /index/{path} does not resolve.
    parts = [p.strip() for p in (category_path or "").split("/") if p.strip()]
    if not parts:
        return []

    node = get_library_index()
    for part in parts:
        if isinstance(node, list):
            candidates = node
        elif isinstance(node, dict):
            candidates = node.get("contents", []) or []
        else:
            return []

        next_node = _find_category_child_node(
            candidates, _normalize_title_for_compare(part))
        if not next_node:
            return []
        node = next_node

    return node if isinstance(node, (dict, list)) else []


def _flatten_text_with_path(arr, path=None):
    """Flatten a Sefaria nested-array text field into (path, string) pairs
    for structural alignment between Hebrew/English arrays. Moved to
    module level (out of get_text()) since a nested closure's own branches
    count against the enclosing function's complexity, but a top-level
    function's don't (SonarCloud python:S3776).
    """
    path = path or ()
    if isinstance(arr, str):
        text = arr.strip()
        return [(path, text)] if text else []
    if isinstance(arr, list):
        result = []
        for idx, item in enumerate(arr, start=1):
            result.extend(_flatten_text_with_path(item, path + (idx,)))
        return result
    return []


def _try_initial_text_attempts(initial_attempts, attempts_tried, lang, context):
    """Try each of get_text()'s initial ref attempts (cached-resolved ref,
    then the requested ref) in order. Returns (data, resolved_ref), with
    data=None if none succeeded. Split out of get_text() (SonarCloud
    python:S3776) -- see _flatten_text_with_path.
    """
    for attempt in initial_attempts:
        attempt_ref = str(attempt or "").strip()
        if not attempt_ref:
            continue
        lowered = attempt_ref.lower()
        if lowered in attempts_tried:
            continue
        attempts_tried.add(lowered)

        attempt_data = _cached_get(_build_text_url(
            attempt_ref, lang, context), ttl=86400)
        if attempt_data and "error" not in attempt_data:
            return attempt_data, attempt_data.get("ref") or attempt_ref
    return None, ""


def _try_candidate_text_refs(requested_ref, attempts_tried, lang, context):
    """Try each resolved candidate ref for get_text() in order. Returns
    (data, resolved_ref), with data=None if none succeeded. Split out of
    get_text() (SonarCloud python:S3776) -- see _flatten_text_with_path.
    """
    for candidate in _resolve_ref_candidates(requested_ref):
        lowered = candidate.lower()
        if lowered in attempts_tried:
            continue
        attempts_tried.add(lowered)

        candidate_data = _cached_get(_build_text_url(
            candidate, lang, context), ttl=86400)
        if candidate_data and "error" not in candidate_data:
            return candidate_data, candidate_data.get("ref") or candidate
    return None, ""


def _build_text_not_found_response(requested_ref):
    """The "no data found" error-response branch of get_text(). Split out
    (SonarCloud python:S3776) -- see _flatten_text_with_path.
    """
    # Surface a specific error when Sefaria is actively blocking requests.
    if _sefaria_block_status.get("is_blocked"):
        reason = _sefaria_block_status.get(
            "block_reason", "Cloudflare 403")
        return {
            "error": f"Sefaria service blocked ({reason}): {requested_ref}",
            "error_type": "sefaria_blocked",
            "ref": requested_ref,
            "he": [],
            "en": [],
        }
    return {"error": f"Text not found: {requested_ref}", "error_type": "not_found", "ref": requested_ref, "he": [], "en": []}


def _build_text_lines(data):
    """Build the aligned {he, en, segment} lines list + flat he/en arrays
    from a raw Sefaria text response. Returns (lines, he_flat, en_flat).
    Split out of get_text() (SonarCloud python:S3776) -- see
    _flatten_text_with_path.
    """
    he_raw = data.get("he", [])
    en_raw = data.get("text", [])

    he_leafs = _flatten_text_with_path(he_raw)
    en_leafs = _flatten_text_with_path(en_raw)

    he_by_path = dict(he_leafs)
    en_by_path = dict(en_leafs)
    all_paths = sorted(set(he_by_path.keys()) | set(en_by_path.keys()))

    lines = []
    for path in all_paths:
        lines.append({
            "he": he_by_path.get(path, ""),
            "en": en_by_path.get(path, ""),
            "segment": ".".join(str(i) for i in path) if path else "1",
        })

    he_flat = [line["he"] for line in lines if line.get("he")]
    en_flat = [line["en"] for line in lines if line.get("en")]
    return lines, he_flat, en_flat


def get_text(ref, lang="both", context=0):
    """
    Fetches a specific text passage from Sefaria.
    Returns:
      {
        "ref": str,
        "he": list of str (Hebrew verses/lines),
        "en": list of str (English verses/lines),
        "title": str,
        "heTitle": str,
        "sections": list,
        "next": str or None,
        "prev": str or None,
        "commentary": list (optional)
      }
    """
    requested_ref = _normalize_requested_ref(ref)
    if not requested_ref:
        return {"error": "Text not found", "ref": "", "he": [], "en": []}

    cache_key = requested_ref.lower()
    attempts_tried = set()

    # --- Try v3 API first (newer endpoint, may avoid Cloudflare block) ---
    v3_url = _build_v3_text_url(requested_ref)
    v3_raw = _cached_get(v3_url, ttl=86400)
    if v3_raw and not v3_raw.get("error"):
        parsed_v3 = _parse_v3_response(v3_raw, requested_ref)
        if parsed_v3:
            _resolved_query_ref_cache.set(cache_key, str(
                parsed_v3.get("ref") or requested_ref))
            return parsed_v3

    # --- Fall back to v2 API with full candidate resolution ---
    cached_ref = _resolved_query_ref_cache.get(cache_key) or ""
    initial_attempts = [cached_ref,
                        requested_ref] if cached_ref else [requested_ref]
    data, resolved_ref = _try_initial_text_attempts(
        initial_attempts, attempts_tried, lang, context)

    if not data:
        data, resolved_ref = _try_candidate_text_refs(
            requested_ref, attempts_tried, lang, context)

    if not data or "error" in data:
        _resolved_query_ref_cache.delete(cache_key)
        return _build_text_not_found_response(requested_ref)

    _resolved_query_ref_cache.set(cache_key, str(
        resolved_ref or data.get("ref") or requested_ref))

    lines, he_flat, en_flat = _build_text_lines(data)

    resolved_output_ref = data.get("ref", resolved_ref or requested_ref)
    fallback_title = str(resolved_output_ref or requested_ref).split(",", 1)[
        0].strip()
    title = data.get("title") or data.get(
        "indexTitle") or data.get("book") or fallback_title

    return {
        "ref": resolved_output_ref,
        "title": title,
        "heTitle": data.get("heTitle", ""),
        "he": he_flat,
        "en": en_flat,
        "lines": lines,
        "sections": data.get("sections", []),
        "sectionNames": data.get("sectionNames", []),
        "next": data.get("next"),
        "prev": data.get("prev"),
        "categories": data.get("categories", []),
        "authors": data.get("authors", []),
        "era": data.get("era", "")
    }


def _probe_sefaria_endpoint(url, timeout=10):
    """Probe one Sefaria API endpoint and report its availability. Moved
    to module level (out of check_sefaria_availability()) since a nested
    closure's own branches count against the enclosing function's
    complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    try:
        r = _http_session.get(url, timeout=timeout)
        status = r.status_code
        if status == 200:
            try:
                body = r.json()
                ok = bool(body) and not body.get("error")
            except Exception:
                ok = False
        else:
            ok = False
        return {"available": ok, "http_status": status, "error": "" if ok else f"HTTP {status}", "url": url}
    except Exception as exc:
        return {"available": False, "http_status": None, "error": str(exc), "url": url}


def check_sefaria_availability():
    """
    Probe both the v3 and v2 Sefaria API endpoints live (no cache).
    Returns a status dict suitable for the /api/diagnostics/sefaria route.
    This call is not cached — it always makes live HTTP requests.
    """
    import time as _time
    probe_ref = "Genesis_1"
    v3_url = f"{SEFARIA_V3_API}/texts/{probe_ref}?version=source"
    v2_url = _build_text_url("Genesis 1")

    v3_result = _probe_sefaria_endpoint(v3_url)
    v2_result = _probe_sefaria_endpoint(v2_url)

    block_type = ""
    if v3_result["http_status"] == 403 or v2_result["http_status"] == 403:
        block_type = "cloudflare_403"
    elif not v3_result["available"] and not v2_result["available"]:
        block_type = "unavailable"

    return {
        "v3_api": v3_result,
        "v2_api": v2_result,
        "block_type": block_type,
        "overall_available": v3_result["available"] or v2_result["available"],
        "cached_block_status": dict(_sefaria_block_status),
        "checked_at": _time.time(),
    }


def _get_search_index_metadata(book, index_cache):
    """Cached get_index_entry() lookup for _add_search_library_result().
    Split out (SonarCloud python:S3776) -- see _add_search_library_result.
    """
    title = str(book or "").strip()
    if not title:
        return {}
    if title not in index_cache:
        data = get_index_entry(title)
        index_cache[title] = data if isinstance(data, dict) else {}
    return index_cache[title]


def _resolve_search_result_fixed_ref(ref_value, label, book, index_entry, fix_map):
    """Return (possibly fix_map-corrected ref_value, the candidate titles used to find it)."""
    candidate_titles = [
        ref_value,
        label,
        book,
        index_entry.get("title") if isinstance(index_entry, dict) else "",
    ]
    for candidate in candidate_titles:
        key = _normalize_title_key(candidate)
        if key and key in fix_map:
            return str(fix_map[key]).strip(), candidate_titles
    return ref_value, candidate_titles


def _is_search_result_removed(ref_value, candidate_titles, remove_keys):
    for candidate in candidate_titles + [ref_value]:
        key = _normalize_title_key(candidate)
        if key and key in remove_keys:
            return True
    return False


def _resolve_search_result_categories(index_entry, explicit_categories, normalized_category_filters):
    """Return the result's categories, or None if it fails the category filter."""
    categories = []
    if isinstance(explicit_categories, list) and explicit_categories:
        categories = [str(item) for item in explicit_categories if item]
    if not categories and isinstance(index_entry, dict):
        categories = [str(item)
                      for item in index_entry.get("categories", []) if item]

    if normalized_category_filters:
        category_blob = " ".join(categories).lower()
        if not any(token in category_blob for token in normalized_category_filters):
            return None
    return categories


def _resolve_search_result_authors(index_entry):
    authors = []
    if isinstance(index_entry, dict):
        authors = index_entry.get("authors", [])
    if isinstance(authors, str):
        authors = [authors]
    if not isinstance(authors, list):
        authors = []
    return authors


def _resolve_search_result_era_geography(index_entry):
    era = ""
    geography = ""
    if isinstance(index_entry, dict):
        era = index_entry.get("era") or index_entry.get(
            "compDateString") or ""
        geography = index_entry.get("compPlaceString") or ""
    return era, geography


def _add_search_library_result(
    ref_value, results, seen_refs, index_cache, fix_map, remove_keys,
    normalized_category_filters, metadata_filters,
    label="", explicit_categories=None, explicit_he_ref="",
):
    """Normalize, dedupe, and (if it passes filters) append one search hit
    to `results`, mutating `results`/`seen_refs`/`index_cache` in place.
    Moved to module level (out of search_library()) since a nested
    closure's own branches count against the enclosing function's
    complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    ref_value = str(ref_value or "").strip()
    if not ref_value:
        return

    book = ref_value.split(",", 1)[0].strip()
    index_entry = _get_search_index_metadata(book, index_cache)

    ref_value, candidate_titles = _resolve_search_result_fixed_ref(
        ref_value, label, book, index_entry, fix_map)
    if _is_search_result_removed(ref_value, candidate_titles, remove_keys):
        return

    if ref_value in seen_refs:
        return

    book = ref_value.split(",", 1)[0].strip()
    index_entry = _get_search_index_metadata(book, index_cache)

    categories = _resolve_search_result_categories(
        index_entry, explicit_categories, normalized_category_filters)
    if categories is None:
        return

    authors = _resolve_search_result_authors(index_entry)
    era, geography = _resolve_search_result_era_geography(index_entry)

    result = {
        "ref": ref_value,
        "heRef": str(explicit_he_ref or "").strip(),
        "text": str(label or ref_value).strip(),
        "categories": categories,
        "path": str(label or ref_value).strip(),
        "authors": authors,
        "era": era,
        "geography": geography,
        "nusach": _infer_nusach(ref_value, categories),
    }

    if not _matches_metadata_filters(result, metadata_filters):
        return

    seen_refs.add(ref_value)
    results.append(result)


def search_library(query, size=10, filters=None, metadata_filters=None):
    """
    Full text search across all of Sefaria.
    Returns a list of results with refs, text snippets, and categories.
    """
    query = (query or "").strip()
    if not query:
        return []

    size = max(1, int(size or 10))

    normalized_category_filters = [
        str(value).strip().lower()
        for value in (filters or [])
        if str(value).strip()
    ]

    results = []
    seen_refs = set()
    index_cache = {}
    adjustments = _load_library_index_adjustments()
    remove_keys = adjustments.get("remove_keys", set())
    fix_map = adjustments.get("fix_map", {})

    def add_result(ref_value, label="", explicit_categories=None, explicit_he_ref=""):
        _add_search_library_result(
            ref_value, results, seen_refs, index_cache, fix_map, remove_keys,
            normalized_category_filters, metadata_filters,
            label=label, explicit_categories=explicit_categories,
            explicit_he_ref=explicit_he_ref,
        )

    # Fast direct completion from /name for exact refs/books.
    name_data = _cached_get(
        f"{SEFARIA_API}/name/{_encode_ref_path(query)}", ttl=300)
    if isinstance(name_data, dict):
        if name_data.get("is_ref") and name_data.get("ref"):
            ref_value = name_data.get("ref")
            if name_data.get("is_book"):
                ref_value = _resolve_opening_ref_for_title(ref_value)
            add_result(ref_value, name_data.get("book", ""))

        for obj in name_data.get("completion_objects", []) or []:
            if obj.get("type") != "ref":
                continue
            ref_value = obj.get("key") or obj.get("title")
            if obj.get("is_book"):
                ref_value = _resolve_opening_ref_for_title(ref_value)
            add_result(ref_value, obj.get("title", ""))
            if len(results) >= size:
                return results[:size]

    # Catalog fallback for modern works that do not resolve well from /name search.
    for row in _search_index_catalog(query, size=max(size * 2, 16), metadata_filters=metadata_filters):
        add_result(
            row.get("ref", ""),
            row.get("text", ""),
            explicit_categories=row.get("categories", []),
            explicit_he_ref=row.get("heRef", ""),
        )
        if len(results) >= size:
            break

    return results[:size]


def get_linked_texts(ref):
    """
    Fetches all texts linked to a given ref (commentaries, parallel texts, etc.)
    Returns grouped links by type.
    """
    safe_ref = _encode_ref_path(ref)
    data = _cached_get(f"{SEFARIA_API}/related/{safe_ref}", ttl=3600)
    if not data:
        return {}

    links = data.get("links", [])
    grouped = {}
    for link in links:
        link_type = link.get("type", "Other")
        category = link.get("category", link_type)
        if category not in grouped:
            grouped[category] = []
        grouped[category].append({
            "ref": link.get("ref", ""),
            "heRef": link.get("heRef", ""),
            "anchorRef": link.get("anchorRef", "")
        })

    # Ensure classic Torah commentary availability: add a canonical Rashi layer when
    # related-link metadata omits it for Tanakh chapter/verse references.
    ref_stripped = str(ref or "").strip()
    # Split off the trailing whitespace-separated token via str.rsplit (a
    # linear-time C-level scan, no backtracking) rather than a single regex
    # spanning the whole string. Newlines are rejected up front: the original
    # regex's "." never matches "\n", so any embedded newline before the
    # chapter/verse token always failed that match, and rsplit(None, 1)
    # would otherwise treat "\n" as an ordinary whitespace separator.
    ref_match = None if "\n" in ref_stripped else ref_stripped.rsplit(None, 1)
    book = ref_match[0] if ref_match and len(ref_match) == 2 else ""
    match = _TRAILING_CHAPTER_VERSE_RE.match(ref_match[1]) if book else None
    if match:
        chapter = str(match.group(1) or "").strip()
        verse = str(match.group(2) or "1").strip() or "1"

        torah_books = {"Genesis", "Exodus",
                       "Leviticus", "Numbers", "Deuteronomy"}
        tanakh_books = torah_books | {
            "Joshua", "Judges", "I Samuel", "II Samuel", "I Kings", "II Kings",
            "Isaiah", "Jeremiah", "Ezekiel", "Hosea", "Joel", "Amos", "Obadiah",
            "Jonah", "Micah", "Nahum", "Habakkuk", "Zephaniah", "Haggai", "Zechariah",
            "Malachi", "Psalms", "Proverbs", "Job", "Song of Songs", "Ruth",
            "Lamentations", "Ecclesiastes", "Esther", "Daniel", "Ezra", "Nehemiah",
            "I Chronicles", "II Chronicles",
        }

        if book in tanakh_books:
            rashi_ref = f"Rashi on {book} {chapter}:{verse}"
            has_rashi = any(
                str(item.get("ref", "")).lower().startswith("rashi on ")
                for items in grouped.values()
                for item in (items or [])
                if isinstance(item, dict)
            )
            if not has_rashi:
                grouped.setdefault("Commentary", []).insert(0, {
                    "ref": rashi_ref,
                    "heRef": "",
                    "anchorRef": str(ref or ""),
                })

    return grouped


def get_popular_texts():
    """
    Returns a curated list of canonical starting texts
    organized by category for the Library homepage.
    """
    return {
        "Tanakh": [
            {"title": "Genesis (Bereishit)", "ref": "Genesis 1", "he": "בְּרֵאשִׁית",
                "description": "The beginning of creation"},
            {"title": "Exodus (Shemot)", "ref": "Exodus 1", "he": "שְׁמוֹת",
                "description": "The journey out of Egypt"},
            {"title": "Psalms (Tehillim)", "ref": "Psalms 1",
                "he": "תְּהִלִּים", "description": "Psalms of David"},
            {"title": "Proverbs (Mishlei)", "ref": "Proverbs 1", "he": "מִשְׁלֵי",
                "description": "Proverbs of Solomon"},
        ],
        "Mishnah": [
            {"title": "Berakhot", "ref": "Mishnah Berakhot 1",
                "he": "בְּרָכוֹת", "description": "Laws of berakhot and prayer"},
            {"title": "Shabbat", "ref": "Mishnah Shabbat 1",
                "he": "שַׁבָּת", "description": "Laws of Shabbat"},
            {"title": "Pesachim", "ref": "Mishnah Pesachim 1",
                "he": "פְּסָחִים", "description": "Laws of Pesach"},
            {"title": "Avot", "ref": "Pirkei Avot 1", "he": "אָבוֹת",
                "description": "Ethics of the Fathers"},
        ],
        "Talmud": [
            {"title": "Berakhot 2a", "ref": "Berakhot 2a", "he": "בְּרָכוֹת",
                "description": "First page of Talmud Bavli"},
            {"title": "Shabbat 2a", "ref": "Shabbat 2a", "he": "שַׁבָּת",
                "description": "Laws of Shabbat in depth"},
            {"title": "Sanhedrin 37a", "ref": "Sanhedrin 37a",
                "he": "סַנְהֶדְרִין", "description": "Whoever saves one life..."},
        ],
        "Halakhah": [
            {"title": "Shulchan Arukh OC 1", "ref": "Shulchan Arukh, Orach Chayim 1",
                "he": "שֻׁלְחָן עָרוּךְ", "description": "Morning conduct"},
            {"title": "Mishneh Torah", "ref": "Mishneh Torah, Torah Study 1",
                "he": "מִשְׁנֵה תּוֹרָה", "description": "Laws of Torah study"},
            {"title": "Kitzur Shulchan Arukh 1", "ref": "Kitzur Shulchan Arukh 1",
                "he": "קִצּוּר שֻׁלְחָן עָרוּךְ", "description": "Abridged code of Jewish law"},
        ]
    }


def _get_en_title(node):
    """Extract the English title for a schema node when available."""
    titles = node.get("titles", []) if isinstance(node, dict) else []
    for item in titles:
        if isinstance(item, dict) and item.get("lang") == "en" and item.get("text"):
            return item["text"]
    if isinstance(node, dict):
        return node.get("title") or node.get("key") or ""
    return ""


def _normalize_title_for_compare(text):
    if not text:
        return ""
    return "".join(ch.lower() for ch in text if ch.isalnum())


def _is_same_work_title(a, b):
    """Heuristic match for equivalent work titles with minor spelling differences."""
    a_norm = _normalize_title_for_compare(a)
    b_norm = _normalize_title_for_compare(b)
    if not a_norm or not b_norm:
        return False
    if a_norm == b_norm or a_norm in b_norm or b_norm in a_norm:
        return True
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= 0.85


def get_index_entry(title):
    """Fetch a text's index record by title."""
    safe_title = _encode_ref_path(title)
    return _cached_get(f"{SEFARIA_API}/index/{safe_title}", ttl=86400) or {}


_CURLY_QUOTE_REPLACEMENTS = {
    "‘": "'", "’": "'", "‚": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-",
}


def _normalize_liturgy_title_text(title):
    """Strip curly quotes/dashes and collapse whitespace for an exact-match
    retry against Sefaria's /api/index/<title> endpoint (which requires an
    exact title string and 404s on the smallest mismatch)."""
    text = str(title or "")
    for src, dst in _CURLY_QUOTE_REPLACEMENTS.items():
        text = text.replace(src, dst)
    return re.sub(r"\s+", " ", text).strip()


def _lookup_canonical_index_title(title):
    """Ask Sefaria's /api/name/<title> completion endpoint for the canonical
    index title when an exact /api/index/<title> lookup fails to resolve.
    Bounded to a single request; returns "" (never raises) when Sefaria has
    no match, so a genuine "this title doesn't exist" case degrades cleanly.
    """
    query = str(title or "").strip()
    if not query:
        return ""
    safe_title = _encode_ref_path(query)
    name_data = _cached_get(f"{SEFARIA_API}/name/{safe_title}", ttl=43200)
    if not isinstance(name_data, dict):
        return ""

    for key in ("book", "index", "key", "title"):
        candidate = str(name_data.get(key) or "").strip()
        if candidate and candidate.lower() != query.lower():
            return candidate

    for obj in name_data.get("completion_objects", []) or []:
        if not isinstance(obj, dict):
            continue
        candidate = str(obj.get("title") or obj.get("key") or "").strip()
        if candidate and candidate.lower() != query.lower():
            return candidate

    return ""


def _walk_liturgy_books(node, books, seen, include_commentary):
    """Recursive descent of the library index collecting liturgy prayer
    books into `books`. Moved to module level (out of get_liturgy_books())
    since a nested closure's own branches count against the enclosing
    function's complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    if isinstance(node, list):
        for child in node:
            _walk_liturgy_books(child, books, seen, include_commentary)
        return

    if not isinstance(node, dict):
        return

    categories = node.get("categories", []) or []
    title = node.get("title", "")
    dependence = node.get("dependence")

    if categories and categories[0] == "Liturgy" and title:
        if _normalize_title_key(title) in NON_LOADING_LITURGY_TITLES:
            return
        if include_commentary or dependence != "Commentary":
            if title not in seen:
                books.append({
                    "name": title,
                    "title": title,
                    "categories": categories,
                })
                seen.add(title)

    if "contents" in node:
        _walk_liturgy_books(node.get("contents"), books, seen, include_commentary)
    if "children" in node:
        _walk_liturgy_books(node.get("children"), books, seen, include_commentary)


def get_liturgy_books(include_commentary=False, max_items=200):
    """Return discoverable liturgy prayer books from Sefaria index."""
    index = get_library_index()
    if not isinstance(index, list):
        return []

    books = []
    seen = set()
    _walk_liturgy_books(index, books, seen, include_commentary)
    books.sort(key=lambda x: x.get("title", ""))
    return books[:max_items]


def _resolve_index_schema_with_fallbacks(title):
    """Resolve a Sefaria index title to its schema, retrying with title
    normalization and canonical-name lookup if the direct lookup 404s.
    Returns (schema, resolved_title). Split out of get_index_leaf_refs()
    to keep this fallback chain out of that function's own complexity
    count (SonarCloud python:S3776).
    """
    entry = get_index_entry(title)
    schema = entry.get("schema", {}) if isinstance(entry, dict) else {}

    if not schema:
        # Fallback A: Sefaria's /api/index/<title> endpoint is an exact-match
        # lookup, so a title copied with curly quotes or stray whitespace
        # (common when titles come from user input or a partially-matching
        # catalog entry) silently 404s and this returns []. Retry once with
        # a normalized title before giving up.
        normalized_title = _normalize_liturgy_title_text(title)
        if normalized_title and normalized_title != str(title or ""):
            entry = get_index_entry(normalized_title)
            schema = entry.get("schema", {}) if isinstance(entry, dict) else {}
            if schema:
                title = normalized_title

        # Fallback B: still nothing -- ask Sefaria's /api/name/ completion
        # endpoint for the canonical title (handles anything beyond simple
        # punctuation drift, e.g. abbreviations or alternate spellings) and
        # retry the exact index lookup once with that. Bounded to a single
        # extra round-trip; a genuine "doesn't exist" title still falls
        # through to an empty list below rather than raising.
        if not schema:
            canonical_title = _lookup_canonical_index_title(title)
            if canonical_title and canonical_title != str(title or ""):
                entry = get_index_entry(canonical_title)
                schema = entry.get("schema", {}) if isinstance(entry, dict) else {}
                if schema:
                    title = canonical_title

    return schema, title


def _add_leaf_ref(title, segments, seen, refs):
    """Append one leaf ref (title + non-empty path segments) if not
    already seen, mutating `seen`/`refs` in place. Split out of
    get_index_leaf_refs() (SonarCloud python:S3776) -- see
    _walk_index_schema_for_leaf_refs.
    """
    ref = ", ".join([title] + [s for s in segments if s])
    if ref not in seen:
        seen.add(ref)
        refs.append(ref)


def _walk_index_schema_for_leaf_refs(node, path_segments, title, title_norm, seen, refs, max_refs):
    """Recursive descent of a Sefaria index schema, collecting leaf refs
    into `refs`. Moved to module level (out of get_index_leaf_refs())
    since a nested closure's own branches count against the enclosing
    function's complexity, but a top-level function's don't (SonarCloud
    python:S3776).
    """
    if len(refs) >= max_refs:
        return
    if not isinstance(node, dict):
        return

    children = node.get("nodes", [])
    node_title = _get_en_title(node)
    node_key = node.get("key", "")
    node_norm = _normalize_title_for_compare(node_title)

    # Default nodes inherit parent path and should not add an extra segment.
    add_segment = bool(node_title) and node_key != "default" and not _is_same_work_title(
        node_title, title) and node_norm != title_norm
    next_path = path_segments + ([node_title] if add_segment else [])

    if children:
        for child in children:
            _walk_index_schema_for_leaf_refs(
                child, next_path, title, title_norm, seen, refs, max_refs)
        return

    _add_leaf_ref(title, next_path, seen, refs)


def get_index_leaf_refs(title, max_refs=120):
    """Build leaf refs from a text schema (e.g., full Siddur structure)."""
    schema, title = _resolve_index_schema_with_fallbacks(title)

    if not schema:
        return []

    refs = []
    seen = set()
    title_norm = _normalize_title_for_compare(title)

    _walk_index_schema_for_leaf_refs(
        schema, [], title, title_norm, seen, refs, max_refs)

    if not refs:
        refs.append(title)
    return refs[:max_refs]
