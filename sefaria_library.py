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
import functools
import time
import difflib

SEFARIA_API = "https://www.sefaria.org/api"

# Simple in-memory cache with TTL
_cache = {}
CACHE_TTL = 3600  # 1 hour


def _cached_get(url, ttl=CACHE_TTL):
    """Cached HTTP GET wrapper."""
    now = time.time()
    if url in _cache and now - _cache[url]['ts'] < ttl:
        return _cache[url]['data']
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        _cache[url] = {'data': data, 'ts': now}
        return data
    except Exception as e:
        print(f"[Sefaria Library] Error fetching {url}: {e}")
        return None


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
    # Clean the ref for URL encoding
    safe_ref = ref.replace(" ", "_").replace(
        ":", ".").replace("/", "_").replace("&", "%26")
    url = f"{SEFARIA_API}/texts/{safe_ref}?lang={lang}&context={context}&pad=0"
    data = _cached_get(url, ttl=86400)  # cache texts for 24h

    if not data or "error" in data:
        return {"error": f"Text not found: {ref}", "ref": ref, "he": [], "en": []}

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

    return {
        "ref": data.get("ref", ref),
        "title": data.get("title", ""),
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


def get_full_book(title, start_section=1, max_sections=10):
    """
    Fetches multiple sections of a book for the inline reader.
    Returns a list of text objects.
    """
    results = []
    for i in range(start_section, start_section + max_sections):
        ref = f"{title} {i}"
        text = get_text(ref)
        if "error" not in text and (text["he"] or text["en"]):
            results.append(text)
        else:
            break
    return results


def search_library(query, size=10, filters=None):
    """
    Full text search across all of Sefaria.
    Returns a list of results with refs, text snippets, and categories.
    """
    query = (query or "").strip()
    if not query:
        return []

    size = max(1, int(size or 10))

    # Legacy full-text search endpoint (may be unavailable for some deployments).
    params = {
        "q": query,
        "size": size,
        "type": "text"
    }
    if filters:
        params["filters"] = ",".join(filters)

    param_str = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{SEFARIA_API}/search-wrapper?{param_str}"

    data = _cached_get(url, ttl=300)  # short cache for search
    hits = []
    if isinstance(data, dict):
        hits = data.get("hits", {}).get("hits", []) or []

    results = []
    for hit in hits:
        src = hit.get("_source", {})
        ref = src.get("ref", "")
        if not ref:
            continue
        results.append({
            "ref": ref,
            "heRef": src.get("heRef", ""),
            "text": src.get("exact", "")[:300],  # snippet
            "categories": src.get("categories", []),
            "path": src.get("path", "")
        })

    if results:
        return results[:size]

    # Fallback search via Sefaria name completion endpoint.
    safe_query = query.replace(" ", "_").replace("&", "%26")
    name_data = _cached_get(f"{SEFARIA_API}/name/{safe_query}", ttl=300)
    if not isinstance(name_data, dict):
        return []

    index_cache = {}
    seen_refs = set()

    def get_categories_for_ref(ref_value):
        book = (ref_value or "").split(",", 1)[0].strip()
        if not book:
            return []
        if book not in index_cache:
            idx = get_index_entry(book)
            index_cache[book] = idx.get(
                "categories", []) if isinstance(idx, dict) else []
        return index_cache.get(book, [])

    def add_ref_result(ref_value, title_value=""):
        ref_value = (ref_value or "").strip()
        if not ref_value or ref_value in seen_refs:
            return
        seen_refs.add(ref_value)
        results.append({
            "ref": ref_value,
            "heRef": "",
            "text": title_value or ref_value,
            "categories": get_categories_for_ref(ref_value),
            "path": ref_value,
        })

    if name_data.get("is_ref") and name_data.get("ref"):
        add_ref_result(name_data.get("ref"), name_data.get("book", ""))

    for obj in name_data.get("completion_objects", []) or []:
        if obj.get("type") != "ref":
            continue
        ref_value = obj.get("key") or obj.get("title")
        add_ref_result(ref_value, obj.get("title", ""))
        if len(results) >= size:
            break

    return results[:size]


def get_linked_texts(ref):
    """
    Fetches all texts linked to a given ref (commentaries, parallel texts, etc.)
    Returns grouped links by type.
    """
    safe_ref = ref.replace(" ", "_")
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
            {"title": "Bereishit", "ref": "Genesis 1", "he": "בְּרֵאשִׁית",
                "description": "The beginning of creation"},
            {"title": "Shemot", "ref": "Exodus 1", "he": "שְׁמוֹת",
                "description": "The Exodus from Egypt"},
            {"title": "Tehillim", "ref": "Psalms 1",
                "he": "תְּהִלִּים", "description": "Psalms of David"},
            {"title": "Mishlei", "ref": "Proverbs 1", "he": "מִשְׁלֵי",
                "description": "Proverbs of Solomon"},
        ],
        "Mishnah": [
            {"title": "Berakhot", "ref": "Mishnah Berakhot 1",
                "he": "בְּרָכוֹת", "description": "Laws of blessings and prayer"},
            {"title": "Shabbat", "ref": "Mishnah Shabbat 1",
                "he": "שַׁבָּת", "description": "Laws of the Sabbath"},
            {"title": "Pesachim", "ref": "Mishnah Pesachim 1",
                "he": "פְּסָחִים", "description": "Laws of Passover"},
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
                "he": "מִשְׁנֵה תּוֹרָה", "description": "The laws of Torah study"},
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
    safe_title = title.replace(" ", "_").replace("&", "%26")
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
