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
from urllib.parse import quote, urlencode

SEFARIA_API = "https://www.sefaria.org/api"

# Simple in-memory cache with TTL
_cache = {}
CACHE_TTL = 3600  # 1 hour
_http_session = requests.Session()
_resolved_title_ref_cache = {}
_resolved_query_ref_cache = {}
_title_catalog_cache = {"ts": 0, "data": []}
_search_query_cache = {}


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


def _cached_get(url, ttl=CACHE_TTL):
    """Cached HTTP GET wrapper."""
    now = time.time()
    if url in _cache and now - _cache[url]['ts'] < ttl:
        return _cache[url]['data']
    try:
        r = _http_session.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        _cache[url] = {'data': data, 'ts': now}
        return data
    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else None
        if status_code not in (400, 404):
            print(f"[Sefaria Library] Error fetching {url}: {e}")
        return None
    except Exception as e:
        print(f"[Sefaria Library] Error fetching {url}: {e}")
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


def _build_text_url(ref, lang="both", context=0):
    encoded_ref = _encode_ref_path(ref)
    params = urlencode({"lang": lang, "context": context, "pad": 0})
    return f"{SEFARIA_API}/texts/{encoded_ref}?{params}"


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


def _resolve_opening_ref_for_title(title):
    """Resolve a title to an opening ref that /texts can load."""
    cache_key = _normalize_title_key(title)
    if cache_key and cache_key in _resolved_title_ref_cache:
        return _resolved_title_ref_cache[cache_key]

    entry = get_index_entry(title)
    if not isinstance(entry, dict) or entry.get("error"):
        resolved = str(title or "").strip()
        if cache_key:
            _resolved_title_ref_cache[cache_key] = resolved
        return resolved

    canonical_title = str(entry.get("title") or title or "").strip()
    first_section = entry.get("firstSectionRef") or entry.get("firstSection")
    if isinstance(first_section, str) and first_section.strip():
        resolved = first_section.strip()
    elif _is_specific_ref_query(canonical_title):
        resolved = canonical_title
    else:
        leaf_refs = get_index_leaf_refs(canonical_title, max_refs=1)
        if leaf_refs:
            resolved = leaf_refs[0]
        elif entry.get("sectionNames"):
            resolved = f"{canonical_title} 1"
        else:
            resolved = canonical_title

    if cache_key:
        _resolved_title_ref_cache[cache_key] = resolved
    return resolved


def _resolve_ref_candidates(raw_ref, max_candidates=12):
    """Build candidate refs for texts that require canonical title or leaf-node resolution."""
    raw = str(raw_ref or "").strip()
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

    title_part, suffix = _split_title_suffix(raw)
    is_specific_ref = _is_specific_ref_query(raw)

    safe_name = _encode_ref_path(raw)
    name_data = _cached_get(f"{SEFARIA_API}/name/{safe_name}", ttl=43200)
    if isinstance(name_data, dict):
        if name_data.get("is_ref") and name_data.get("ref"):
            add(name_data.get("ref"))

        for obj in name_data.get("completion_objects", []) or []:
            if obj.get("type") != "ref":
                continue
            add(obj.get("key") or obj.get("title"))
            if len(candidates) >= max_candidates:
                return candidates[:max_candidates]

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

    for title in candidate_titles:
        entry = get_index_entry(title)
        if not isinstance(entry, dict) or entry.get("error"):
            continue

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
                    return candidates[:max_candidates]

        if len(candidates) >= max_candidates:
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
    now = time.time()
    cached_data = _title_catalog_cache.get("data", [])
    cached_ts = _title_catalog_cache.get("ts", 0)
    if cached_data and now - cached_ts < ttl:
        return cached_data

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

    _title_catalog_cache["ts"] = now
    _title_catalog_cache["data"] = rows
    return rows


def _search_index_catalog(query, size=10, metadata_filters=None):
    normalized_query = re.sub(
        r"[^0-9a-z\u0590-\u05ff]+", " ", str(query or "").lower()).strip()
    tokens = [token for token in normalized_query.split() if token]
    if not tokens:
        return []

    cache_key = f"{normalized_query}|{size}|{str(metadata_filters or {})}"
    now = time.time()
    if cache_key in _search_query_cache:
        cached = _search_query_cache[cache_key]
        if now - cached["ts"] < 300:
            return cached["data"]

    joined_query = " ".join(tokens)
    ranked_rows = []
    for row in _get_title_catalog():
        haystack = row.get("search", "")
        if not all(token in haystack for token in tokens):
            continue

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

        ranked_rows.append((score, row))

    ranked_rows.sort(key=lambda item: (-item[0], item[1].get("title", "")))

    results = []
    seen_refs = set()
    for _, row in ranked_rows[:max(size * 5, 30)]:
        opening_ref = _resolve_opening_ref_for_title(row.get("title", ""))
        if not opening_ref:
            continue
        if opening_ref in seen_refs:
            continue

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
            continue

        seen_refs.add(opening_ref)
        results.append(result)
        if len(results) >= size:
            break

    _search_query_cache[cache_key] = {"ts": now, "data": results}
    return results


def get_library_index():
    """
    Fetches the full Sefaria library category tree.
    Returns a nested structure of categories and texts.
    """
    data = _cached_get(f"{SEFARIA_API}/index")
    if not data:
        return []
    return data


