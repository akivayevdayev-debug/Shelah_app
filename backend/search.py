"""
External knowledge search connectors.

Contains lightweight wrappers for:
- Wikipedia summaries.
- Hebcal daily-learning feed parsing.
- Halachipedia search and extract retrieval.

These helpers are intentionally simple and resilient because they are best-effort
enrichment sources, not the primary authoritative text source.
"""

import logging
import requests
import httpx
import time
import re
from html import unescape
from urllib.parse import quote_plus, urljoin

from backend.cache import TTLCache

logger = logging.getLogger(__name__)

_HTTP = requests.Session()
_ASYNC_HTTP_CLIENT: httpx.AsyncClient | None = None
_CACHE_TTL_SECONDS = 60 * 10
_CACHE_MAX_SIZE = 256
_DAILY_CACHE_KEY = "daily_learning"
_WIKI_CACHE = TTLCache(maxsize=_CACHE_MAX_SIZE, ttl=_CACHE_TTL_SECONDS)
_HALACHIPEDIA_CACHE = TTLCache(maxsize=_CACHE_MAX_SIZE, ttl=_CACHE_TTL_SECONDS)
_HEBREWBOOKS_CACHE = TTLCache(maxsize=_CACHE_MAX_SIZE, ttl=_CACHE_TTL_SECONDS)
_DAILY_CACHE = TTLCache(ttl=60 * 5)


def _get_async_client() -> httpx.AsyncClient:
    """Lazily-created, process-wide httpx.AsyncClient shared by the three
    async_search_* connectors below so each call reuses pooled connections
    instead of paying a fresh handshake every time (plan.md §3.6)."""
    global _ASYNC_HTTP_CLIENT
    if _ASYNC_HTTP_CLIENT is None:
        _ASYNC_HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)
    return _ASYNC_HTTP_CLIENT


def search_wikipedia(title):
    cache_key = str(title or "").strip().lower()
    if cache_key:
        cached = _WIKI_CACHE.get(cache_key)
        if cached is not None:
            return cached

    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}"
        logger.debug("[Wiki Request] %s", url)

        r = _HTTP.get(url, timeout=10)
        logger.debug("[Wiki Status] %s", r.status_code)

        if r.status_code == 200:
            data = r.json()
            logger.debug("[Wiki Found] %s", data.get("title"))

            payload = {
                "title": data.get("title", ""),
                "summary": data.get("extract", "")[:300]
            }
            if cache_key:
                _WIKI_CACHE.set(cache_key, payload)
            return payload
    except Exception as e:
        logger.warning("[Wiki Error] %s", e)

    return None


def get_daily_learning():
    """Fetch daily portions using Hebcal API"""
    cached_daily = _DAILY_CACHE.get(_DAILY_CACHE_KEY)
    if cached_daily:
        return cached_daily

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
        _DAILY_CACHE.set(_DAILY_CACHE_KEY, payload)
        return payload
    except Exception as e:
        logger.warning("[Hebcal Error] %s", e)
        return {"parsha": None, "portions": []}


def search_halachipedia(query):
    """Search Halachipedia MediaWiki API for relevant articles"""
    cache_key = str(query or "").strip().lower()
    if cache_key:
        cached = _HALACHIPEDIA_CACHE.get(cache_key)
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
                _HALACHIPEDIA_CACHE.set(cache_key, payload)
            return payload

        return None
    except Exception as e:
        logger.warning("[Halachipedia Error] %s", e)
        return None


