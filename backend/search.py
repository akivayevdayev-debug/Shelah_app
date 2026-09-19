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
import re
from html import unescape
from urllib.parse import quote, quote_plus, urljoin

from backend.cache import TTLCache
from backend.health_check import health

logger = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ShelahBot/1.0; +https://www.sefaria.org)"

_HTTP = requests.Session()
_HTTP.headers.update({"User-Agent": _DEFAULT_USER_AGENT})
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
        _ASYNC_HTTP_CLIENT = httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": _DEFAULT_USER_AGENT}
        )
    return _ASYNC_HTTP_CLIENT


def search_wikipedia(title):
    cache_key = str(title or "").strip().lower()
    if cache_key:
        cached = _WIKI_CACHE.get(cache_key)
        if cached is not None:
            return cached

    try:
        safe_title = quote(str(title or "").strip().replace(" ", "_"), safe="_")
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{safe_title}"
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

    if not health.is_healthy('hebcal'):
        return {"parsha": None, "portions": []}

    try:
        url = "https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on&nx=on&year=now&month=now&ss=on&mf=on&c=on&geo=zip&zip=11213"
        response = _HTTP.get(url, timeout=10)
        data = response.json()
        health.record_success('hebcal')

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
        health.record_failure('hebcal')
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
        search_url = "https://halachipedia.com/api.php"
        r_search = _HTTP.get(
            search_url,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "utf8": "",
                "format": "json",
            },
            timeout=10,
        )
        data = r_search.json()

        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            return None

        top_title = search_results[0]["title"]

        # Get intro extract for top article
        r_extract = _HTTP.get(
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
            timeout=10,
        )
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


def _parse_hebrewbooks_html(html, normalized_query):
    """Extract a HebrewBooks search-result payload from a search-results page.

    Returns None if there's no parseable result (a Cloudflare challenge page,
    or no pdfpager link at all). Shared by the sync and async HebrewBooks
    search functions below so the response-parsing branches aren't counted
    against both call sites' own complexity (SonarCloud python:S3776).
    """
    lowered = html.lower()
    if "just a moment" in lowered and "cloudflare" in lowered:
        # Cloudflare challenge page; no parseable search content.
        return None

    # Every run is bounded: an unclosed anchor repeated through a large page
    # made the unbounded `[^>]*` / lazy `.*?` rescan to the end of the document
    # from each candidate href (O(n^2); SonarCloud python:S8786). A result
    # anchor is a short tag with a short title, far inside these limits.
    match = re.search(
        r'href="(?P<href>[^"#]{0,500}pdfpager\.aspx\?req=[^"]{1,500})"[^>]{0,1000}>(?P<title>.{0,2000}?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    href = str(match.group("href") or "").strip()
    title_html = str(match.group("title") or "").strip()
    title = _clean_html_text(title_html) or f"HebrewBooks search result for {normalized_query}"

    return {
        "title": f"[HebrewBooks] {title}",
        "summary": f"HebrewBooks keyword search match for '{normalized_query}'.",
        "url": urljoin("https://www.hebrewbooks.org/", href),
    }


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
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if response.status_code != 200:
            return None

        payload = _parse_hebrewbooks_html(response.text or "", normalized_query)
        if payload and cache_key:
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
        encoded_title = quote(safe_title.replace(" ", "_"), safe="_")
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded_title}"
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
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

        if response.status_code != 200:
            return None

        payload = _parse_hebrewbooks_html(response.text or "", normalized_query)
        if payload and cache_key:
            _HEBREWBOOKS_CACHE.set(cache_key, payload)
        return payload
    except Exception as e:
        logger.warning("[HebrewBooks Async Error] %s", e)
        return None
