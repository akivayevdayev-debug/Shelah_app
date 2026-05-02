"""
External knowledge search connectors.

Contains lightweight wrappers for:
- Wikipedia summaries.
- Hebcal daily-learning feed parsing.
- Halachipedia search and extract retrieval.

These helpers are intentionally simple and resilient because they are best-effort
enrichment sources, not the primary authoritative text source.
"""

import requests
import time

_HTTP = requests.Session()
_CACHE_TTL_SECONDS = 60 * 10
_WIKI_CACHE = {}
_HALACHIPEDIA_CACHE = {}
_DAILY_CACHE = {"ts": 0, "data": None}


def _cached_lookup(store, key):
    row = store.get(key)
    if not row:
        return None
    if time.time() - row.get("ts", 0) > _CACHE_TTL_SECONDS:
        return None
    return row.get("value")


def _cached_store(store, key, value):
    store[key] = {"ts": time.time(), "value": value}


def search_wikipedia(title):
    cache_key = str(title or "").strip().lower()
    if cache_key:
        cached = _cached_lookup(_WIKI_CACHE, cache_key)
        if cached is not None:
            return cached

    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}"
        print("[Wiki Request]", url)

        r = _HTTP.get(url, timeout=10)
        print("[Wiki Status]", r.status_code)

        if r.status_code == 200:
            data = r.json()
            print("[Wiki Found]", data.get("title"))

            payload = {
                "title": data.get("title", ""),
                "summary": data.get("extract", "")[:300]
            }
            if cache_key:
                _cached_store(_WIKI_CACHE, cache_key, payload)
            return payload
    except Exception as e:
        print("[Wiki Error]", e)

    return None


def get_daily_learning():
    """Fetch daily portions using Hebcal API"""
    if _DAILY_CACHE.get("data") and time.time() - _DAILY_CACHE.get("ts", 0) < 60 * 5:
        return _DAILY_CACHE.get("data")

    try:
        url = "https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on&nx=on&year=now&month=now&ss=on&mf=on&c=on&geo=zip&zip=11213"
        response = _HTTP.get(url, timeout=10)
        data = response.json()

        items = data.get('items', [])

        parsha = None
        rambam_portions = []

        for i in items:
            title = i.get('title', '')
            if 'Parashat' in title:
                parsha = title
            elif 'Rambam' in title or 'Chitas' in title:
                rambam_portions.append(title)

        payload = {
            "parsha": parsha,
            "portions": rambam_portions
        }
        _DAILY_CACHE["ts"] = time.time()
        _DAILY_CACHE["data"] = payload
        return payload
    except Exception as e:
        print("[Hebcal Error]", e)
        return {"parsha": None, "portions": []}


def search_halachipedia(query):
    """Search Halachipedia MediaWiki API for relevant articles"""
    cache_key = str(query or "").strip().lower()
    if cache_key:
        cached = _cached_lookup(_HALACHIPEDIA_CACHE, cache_key)
        if cached is not None:
            return cached

    try:
        # Search for title
        search_url = f"https://halachipedia.com/api.php?action=query&list=search&srsearch={query}&utf8=&format=json"

        r_search = _HTTP.get(search_url, timeout=10)
        data = r_search.json()

        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            return None

        top_title = search_results[0]["title"]

        # Get intro extract for top article
        extract_url = f"https://halachipedia.com/api.php?action=query&prop=extracts&exsentences=10&exintro=1&explaintext=1&titles={top_title}&format=json"
        r_extract = _HTTP.get(extract_url, timeout=10)
        ext_data = r_extract.json()

        pages = ext_data.get("query", {}).get("pages", {})
        for page_info in pages.values():
            payload = {
                "title": f"[Halachipedia] {page_info.get('title', '')}",
                "summary": page_info.get("extract", "")[:1000]
            }
            if cache_key:
                _cached_store(_HALACHIPEDIA_CACHE, cache_key, payload)
            return payload

        return None
    except Exception as e:
        print(f"[Halachipedia Error] {e}")
        return None