def get_category_contents(category_path):
    """
    Returns all books/texts under a given category path.
    category_path: e.g. "Tanakh" or "Tanakh/Torah"
    """
    encoded = category_path.replace("/", ",")
    data = _cached_get(f"{SEFARIA_API}/index/{encoded}")
    if data and not (isinstance(data, dict) and data.get("error")):
        return data

    # Fallback for category paths that Sefaria's /index/{path} does not resolve.
    parts = [p.strip() for p in (category_path or "").split("/") if p.strip()]
    if not parts:
        return []

    node = get_library_index()
    for part in parts:
        candidates = []
        if isinstance(node, list):
            candidates = node
        elif isinstance(node, dict):
            candidates = node.get("contents", []) or []
        else:
            return []

        next_node = None
        part_norm = _normalize_title_for_compare(part)
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
                next_node = candidate
                break

        if not next_node:
            return []
        node = next_node

    return node if isinstance(node, (dict, list)) else []


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
    requested_ref = str(ref or "").strip()
    if not requested_ref:
        return {"error": "Text not found", "ref": "", "he": [], "en": []}

    cache_key = requested_ref.lower()
    resolved_ref = ""
    attempts_tried = set()

    data = None
    cached_ref = _resolved_query_ref_cache.get(cache_key, "")
    initial_attempts = [cached_ref,
                        requested_ref] if cached_ref else [requested_ref]
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
            data = attempt_data
            resolved_ref = attempt_data.get("ref") or attempt_ref
            break

    if not data:
        for candidate in _resolve_ref_candidates(requested_ref):
            lowered = candidate.lower()
            if lowered in attempts_tried:
                continue
            attempts_tried.add(lowered)

            candidate_data = _cached_get(_build_text_url(
                candidate, lang, context), ttl=86400)
            if candidate_data and "error" not in candidate_data:
                data = candidate_data
                resolved_ref = candidate_data.get("ref") or candidate
                break

    if not data or "error" in data:
        _resolved_query_ref_cache.pop(cache_key, None)
        return {"error": f"Text not found: {requested_ref}", "ref": requested_ref, "he": [], "en": []}

    _resolved_query_ref_cache[cache_key] = str(
        resolved_ref or data.get("ref") or requested_ref)

    # Flatten nested arrays for display
    he_raw = data.get("he", [])
    en_raw = data.get("text", [])

    def flatten_with_path(arr, path=None):
        """Flatten nested text into (path, string) for structural alignment."""
        path = path or ()
        if isinstance(arr, str):
            text = arr.strip()
            return [(path, text)] if text else []
        if isinstance(arr, list):
            result = []
            for idx, item in enumerate(arr, start=1):
                result.extend(flatten_with_path(item, path + (idx,)))
            return result
        return []

    he_leafs = flatten_with_path(he_raw)
    en_leafs = flatten_with_path(en_raw)

    he_by_path = {p: t for p, t in he_leafs}
    en_by_path = {p: t for p, t in en_leafs}
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

    def get_index_metadata(book):
        title = str(book or "").strip()
        if not title:
            return {}
        if title not in index_cache:
            data = get_index_entry(title)
            index_cache[title] = data if isinstance(data, dict) else {}
        return index_cache[title]

    def add_result(ref_value, label="", explicit_categories=None, explicit_he_ref=""):
        ref_value = str(ref_value or "").strip()
        if not ref_value or ref_value in seen_refs:
            return

        book = ref_value.split(",", 1)[0].strip()
        index_entry = get_index_metadata(book)

        categories = []
        if isinstance(explicit_categories, list) and explicit_categories:
            categories = [str(item) for item in explicit_categories if item]
        if not categories and isinstance(index_entry, dict):
            categories = [str(item)
                          for item in index_entry.get("categories", []) if item]

        if normalized_category_filters:
            category_blob = " ".join(categories).lower()
            if not any(token in category_blob for token in normalized_category_filters):
                return

        authors = []
        if isinstance(index_entry, dict):
            authors = index_entry.get("authors", [])
        if isinstance(authors, str):
            authors = [authors]
        if not isinstance(authors, list):
            authors = []

        era = ""
        geography = ""
        if isinstance(index_entry, dict):
            era = index_entry.get("era") or index_entry.get(
                "compDateString") or ""
            geography = index_entry.get("compPlaceString") or ""

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


def get_liturgy_books(include_commentary=False, max_items=200):
    """Return discoverable liturgy prayer books from Sefaria index."""
    index = get_library_index()
    if not isinstance(index, list):
        return []

    books = []
    seen = set()

    def walk(node):
        if isinstance(node, list):
            for child in node:
                walk(child)
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
            walk(node.get("contents"))
        if "children" in node:
            walk(node.get("children"))

    walk(index)
    books.sort(key=lambda x: x.get("title", ""))
    return books[:max_items]


def get_index_leaf_refs(title, max_refs=120):
    """Build leaf refs from a text schema (e.g., full Siddur structure)."""
    entry = get_index_entry(title)
    schema = entry.get("schema", {}) if isinstance(entry, dict) else {}
    if not schema:
        return []

    refs = []
    seen = set()

    def add_ref(segments):
        ref = ", ".join([title] + [s for s in segments if s])
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)

    title_norm = _normalize_title_for_compare(title)

    def walk(node, path_segments):
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
                walk(child, next_path)
            return

        add_ref(next_path)

    walk(schema, [])
    if not refs:
        refs.append(title)
    return refs[:max_refs]