def _clean_html_text(value):
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def search_hebrewbooks(query):
    """Best-effort keyword search in HebrewBooks public search endpoint."""
    cache_key = str(query or "").strip().lower()
    if cache_key:
        cached = _HEBREWBOOKS_CACHE.get(cache_key)
        if cached is not None:
            return cached

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return None

    try:
        encoded_q = quote_plus(normalized_query)
        search_url = f"https://www.hebrewbooks.org/search.aspx?st=FT&q={encoded_q}"
        response = _HTTP.get(
            search_url,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ShelahBot/1.0; +https://www.sefaria.org)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if response.status_code != 200:
            return None

        html = response.text or ""
        lowered = html.lower()
        if "just a moment" in lowered and "cloudflare" in lowered:
            # Cloudflare challenge page; no parseable search content.
            return None

        match = re.search(
            r'href="(?P<href>[^"#]*pdfpager\.aspx\?req=[^"]+)"[^>]*>(?P<title>.*?)</a>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None

        href = str(match.group("href") or "").strip()
        title_html = str(match.group("title") or "").strip()
        title = _clean_html_text(title_html)
        if not title:
            title = f"HebrewBooks search result for {normalized_query}"

        result_url = urljoin("https://www.hebrewbooks.org/", href)
        payload = {
            "title": f"[HebrewBooks] {title}",
            "summary": f"HebrewBooks keyword search match for '{normalized_query}'.",
            "url": result_url,
        }

        if cache_key:
            _HEBREWBOOKS_CACHE.set(cache_key, payload)
        return payload
    except Exception as e:
        logger.warning("[HebrewBooks Error] %s", e)
        return None


async def async_search_wikipedia(title):
    """Async Wikipedia summary lookup using httpx with shared cache semantics."""
    cache_key = str(title or "").strip().lower()
    if cache_key:
        cached = _WIKI_CACHE.get(cache_key)
        if cached is not None:
            return cached

    safe_title = str(title or "").strip()
    if not safe_title:
        return None

    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{safe_title.replace(' ', '_')}"
        client = _get_async_client()
        response = await client.get(url)

        if response.status_code != 200:
            return None

        data = response.json() or {}
        payload = {
            "title": data.get("title", ""),
            "summary": str(data.get("extract", ""))[:300],
        }
        if cache_key:
            _WIKI_CACHE.set(cache_key, payload)
        return payload
    except Exception as e:
        logger.warning("[Wiki Async Error] %s", e)
        return None


async def async_search_halachipedia(query):
    """Async Halachipedia search using MediaWiki API and httpx."""
    cache_key = str(query or "").strip().lower()
    if cache_key:
        cached = _HALACHIPEDIA_CACHE.get(cache_key)
        if cached is not None:
            return cached

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return None

    try:
        search_url = "https://halachipedia.com/api.php"
        client = _get_async_client()
        r_search = await client.get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": normalized_query,
                "utf8": "",
                "format": "json",
            },
        )
        data = r_search.json() if r_search.status_code == 200 else {}

        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            return None

        top_title = search_results[0].get("title", "")
        if not top_title:
            return None

        r_extract = await client.get(
            search_url,
            params={
                "action": "query",
                "prop": "extracts",
                "exsentences": 10,
                "exintro": 1,
                "explaintext": 1,
                "titles": top_title,
                "format": "json",
            },
        )
        if r_extract.status_code != 200:
            return None

        ext_data = r_extract.json() or {}
        pages = ext_data.get("query", {}).get("pages", {})
        for page_info in pages.values():
            payload = {
                "title": f"[Halachipedia] {page_info.get('title', '')}",
                "summary": str(page_info.get("extract", ""))[:1000],
            }
            if cache_key:
                _HALACHIPEDIA_CACHE.set(cache_key, payload)
            return payload
        return None
    except Exception as e:
        logger.warning("[Halachipedia Async Error] %s", e)
        return None


async def async_search_hebrewbooks(query):
    """Async best-effort HebrewBooks search using httpx and regex extraction."""
    cache_key = str(query or "").strip().lower()
    if cache_key:
        cached = _HEBREWBOOKS_CACHE.get(cache_key)
        if cached is not None:
            return cached

    normalized_query = str(query or "").strip()
    if not normalized_query:
        return None

    try:
        search_url = "https://www.hebrewbooks.org/search.aspx"
        client = _get_async_client()
        response = await client.get(
            search_url,
            params={"st": "FT", "q": normalized_query},
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; ShelahBot/1.0; +https://www.sefaria.org)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        if response.status_code != 200:
            return None

        html = response.text or ""
        lowered = html.lower()
        if "just a moment" in lowered and "cloudflare" in lowered:
            return None

        match = re.search(
            r'href="(?P<href>[^"#]*pdfpager\.aspx\?req=[^"]+)"[^>]*>(?P<title>.*?)</a>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return None

        href = str(match.group("href") or "").strip()
        title_html = str(match.group("title") or "").strip()
        title = _clean_html_text(title_html)
        if not title:
            title = f"HebrewBooks search result for {normalized_query}"

        payload = {
            "title": f"[HebrewBooks] {title}",
            "summary": f"HebrewBooks keyword search match for '{normalized_query}'.",
            "url": urljoin("https://www.hebrewbooks.org/", href),
        }
        if cache_key:
            _HEBREWBOOKS_CACHE.set(cache_key, payload)
        return payload
    except Exception as e:
        logger.warning("[HebrewBooks Async Error] %s", e)
        return None
